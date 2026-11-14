"""Read and apply safe, geometry-only edits to a DXF drawing.

The mobile editor uses this as a small interchange layer.  The original DXF
remains the source of truth; the client only sends explicit entity operations.
Unsupported entities are left untouched and remain visible in the full CAD
preview.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import re
import tempfile
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import path as ezdxf_path
from ezdxf import bbox as ezdxf_bbox
from ezdxf import recover as ezdxf_recover
from ezdxf.addons.drawing import RenderContext
from revit_geometry_measurements import measure_orthographic_box

try:
    from build_schematic import build_full_cad_review_svg
except Exception:
    build_full_cad_review_svg = None


VIEWBOX_WIDTH = 1_000_000
MAX_ENTITIES = 20_000
MAX_FULL_SVG_ENTITIES = 8_000
MAX_FULL_SVG_BYTES = 30 * 1024 * 1024
MAX_REVIT_DIMENSION_ENTITIES = 80
MAX_FITTING_CANDIDATES = 1000

DIMENSION_UNIT_FACTORS = {
    "mm": 1.0,
    "cm": 10.0,
    "m": 1000.0,
    "in": 25.4,
    "ft": 304.8,
    "yd": 914.4,
}


def read_document(path: str):
    """Load converted DXFs with structural recovery enabled.

    LibreDWG can emit an incomplete entity boundary for a single polyline while
    the rest of Model Space remains valid. The recovery reader discards or
    repairs that entity instead of rejecting the complete drawing.
    """
    try:
        document, _auditor = ezdxf_recover.readfile(path)
        return document
    except ValueError as error:
        if "Invalid handle 0" not in str(error):
            raise

        repaired_path = repair_zero_entity_handles(path)
        if repaired_path is None:
            raise
        try:
            document, _auditor = ezdxf_recover.readfile(repaired_path)
            return document
        finally:
            try:
                Path(repaired_path).unlink()
            except OSError:
                pass


def read_document_for_inspection(path: str):
    """Quickly load read-only preview/validation work before using recovery.

    The recovery loader performs extensive structural rebuilding on large DXFs.
    A LibreDWG output can be valid and loadable by ezdxf's normal reader while
    recovery exceeds the API child-process timeout. Keep recovery as the
    fallback for malformed files, and continue to use it for edit/write paths.
    """
    try:
        return ezdxf.readfile(path)
    except Exception:
        return read_document(path)


def repair_zero_entity_handles(path: str) -> str | None:
    """Remove only entity self-handles encoded as group code 5/105 = 0.

    ezdxf's recovery loader already repairs malformed non-hex handles, but
    treats the hexadecimal value "0" as a special invalid entity handle. Some
    LibreDWG conversions emit that value for an otherwise usable entity.
    Pointer handles such as 330 are deliberately left untouched.
    """
    source_path = Path(path)
    repaired_path: str | None = None
    repair_count = 0
    try:
        with source_path.open("r", encoding="utf-8", errors="surrogateescape", newline="") as source:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                errors="surrogateescape",
                newline="",
                dir=source_path.parent,
                prefix=f".{source_path.name}.handle-repair-",
                suffix=".dxf",
                delete=False,
            ) as target:
                repaired_path = target.name
                pending_code = source.readline()
                while pending_code:
                    value = source.readline()
                    if not value:
                        target.write(pending_code)
                        break
                    if pending_code.strip() in {"5", "105"} and value.strip() == "0":
                        repair_count += 1
                    else:
                        target.write(pending_code)
                        target.write(value)
                    pending_code = source.readline()
    except Exception:
        if repaired_path is not None:
            try:
                Path(repaired_path).unlink()
            except OSError:
                pass
        raise

    if repair_count == 0:
        if repaired_path is not None:
            Path(repaired_path).unlink()
        return None
    return repaired_path


def point_tuple(value: Any) -> list[float]:
    return [float(value[0]), float(value[1])]


def finite_points(points: list[list[float]]) -> list[list[float]]:
    return [
        [x, y]
        for x, y in points
        if math.isfinite(x) and math.isfinite(y)
    ]


def entity_points(entity: Any) -> tuple[str, list[list[float]], bool] | None:
    kind = entity.dxftype()
    if kind == "LINE":
        return (
            "LINE",
            [point_tuple(entity.dxf.start), point_tuple(entity.dxf.end)],
            False,
        )
    if kind == "LWPOLYLINE":
        points = [[float(x), float(y)] for x, y, *_ in entity.get_points("xyseb")]
        return "LWPOLYLINE", points, bool(entity.closed)
    if kind == "POLYLINE":
        points = [point_tuple(vertex.dxf.location) for vertex in entity.vertices]
        return "POLYLINE", points, bool(entity.is_closed)
    return None


def _bulge_segment_length(start: tuple[float, float], end: tuple[float, float], bulge: float) -> float:
    chord = math.hypot(end[0] - start[0], end[1] - start[1])
    if not math.isfinite(bulge) or abs(bulge) < 1e-12:
        return chord
    included_angle = 4 * math.atan(abs(bulge))
    radius = chord * (1 + bulge * bulge) / (4 * abs(bulge))
    return radius * included_angle


def entity_measure_length(entity: Any) -> float:
    """Measure editable plan geometry in drawing units, including polyline arcs."""
    kind = entity.dxftype()
    if kind == "LINE":
        start = entity.dxf.start
        end = entity.dxf.end
        return math.hypot(float(end.x) - float(start.x), float(end.y) - float(start.y))

    if kind == "LWPOLYLINE":
        vertices = [
            (float(x), float(y), float(bulge))
            for x, y, bulge in entity.get_points("xyb")
        ]
        closed = bool(entity.closed)
    elif kind == "POLYLINE":
        vertices = [
            (
                float(vertex.dxf.location.x),
                float(vertex.dxf.location.y),
                float(vertex.dxf.get("bulge", 0.0)),
            )
            for vertex in entity.vertices
        ]
        closed = bool(entity.is_closed)
    else:
        return 0.0

    total = 0.0
    segment_count = len(vertices) if closed else max(0, len(vertices) - 1)
    for index in range(segment_count):
        start = vertices[index]
        end = vertices[(index + 1) % len(vertices)]
        total += _bulge_segment_length(start[:2], end[:2], start[2])
    return total


def drawing_unit_info(document: Any) -> tuple[int, str, float | None]:
    try:
        unit_code = int(document.header.get("$INSUNITS", 0))
        if unit_code > 0:
            unit_name = ezdxf.units.unit_name(unit_code)
            unit_to_mm = float(ezdxf.units.conversion_factor(unit_code, ezdxf.units.MM))
            if math.isfinite(unit_to_mm) and unit_to_mm > 0:
                return unit_code, unit_name, unit_to_mm
    except Exception:
        pass
    return 0, "未指定", None


def all_bounds(
    entities: list[Any],
    editable: list[dict[str, Any]],
    *,
    fast: bool = False,
) -> tuple[float, float, float, float]:
    try:
        bounds = ezdxf_bbox.extents(entities, fast=fast)
        if bounds.has_data:
            min_x, min_y, _ = bounds.extmin
            max_x, max_y, _ = bounds.extmax
            if max_x > min_x and max_y > min_y:
                return float(min_x), float(min_y), float(max_x), float(max_y)
    except Exception:
        pass

    points = [
        point
        for item in editable
        for point in item["points"]
    ]
    if not points:
        raise ValueError("Model Space 沒有可預覽的 CAD 內容")
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    if max_x <= min_x:
        max_x = min_x + 1
    if max_y <= min_y:
        max_y = min_y + 1
    return min_x, min_y, max_x, max_y


def svg_viewbox(svg: str, bounds: tuple[float, float, float, float]) -> list[float]:
    match = re.search(r'viewBox="0 0 ([\d.eE+-]+) ([\d.eE+-]+)"', svg)
    if match:
        return [0.0, 0.0, float(match.group(1)), float(match.group(2))]
    min_x, min_y, max_x, max_y = bounds
    width = max_x - min_x
    height = max(max_y - min_y, width * 0.01)
    return [0.0, 0.0, float(VIEWBOX_WIDTH), float(round(VIEWBOX_WIDTH * height / width))]


def to_view_point(
    point: list[float],
    bounds: tuple[float, float, float, float],
    viewbox: list[float],
) -> list[float]:
    min_x, min_y, max_x, max_y = bounds
    _, _, view_width, view_height = viewbox
    width = max(max_x - min_x, 1e-9)
    height = max(max_y - min_y, 1e-9)
    return [
        (point[0] - min_x) / width * view_width,
        (max_y - point[1]) / height * view_height,
    ]


def fitting_candidates(document: Any, entities: list[Any] | None = None) -> list[dict[str, Any]]:
    """Find fitting-like INSERT references for human review, never installed quantities."""
    entities = entities if entities is not None else list(document.modelspace())
    metadata_cache: dict[str, list[str]] = {}

    def block_metadata(name: str, depth: int = 0, visited: set[str] | None = None) -> list[str]:
        if name in metadata_cache:
            return metadata_cache[name]
        if depth > 3:
            return []
        visited = set() if visited is None else visited
        if name in visited:
            return []
        visited.add(name)
        values = [name]
        try:
            block = document.blocks.get(name)
            for child in itertools.islice(block, 500):
                try:
                    values.append(str(child.dxf.get("layer", "")))
                    if child.dxftype() == "INSERT":
                        values.extend(block_metadata(str(child.dxf.get("name", "")), depth + 1, visited))
                    elif child.dxftype() in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
                        text = child.dxf.get("text", "")
                        if not text and child.dxftype() == "MTEXT":
                            text = child.text
                        values.append(str(text))
                except Exception:
                    continue
        except Exception:
            pass
        metadata_cache[name] = values[:2001]
        return metadata_cache[name]

    def classify(values: list[str]) -> tuple[str, str]:
        combined = " ".join(values).upper()
        if re.search(r"\bTEE\b|三通", combined):
            return "TEE", "圖塊名稱／內層資料"
        if re.search(r"ELBOW|ELB\b|彎頭", combined):
            return "ELBOW", "圖塊名稱／內層資料"
        if re.search(r"UNION|梳杰|梳傑", combined):
            return "UNION", "圖塊名稱／內層資料"
        if re.search(r"\bG\.?V\.?(?:\b|$)|VALVE|閥", combined):
            return "GV", "圖塊名稱／內層資料"
        return "UNKNOWN", "TG 圖層候選"

    def nominal_size(values: list[str]) -> int | None:
        for value in values:
            match = re.search(r"(?:DN|DIA\.?\s*|Ø\s*)(\d{2,4})\s*(?:MM)?", value, re.IGNORECASE)
            if match:
                return int(match.group(1))
        for value in values:
            match = re.match(r"\s*(\d{2,4})\s*(?:MM\b|(?=[A-Z_\s-]))", value, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    bbox_cache = ezdxf_bbox.Cache()
    result: list[dict[str, Any]] = []
    for index, entity in enumerate(entities):
        if entity.dxftype() != "INSERT":
            continue
        try:
            name = str(entity.dxf.get("name", "")).strip()
            layer = str(entity.dxf.get("layer", ""))
            if not name:
                continue
            gas_layer = bool(re.search(r"(^|[-_ ])TG($|[-_ ])|PIPE|GAS|VALVE|FITT", layer, re.IGNORECASE))
            if not gas_layer and classify([name])[0] == "UNKNOWN":
                continue
            evidence = [name, layer, *block_metadata(name)]
            category, basis = classify(evidence)
            bounds = ezdxf_bbox.extents([entity], fast=True, cache=bbox_cache)
            x = y = None
            anchor_method = "unlocated"
            if bounds.has_data:
                center = bounds.center
                if math.isfinite(center.x) and math.isfinite(center.y):
                    x, y = float(center.x), float(center.y)
                    anchor_method = "transformed-block-bounds"
            result.append({
                "handle": str(entity.dxf.get("handle", "")),
                "entityIndex": index,
                "category": category,
                "blockName": name[:160],
                "layer": layer[:160],
                "nominalSizeMm": nominal_size(evidence),
                "x": x,
                "y": y,
                "anchorMethod": anchor_method,
                "basis": basis,
            })
            if len(result) > MAX_FITTING_CANDIDATES:
                raise ValueError(
                    f"候選圖塊超過安全上限（{MAX_FITTING_CANDIDATES}）；請先按樓層或圖框分拆圖紙"
                )
        except ValueError:
            raise
        except Exception:
            continue

    result.sort(key=lambda row: (
        row["y"] is None, -(row["y"] or 0), row["x"] or 0,
        row["blockName"], row["entityIndex"],
    ))
    for ordinal, row in enumerate(result, 1):
        row["id"] = f"C{ordinal:03d}"
    return result


def inspect(path: str, *, fast_only: bool = False) -> dict[str, Any]:
    # Import candidates must not enter structural recovery until every normal
    # writer has been tried. Return the editor model from this same read.
    document = ezdxf.readfile(path) if fast_only else read_document_for_inspection(path)
    entities = list(document.modelspace())
    if len(entities) > MAX_ENTITIES:
        raise ValueError(f"圖紙圖元數量超過上限（{MAX_ENTITIES}）")
    unit_code, unit_name, unit_to_mm = drawing_unit_info(document)

    editable: list[dict[str, Any]] = []
    for index, entity in enumerate(entities):
        try:
            geometry = entity_points(entity)
        except Exception:
            # A recovered malformed entity is preserved as unsupported
            # background content rather than blocking the complete drawing.
            continue
        if not geometry:
            continue
        kind, points, closed = geometry
        points = finite_points(points)
        if len(points) < 2:
            continue
        editable.append({
            "id": str(index),
            "index": index,
            "kind": kind,
            "layer": str(getattr(entity.dxf, "layer", "")),
            "points": points,
            "closed": closed,
            "lengthDrawingUnits": entity_measure_length(entity),
        })

    # Large drawings do not receive the full background SVG; avoid the costly
    # exact curve expansion for that review-only viewport while retaining the
    # exact path for normal-sized drawings and editable exports.
    bounds = all_bounds(entities, editable, fast=len(entities) > MAX_FULL_SVG_ENTITIES)
    try:
        context = RenderContext(document)
    except Exception:
        # A malformed layer/style table can still leave the geometry usable.
        # Report substitutions instead of failing to open the whole drawing.
        context = None
    color_fallback_count = 0
    flatten_tolerance = max((bounds[2] - bounds[0]) / 20000, 0.001)
    for item in editable:
        entity = entities[item["index"]]
        try:
            if context is None:
                raise ValueError("無法讀取圖層顏色")
            properties = context.resolve_all(entity)
            item["color"] = "#dbeafe" if properties.color.lower() == "#000000" else properties.color
            item["visible"] = bool(properties.is_visible)
        except Exception:
            color_fallback_count += 1
            item["color"] = "#dbeafe"
            item["visible"] = True
        if item["kind"] != "LINE":
            try:
                sampled = list(ezdxf_path.make_path(entity).flattening(distance=flatten_tolerance))
                if 2 <= len(sampled) <= 10000:
                    item["renderPoints"] = [
                        [float(point.x), float(point.y)] for point in sampled
                    ]
            except Exception:
                pass
    full_svg = ""
    full_preview_available = False
    source_size = Path(path).stat().st_size
    if (
        build_full_cad_review_svg is not None
        and len(entities) <= MAX_FULL_SVG_ENTITIES
        and source_size <= MAX_FULL_SVG_BYTES
    ):
        try:
            full_svg = build_full_cad_review_svg(
                {"path": path, "floor": "Model Space"},
                [],
                [],
                editor_preview=True,
                render_bounds=bounds,
            ) or ""
            full_preview_available = bool(full_svg)
        except Exception:
            # Large or malformed drawings still retain their editable geometry;
            # the complete background preview is optional and resource-heavy.
            full_svg = ""
    viewbox = svg_viewbox(full_svg, bounds)
    if not full_svg:
        full_svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {viewbox[2]:.2f} {viewbox[3]:.2f}"></svg>'
        )
    for item in editable:
        item["viewPoints"] = [
            to_view_point(point, bounds, viewbox)
            for point in item["points"]
        ]
        if "renderPoints" in item:
            item["renderPoints"] = [
                to_view_point(point, bounds, viewbox)
                for point in item["renderPoints"]
            ]

    return {
        "fileName": Path(path).name,
        "drawingUnitCode": unit_code,
        "drawingUnitName": unit_name,
        "drawingUnitToMm": unit_to_mm,
        "entityCount": len(entities),
        "editableEntityCount": len(editable),
        "unsupportedEntityCount": max(0, len(entities) - len(editable)),
        "fullPreviewAvailable": full_preview_available,
        "colorFallbackCount": color_fallback_count,
        "bounds": list(bounds),
        "viewBox": viewbox,
        "entities": editable,
        "svg": full_svg,
        "fittingCandidates": fitting_candidates(document, entities),
    }


def as_float(value: Any, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} 必須是有效數字")
    return number


def operation_points(raw: Any) -> list[list[float]]:
    if not isinstance(raw, list) or len(raw) > 10_000:
        raise ValueError("圖元座標格式不正確")
    points: list[list[float]] = []
    for point in raw:
        if not isinstance(point, list) or len(point) < 2:
            raise ValueError("圖元座標格式不正確")
        points.append([as_float(point[0], "x"), as_float(point[1], "y")])
    if len(points) < 2:
        raise ValueError("圖元至少需要兩個座標點")
    return points


def apply_entity_points(entity: Any, points: list[list[float]]) -> None:
    kind = entity.dxftype()
    if kind == "LINE":
        entity.dxf.start = (points[0][0], points[0][1], 0)
        entity.dxf.end = (points[1][0], points[1][1], 0)
    elif kind == "LWPOLYLINE":
        existing = list(entity.get_points("xyseb"))
        bulges = [row[2] if len(row) > 2 else 0 for row in existing]
        widths = [(row[3], row[4]) if len(row) > 4 else (0, 0) for row in existing]
        updated = []
        for index, point in enumerate(points):
            bulge = bulges[index] if index < len(bulges) else 0
            start_width, end_width = widths[index] if index < len(widths) else (0, 0)
            updated.append((point[0], point[1], bulge, start_width, end_width))
        entity.set_points(updated, format="xyseb")
    elif kind == "POLYLINE":
        vertices = list(entity.vertices)
        for vertex, point in zip(vertices, points):
            vertex.dxf.location = (point[0], point[1], 0)
    else:
        raise ValueError(f"不支援修改 {kind} 圖元")


def apply_operation(entity_list: list[Any], operation: dict[str, Any]) -> None:
    op = operation.get("op")
    if op == "moveDrawing":
        dx = as_float(operation.get("dx"), "dx")
        dy = as_float(operation.get("dy"), "dy")
        for entity in entity_list:
            try:
                entity.translate(dx, dy, 0)
            except Exception:
                try:
                    geometry = entity_points(entity)
                except Exception:
                    continue
                if geometry:
                    _, points, _ = geometry
                    apply_entity_points(entity, [[x + dx, y + dy] for x, y in points])
        return

    if op not in {"moveEntity", "setEntityPoints", "deleteEntity"}:
        raise ValueError(f"不支援的編輯指令：{op}")
    index = int(operation.get("index"))
    if index < 0 or index >= len(entity_list):
        raise ValueError("圖元索引無效")
    entity = entity_list[index]

    if op == "deleteEntity":
        entity.destroy()
    elif op == "moveEntity":
        dx = as_float(operation.get("dx"), "dx")
        dy = as_float(operation.get("dy"), "dy")
        geometry = entity_points(entity)
        if not geometry:
            raise ValueError("只可以移動 LINE、LWPOLYLINE 或 POLYLINE")
        _, points, closed = geometry
        apply_entity_points(entity, [[x + dx, y + dy] for x, y in points])
    else:
        apply_entity_points(entity, operation_points(operation.get("points")))


def apply(path: str, output: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
    if len(operations) > 500:
        raise ValueError("一次最多套用 500 個編輯操作")
    document = read_document(path)
    entities = list(document.modelspace())
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("編輯操作格式不正確")
        apply_operation(entities, operation)
    document.saveas(output)
    return {"appliedOperationCount": len(operations), "entityCount": len(entities)}


def validate(path: str, *, fast_only: bool = False) -> dict[str, Any]:
    """Verify that Model Space can be read without rendering the file."""
    # Try every writer's normal output before spending the shared import budget
    # recovering a broken candidate. Keep recovery available in the second pass.
    document = ezdxf.readfile(path) if fast_only else read_document_for_inspection(path)
    entity_count = sum(1 for _entity in document.modelspace())
    if entity_count == 0:
        raise ValueError("Model Space 沒有可用的 CAD 圖元")
    return {"entityCount": entity_count}


def revit_preview(path: str) -> dict[str, Any]:
    """Render every visible entity, not the editor's background-only SVG."""
    if Path(path).stat().st_size > MAX_FULL_SVG_BYTES:
        raise ValueError("DWG 轉換圖面過大，請另存為 PDF")
    document = read_document(path)
    entities = list(document.modelspace())
    if not entities or len(entities) > MAX_FULL_SVG_ENTITIES:
        raise ValueError("DWG 圖元數量超出預覽限制，請分拆或另存為 PDF")
    if build_full_cad_review_svg is None:
        raise ValueError("CAD 預覽工具未準備好")
    bounds = all_bounds(entities, [])
    svg = build_full_cad_review_svg(
        {"path": path, "_cadDocument": document, "floor": "Model Space"},
        [], [], editor_preview=False, render_bounds=bounds,
    )
    if not svg or len(svg.encode("utf-8")) > MAX_FULL_SVG_BYTES or not re.search(r"<(?:path|line|polyline|circle|ellipse|text|use)\b", svg):
        raise ValueError("DWG 未能產生完整的安全預覽，請另存為 PDF")
    return {"svg": svg, "fullPreviewAvailable": True, "entityCount": len(entities)}


