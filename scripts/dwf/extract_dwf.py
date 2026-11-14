#!/usr/bin/env python3
"""Bounded 2D DWF/DWFx text extraction and SVG preview worker."""

from __future__ import annotations

import argparse
from dataclasses import replace
import html
import json
import math
import re
import sys
from pathlib import Path

import ezdwf
from ezdwf.document import EntityQuery


LEGACY_DWF_HEADER = re.compile(rb"^\(DWF (V00\.\d{2})\)")
LEGACY_GEOMETRY_COMPATIBLE_VERSIONS = {b"V00.30"}
MAX_LEGACY_STRING_COUNT = 500
MAX_LEGACY_STRING_LENGTH = 1_024
SVG_TEXT_TAG = re.compile(r"<text\b[^>]*>", re.IGNORECASE)
SVG_TEXT_ROTATION = re.compile(
    r'\s+transform="rotate\(([-+]?\d+(?:\.\d+)?)\s+'
    r'[-+]?\d+(?:\.\d+)?\s+[-+]?\d+(?:\.\d+)?\)"',
    re.IGNORECASE,
)
MAX_REVIT_GEOMETRY_COMPONENTS = 40


def _sheet_bounds(sheet: object) -> tuple[float, float, float, float] | None:
    value = getattr(sheet, "content_bounds", None) or getattr(sheet, "paper_bounds", None)
    if value is None:
        value = sheet.bbox()
    if value is None or len(value) != 4:
        return None
    bounds = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in bounds):
        return None
    min_x, min_y, max_x, max_y = bounds
    if max_x <= min_x or max_y <= min_y:
        return None
    return min_x, min_y, max_x, max_y


def _unit_scale_mm(units: str | None) -> float | None:
    normalized = (units or "").strip().lower().replace(" ", "")
    return {
        "mm": 1.0,
        "millimeter": 1.0,
        "millimeters": 1.0,
        "cm": 10.0,
        "centimeter": 10.0,
        "centimeters": 10.0,
        "m": 1000.0,
        "meter": 1000.0,
        "meters": 1000.0,
        "in": 25.4,
        "inch": 25.4,
        "inches": 25.4,
        "ft": 304.8,
        "foot": 304.8,
        "feet": 304.8,
    }.get(normalized)


def _is_axis_aligned_rectangle(
    points: tuple[object, ...],
    bounds: tuple[float, float, float, float],
    tolerance: float,
    closed: bool,
) -> bool:
    if not 4 <= len(points) <= 128:
        return False
    x1, y1, x2, y2 = bounds
    first, last = points[0], points[-1]
    has_repeated_start = math.hypot(
        float(first.x) - float(last.x),
        float(first.y) - float(last.y),
    ) <= tolerance
    if not closed and not has_repeated_start:
        return False

    vertices = points[:-1] if has_repeated_start else points
    loop_points = points if has_repeated_start else points + (points[0],)
    required_corners = {(x1, y1), (x1, y2), (x2, y1), (x2, y2)}
    found_corners: set[tuple[float, float]] = set()
    for point in vertices:
        x = float(point.x)
        y = float(point.y)
        on_vertical_edge = abs(x - x1) <= tolerance or abs(x - x2) <= tolerance
        on_horizontal_edge = abs(y - y1) <= tolerance or abs(y - y2) <= tolerance
        if not (on_vertical_edge or on_horizontal_edge):
            return False
        for corner_x, corner_y in required_corners:
            if abs(x - corner_x) <= tolerance and abs(y - corner_y) <= tolerance:
                found_corners.add((corner_x, corner_y))

    for start, end in zip(loop_points, loop_points[1:]):
        horizontal = abs(float(start.y) - float(end.y)) <= tolerance
        vertical = abs(float(start.x) - float(end.x)) <= tolerance
        if not (horizontal or vertical):
            return False
    return found_corners == required_corners


def extract_sheet_geometry(sheet: object, index: int) -> dict[str, object]:
    """Return a bounded, normalized set of supported 2D geometry candidates.

    Coordinates are normalized to the sheet bounds because many DWF files do
    not retain a trustworthy paper-unit scale. The app maps these candidates
    onto the user-confirmed Revit Length and Width.
    """
    bounds = _sheet_bounds(sheet)
    label = (getattr(sheet, "title", None) or getattr(sheet, "name", None) or f"第 {index} 張圖紙").strip()
    entities = list(getattr(sheet, "all_entities", ()))
    if bounds is None:
        return {
            "index": index,
            "title": label,
            "units": getattr(sheet, "units", None),
            "unitScaleMm": _unit_scale_mm(getattr(sheet, "units", None)),
            "bounds": None,
            "sourceEntityCount": len(entities),
            "candidateCount": 0,
            "truncated": False,
            "components": [],
        }

    min_x, min_y, max_x, max_y = bounds
    sheet_width = max_x - min_x
    sheet_height = max_y - min_y
    candidates: list[dict[str, float | str]] = []
    seen: set[tuple[object, ...]] = set()

    for entity in entities:
        kind = str(getattr(entity, "kind", "")).upper()
        entity_bounds = entity.bbox()
        if entity_bounds is None:
            continue
        x1, y1, x2, y2 = (float(value) for value in entity_bounds)
        width = x2 - x1
        height = y2 - y1
        if (
            not all(math.isfinite(value) for value in (x1, y1, x2, y2))
            or width <= 0
            or height <= 0
        ):
            continue

        width_ratio = width / sheet_width
        height_ratio = height / sheet_height
        area_ratio = width_ratio * height_ratio
        # Reject dimension ticks, dots and frame-like strips. A legitimate
        # standalone object may occupy the full content bounds, so never reject
        # a large shape only because it fills the drawing.
        if (
            width_ratio < 0.001
            or height_ratio < 0.001
            or area_ratio < 0.000002
            or (max(width_ratio, height_ratio) > 0.98 and min(width_ratio, height_ratio) < 0.01)
        ):
            continue
        # A full-sheet rectangle surrounding other entities is normally a page
        # frame. Excluding it is safer than extruding an enclosing solid around
        # every actual component. A standalone full-bounds object is retained.
        if width_ratio >= 0.995 and height_ratio >= 0.995 and len(entities) > 1:
            continue

        component_kind = ""
        if kind == "CIRCLE" and getattr(entity, "center", None) is not None:
            component_kind = "circle"
        elif kind == "POLYLINE":
            points = tuple(getattr(entity, "points", ()))
            tolerance = max(sheet_width, sheet_height) * 0.000001
            if not _is_axis_aligned_rectangle(
                points,
                (x1, y1, x2, y2),
                tolerance,
                bool(getattr(entity, "closed", False)),
            ):
                continue
            component_kind = "rect"
        else:
            continue

        center_x = ((x1 + x2) / 2.0 - min_x) / sheet_width
        center_y = ((y1 + y2) / 2.0 - min_y) / sheet_height
        key = (
            component_kind,
            round(center_x, 4),
            round(center_y, 4),
            round(width_ratio, 4),
            round(height_ratio, 4),
        )
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "kind": component_kind,
            "centerX": round(center_x, 7),
            "centerY": round(center_y, 7),
            "width": round(width_ratio, 7),
            "height": round(height_ratio, 7),
            "area": area_ratio,
        })

    candidates.sort(key=lambda item: float(item["area"]), reverse=True)
    total_candidates = len(candidates)
    components = [
        {key: value for key, value in candidate.items() if key != "area"}
        for candidate in candidates[:MAX_REVIT_GEOMETRY_COMPONENTS]
    ]
    return {
        "index": index,
        "title": label,
        "units": getattr(sheet, "units", None),
        "unitScaleMm": _unit_scale_mm(getattr(sheet, "units", None)),
        "bounds": {
            "minX": min_x,
            "minY": min_y,
            "maxX": max_x,
            "maxY": max_y,
            "width": sheet_width,
            "height": sheet_height,
        },
        "sourceEntityCount": len(entities),
        "candidateCount": total_candidates,
        "truncated": total_candidates > MAX_REVIT_GEOMETRY_COMPONENTS,
        "components": components,
    }


def fail(message: str) -> None:
    print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
    raise SystemExit(1)