def _dimension_text_override(entity: Any) -> str | None:
    """Return only a short, numeric dimension override; never pass arbitrary CAD text to AI."""
    value = str(entity.dxf.get("text", "") or "").strip()
    if not value or value == "<>":
        return None
    if len(value) > 48:
        return None
    allowed = re.fullmatch(
        r"\s*[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*(?:mm|cm|m|in|ft|yd)?\s*",
        value,
        re.IGNORECASE,
    )
    return value if allowed else None


def _dimension_override_mm(value: str, unit_to_mm: float | None) -> float | None:
    match = re.fullmatch(
        r"\s*([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(mm|cm|m|in|ft|yd)?\s*",
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    suffix = (match.group(2) or "").lower()
    factor = DIMENSION_UNIT_FACTORS.get(suffix) if suffix else unit_to_mm
    return number * factor if factor is not None else None


def _dimension_axis(entity: Any, kind: int, p1: Any, p2: Any) -> str:
    if kind == 0:
        angle = float(entity.dxf.get("angle", 0.0)) % 180
        if min(angle, 180 - angle) <= 1:
            return "x"
        if abs(angle - 90) <= 1:
            return "y"
        return "diagonal"
    dx = float(p2.x) - float(p1.x)
    dy = float(p2.y) - float(p1.y)
    if abs(dy) <= max(abs(dx), 1.0) * 0.01:
        return "x"
    if abs(dx) <= max(abs(dy), 1.0) * 0.01:
        return "y"
    return "diagonal"


def revit_dwg_dimensions(path: str) -> dict[str, Any]:
    """Extract bounded linear/aligned DIMENSION evidence and verify numeric overrides."""
    document = read_document(path)
    entities = list(document.modelspace())
    if len(entities) > MAX_ENTITIES:
        raise ValueError(f"圖紙圖元數量超過上限（{MAX_ENTITIES}）")

    unit_code = document.header.get("$INSUNITS", 0)
    try:
        unit_code = int(unit_code)
        unit_name = ezdxf.units.unit_name(unit_code)
        unit_to_mm = float(ezdxf.units.conversion_factor(unit_code, ezdxf.units.MM))
    except Exception:
        unit_code = 0
        unit_name = "未指定"
        unit_to_mm = None

    dimensions: list[dict[str, Any]] = []
    total_dimension_count = 0
    for index, entity in enumerate(entities):
        if entity.dxftype() != "DIMENSION":
            continue
        kind = int(entity.dxf.get("dimtype", 0)) & 7
        if kind not in (0, 1):
            continue
        try:
            measured = float(entity.get_measurement())
            p1 = entity.dxf.defpoint2
            p2 = entity.dxf.defpoint3
            if (
                not math.isfinite(measured)
                or measured <= 0
                or measured > 1_000_000_000
                or not all(math.isfinite(float(value)) for value in (p1.x, p1.y, p2.x, p2.y))
            ):
                continue
            measured_mm = measured * unit_to_mm if unit_to_mm is not None else None
            if measured_mm is not None and (not math.isfinite(measured_mm) or measured_mm > 100_000_000):
                continue
            raw_override = str(entity.dxf.get("text", "") or "").strip()
            has_override = bool(raw_override and raw_override != "<>")
            override = _dimension_text_override(entity)
            if not has_override:
                consistent = True
            elif override is None:
                consistent = None
            else:
                override_mm = _dimension_override_mm(override, unit_to_mm)
                if override_mm is None or measured_mm is None:
                    consistent = None
                else:
                    tolerance = max(0.5, abs(measured_mm) * 0.005)
                    consistent = abs(override_mm - measured_mm) <= tolerance
            total_dimension_count += 1
            if len(dimensions) >= MAX_REVIT_DIMENSION_ENTITIES:
                continue
            dimensions.append({
                "id": index,
                "kind": "linear" if kind == 0 else "aligned",
                "axis": _dimension_axis(entity, kind, p1, p2),
                "measurementDrawingUnits": round(measured, 6),
                "measurementMm": round(measured_mm, 3) if measured_mm is not None else None,
                "textOverride": override,
                "consistentWithGeometry": consistent,
                "p1": [round(float(p1.x), 6), round(float(p1.y), 6)],
                "p2": [round(float(p2.x), 6), round(float(p2.y), 6)],
            })
        except Exception:
            # One malformed dimension must not discard usable drawing evidence.
            continue

    return {
        "unitCode": unit_code,
        "unitName": unit_name,
        "unitToMm": unit_to_mm,
        "dimensions": dimensions,
        "totalDimensionCount": total_dimension_count,
        "truncated": total_dimension_count > len(dimensions),
        "geometryReview": measure_orthographic_box(entities, unit_to_mm),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("inspect", "inspect-fast", "validate", "validate-fast", "apply", "revit-preview", "revit-dimensions", "fitting-review"))
    parser.add_argument("path")
    parser.add_argument("--output")
    parser.add_argument("--operations-json", default="[]")
    args = parser.parse_args()
    if args.mode in {"inspect", "inspect-fast"}:
        print(json.dumps(inspect(args.path, fast_only=args.mode == "inspect-fast"), ensure_ascii=False, separators=(",", ":")))
        return
    if args.mode in {"validate", "validate-fast"}:
        print(json.dumps(validate(args.path, fast_only=args.mode == "validate-fast"), ensure_ascii=False, separators=(",", ":")))
        return
    if args.mode == "revit-preview":
        print(json.dumps(revit_preview(args.path), ensure_ascii=False, separators=(",", ":")))
        return
    if args.mode == "revit-dimensions":
        print(json.dumps(revit_dwg_dimensions(args.path), ensure_ascii=False, separators=(",", ":")))
        return
    if args.mode == "fitting-review":
        document = read_document(args.path)
        entities = list(document.modelspace())
        if len(entities) > MAX_ENTITIES:
            raise ValueError(f"圖紙圖元數量超過上限（{MAX_ENTITIES}）")
        print(json.dumps(
            {"candidates": fitting_candidates(document, entities)},
            ensure_ascii=False,
            separators=(",", ":"),
        ))
        return
    if not args.output:
        raise ValueError("--output required")
    operations = json.loads(args.operations_json)
    print(json.dumps(apply(args.path, args.output, operations), ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    import resource
    import sys
    import time
    from ezdxf.acc import USE_C_EXT

    started_at = time.monotonic()
    try:
        main()
    finally:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # Keep diagnostics off the JSON result. CPU vs wall time distinguishes
        # slow CAD work from time spent without CPU in the deployed runtime.
        print("CAD_RUNTIME " + json.dumps({
            "mode": sys.argv[1] if len(sys.argv) > 1 else "",
            "wallMs": round((time.monotonic() - started_at) * 1000),
            "cpuMs": round((usage.ru_utime + usage.ru_stime) * 1000),
            "cExtensions": USE_C_EXT,
        }), file=sys.stderr, flush=True)