def legacy_dwf_text(source: Path, max_text_characters: int) -> tuple[str, list[str]] | None:
    """Extract bounded printable metadata from legacy V00.xx DWF files.

    Some older WHIP streams are valid DWF files that ezdwf recognizes but
    cannot decode into geometry. This fallback
    deliberately never attempts to interpret binary drawing instructions. It
    only retains printable ASCII runs for search and a transparent
    compatibility preview.
    """
    raw = source.read_bytes()
    header = LEGACY_DWF_HEADER.match(raw)
    if header is None or not raw.rstrip(b"\x00\r\n\t ").endswith(b"(EndOfDWF)"):
        return None
    version = header.group(1).decode("ascii")

    strings: list[str] = []
    current = bytearray()
    total_characters = 0

    def add_current() -> bool:
        nonlocal current, total_characters
        if len(current) < 4:
            current = bytearray()
            return False
        value = current.decode("ascii", errors="ignore")
        current = bytearray()
        value = re.sub(r"\s+", " ", value).strip()
        if (
            len(value) < 4
            or value.startswith("(DWF ")
            or value == "(EndOfDWF)"
            or not re.search(r"[A-Za-z0-9]", value)
            or value in strings
        ):
            return False
        remaining = max_text_characters - total_characters
        if remaining <= 0:
            return True
        value = value[:remaining]
        strings.append(value)
        total_characters += len(value)
        return total_characters >= max_text_characters or len(strings) >= MAX_LEGACY_STRING_COUNT

    for byte in raw:
        if 32 <= byte <= 126:
            if len(current) < MAX_LEGACY_STRING_LENGTH:
                current.append(byte)
            continue
        if add_current():
            break
    if len(strings) < MAX_LEGACY_STRING_COUNT and total_characters < max_text_characters:
        add_current()
    return version, strings


def geometry_compatible_source(source: Path) -> Path | bytes:
    """Open verified legacy streams through the compatible WHIP decoder.

    ezdwf 0.0.6 can decode the V00.30 binary opcodes used by legacy DWF files,
    but its public version gate starts at V00.34. The stream grammar is
    compatible, so replace only the fixed-width version token in memory. The
    original file is never changed, and malformed/truncated files still follow
    the normal validation and fallback paths.
    """
    with source.open("rb") as handle:
        prefix = handle.read(13)
    header = LEGACY_DWF_HEADER.match(prefix)
    if header is None or header.group(1) not in LEGACY_GEOMETRY_COMPATIBLE_VERSIONS:
        return source

    raw = source.read_bytes()
    if not raw.rstrip(b"\x00\r\n\t ").endswith(b"(EndOfDWF)"):
        return source
    return b"(DWF V00.34)" + raw[12:]


def preview_swipe_bridge() -> str:
    return (
        "<script>(()=>{let x=null,y=null;"
        "const r=()=>{x=null;y=null};"
        "document.addEventListener('touchstart',e=>{"
        "if(e.touches.length!==1){r();return}"
        "x=e.touches[0].clientX;y=e.touches[0].clientY},{passive:true});"
        "document.addEventListener('touchend',e=>{"
        "if(x===null||y===null||e.changedTouches.length!==1){r();return}"
        "const dx=e.changedTouches[0].clientX-x,dy=e.changedTouches[0].clientY-y;r();"
        "if(Math.abs(dx)<56||Math.abs(dx)<=Math.abs(dy)*1.25)return;"
        "const p=JSON.stringify({type:'reference-preview-swipe',direction:dx<0?'next':'previous'});"
        "if(window.ReactNativeWebView&&typeof window.ReactNativeWebView.postMessage==='function')"
        "window.ReactNativeWebView.postMessage(p);"
        "else if(window.parent&&window.parent!==window)window.parent.postMessage(p,'*')"
        "},{passive:true});"
        "document.addEventListener('touchcancel',r,{passive:true})})();</script>"
    )


def legacy_dwf_preview(version: str, strings: list[str]) -> str:
    rows = "".join(f"<li>{html.escape(value)}</li>" for value in strings)
    if not rows:
        rows = "<li>未找到可讀取的內嵌文字。</li>"
    return "".join((
        "<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'\">",
        "<title>舊版 DWF 預覽</title><style>",
        "body{margin:0;background:#edf1f5;color:#10233d;font-family:system-ui,sans-serif}",
        "main{max-width:860px;margin:0 auto;padding:20px}",
        "section{background:#fff;border:1px solid #d9e1ea;border-radius:10px;padding:18px}",
        "h1{margin:0 0 12px;font-size:20px}",
        "p{line-height:1.6} .notice{background:#fff8e1;border-color:#efd893}",
        "ul{padding-left:20px;word-break:break-word}</style></head><body><main>",
        f"<section class='notice'><h1>舊版 DWF {html.escape(version)}</h1>",
        "<p>系統已擷取可搜尋的內嵌文字，但此舊格式未能在瀏覽器重繪原圖。請下載原檔並使用相容的 DWF 檢視器開啟圖紙。</p></section>",
        "<section><h2>可讀取的內嵌文字</h2><ul>", rows, "</ul></section>",
        "</main>", preview_swipe_bridge(), "</body></html>",
    ))


def normalize_uniform_quarter_turn_text(svg: str) -> str:
    """Remove a duplicated sheet rotation applied uniformly to SVG text.

    Some landscape W2D sheets are already emitted in paper coordinates, but
    ezdwf also applies the sheet's +/-90 degree orientation to every text
    element. Geometry is correct while every label becomes vertical and
    displaced. Only normalize when a sheet has enough text to identify the
    pattern and every text tag carries the same quarter-turn rotation.
    """
    tags = SVG_TEXT_TAG.findall(svg)
    if len(tags) < 8:
        return svg

    angles: list[float] = []
    for tag in tags:
        match = SVG_TEXT_ROTATION.search(tag)
        if match is None:
            return svg
        angles.append(float(match.group(1)))

    first = angles[0]
    if not (abs(first - 90.0) <= 0.05 or abs(first + 90.0) <= 0.05):
        return svg
    if any(abs(angle - first) > 0.05 for angle in angles[1:]):
        return svg

    return SVG_TEXT_TAG.sub(
        lambda match: SVG_TEXT_ROTATION.sub("", match.group(0), count=1),
        svg,
    )


def render_sheet_svg_without_packaged_glyphs(sheet: object) -> str:
    """Render text as SVG text instead of opaque packaged-font outlines.

    ezdwf prefers packaged DWF glyph outlines when they are present. Those
    outlines can be rectangles or otherwise unusable when the source CAD font
    is a missing/obfuscated CJK font. Keeping the decoded entity text lets the
    preview HTML and the server-side PDF renderer use the bundled CJK font.
    """
    entities = EntityQuery(
        replace(entity, glyph_outline=None)
        if entity.kind == "TEXT" and entity.text
        else entity
        for entity in sheet.entities
    )
    return replace(sheet, entities=entities).render_svg()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_path")
    parser.add_argument("--preview-output")
    parser.add_argument("--max-sheets", type=int, default=60)
    parser.add_argument("--max-preview-sheets", type=int, default=10)
    parser.add_argument("--max-text-characters", type=int, default=300_000)
    parser.add_argument("--include-geometry", action="store_true")
    args = parser.parse_args()

    source = Path(args.input_path)
    if not source.is_file() or source.stat().st_size <= 0:
        fail("DWF 檔案不存在或內容為空")

    limits = ezdwf.ParseLimits(
        max_file_size=20 * 1024 * 1024,
        max_archive_entries=1_000,
        max_entry_size=20 * 1024 * 1024,
        max_total_uncompressed_size=64 * 1024 * 1024,
        max_compression_ratio=100,
        max_xml_size=8 * 1024 * 1024,
        max_w2d_records=500_000,
        max_w2d_total_points=2_000_000,
        max_w2d_string_size=256_000,
        max_xps_visuals=500_000,
        max_xps_path_segments=2_000_000,
    )
    try:
        drawing = ezdwf.read(geometry_compatible_source(source), limits=limits)
    except ezdwf.UnsupportedDwfError:
        legacy = legacy_dwf_text(source, args.max_text_characters)
        if legacy is None:
            fail("DWF 文件無法解析；只支援未損壞的 2D DWF 或 DWFx")
        version, strings = legacy
        sections = [{
            "content": "\n".join(strings) or f"舊版 DWF {version}：未找到可讀取的內嵌文字。",
            "sourceLabel": f"舊版 DWF {version}（內嵌文字）",
            "pageNumber": 1,
        }]
        if args.preview_output:
            Path(args.preview_output).write_text(legacy_dwf_preview(version, strings), encoding="utf-8")
        print(json.dumps({
            "sections": sections,
            **({"geometry": {
                "version": 1,
                "sheets": [],
                "warnings": ["此舊版 DWF 只能讀取文字，未能解碼可供 Revit 使用的2D幾何。"],
            }} if args.include_geometry else {}),
        }, ensure_ascii=False))
        return
    except ezdwf.InvalidDwfError:
        fail("DWF 文件無法解析；只支援未損壞的 2D DWF 或 DWFx")
    except ezdwf.DwfLimitError:
        fail("DWF 內容超出安全處理限制，請分拆圖紙後再試")
    except Exception:
        fail("DWF 文件處理失敗")

    if not drawing.sheets:
        fail("DWF 未包含可讀取的 2D 圖紙；暫未支援只有 3D 模型的 DWF")
    if len(drawing.sheets) > args.max_sheets:
        fail(f"DWF 圖紙數量超過上限（最多 {args.max_sheets} 張）")

    sections: list[dict[str, object]] = []
    total_text = 0
    for index, sheet in enumerate(drawing.sheets, start=1):
        lines = [entity.text.strip() for entity in sheet.all_entities if entity.text and entity.text.strip()]
        content = "\n".join(lines).strip()
        total_text += len(content)
        if total_text > args.max_text_characters:
            fail("DWF 可擷取文字過多，請分拆圖紙後再試")
        label = (sheet.title or sheet.name or f"第 {index} 張圖紙").strip()
        sections.append({
            "content": content,
            "sourceLabel": f"圖紙：{label}",
            "pageNumber": index,
        })

    if args.preview_output:
        preview_parts = [
            "<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width,initial-scale=1'>",
            "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src http: https:; script-src 'unsafe-inline'\">",
            "<title>DWF 預覽</title><style>",
            "body{margin:0;background:#edf1f5;color:#10233d;font-family:system-ui,sans-serif}",
            "header{padding:16px 20px;background:#fff;border-bottom:1px solid #d9e1ea;font-weight:700}",
            "section{margin:16px;background:#fff;border:1px solid #d9e1ea;border-radius:10px;overflow:auto}",
            "h2{margin:0;padding:12px 16px;font-size:15px;border-bottom:1px solid #e5eaf0}",
            "@font-face{font-family:'ReferenceDwfCjk';src:url('__REFERENCE_DWF_CJK_FONT__') format('opentype');font-display:swap}",
            "svg{display:block;width:100%;height:auto;min-height:280px}",
            "svg text{font-family:'ReferenceDwfCjk',sans-serif!important}</style></head><body>",
            f"<header>DWF 預覽（首 {min(len(drawing.sheets), args.max_preview_sheets)} 張圖紙）</header>",
        ]
        for index, sheet in enumerate(drawing.sheets[:args.max_preview_sheets], start=1):
            label = html.escape((sheet.title or sheet.name or f"第 {index} 張圖紙").strip())
            try:
                svg = normalize_uniform_quarter_turn_text(
                    render_sheet_svg_without_packaged_glyphs(sheet)
                )
            except Exception:
                continue
            if len(svg.encode("utf-8")) > 4 * 1024 * 1024:
                continue
            preview_parts.extend((f"<section><h2>{label}</h2>", svg, "</section>"))
        preview_parts.extend((preview_swipe_bridge(), "</body></html>"))
        preview = "".join(preview_parts)
        if len(preview.encode("utf-8")) > 12 * 1024 * 1024:
            fail("DWF 預覽內容過大，請分拆圖紙後再試")
        Path(args.preview_output).write_text(preview, encoding="utf-8")

    print(json.dumps({
        "sections": sections,
        **({"geometry": {
            "version": 1,
            "sheets": [
                extract_sheet_geometry(sheet, index)
                for index, sheet in enumerate(drawing.sheets, start=1)
            ],
            "warnings": [
                "DWF是2D來源；Height需要由使用者確認。",
                "沒有可靠單位的圖紙會分別按目前Length及Width縮放，不會假設原始座標是毫米；比例可能改變。",
                "貼齊整張圖紙並包圍其他實體的矩形會視為頁框排除；只會轉換可驗證的矩形及圓形。",
            ],
        }} if args.include_geometry else {}),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()