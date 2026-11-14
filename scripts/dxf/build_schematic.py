"""Extract a floor-to-floor main-pipe graph from DWG-converted DXFs or vector PDFs.

The output is deliberately structured JSON plus a deterministic SVG. CAD vectors,
layers and blocks are the source of truth; no image model is used to invent routes.
"""

import argparse
import base64
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from cad_floor_regions import attach_title_frames, candidate_review_svg, contains, plan_title_floor, rectangular_frame

try:
    import ezdxf
except ImportError:
    ezdxf = None

try:
    from ezdxf import bbox as ezdxf_bbox
    from ezdxf import recover as ezdxf_recover
    from ezdxf.math import BoundingBox2d
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing import config as ezdxf_drawing_config
    from ezdxf.addons.drawing import layout as ezdxf_drawing_layout
    from ezdxf.addons.drawing import svg as ezdxf_svg
except ImportError:
    ezdxf_bbox = None
    ezdxf_recover = None
    BoundingBox2d = None
    Frontend = None
    RenderContext = None
    ezdxf_drawing_config = None
    ezdxf_drawing_layout = None
    ezdxf_svg = None

try:
    import pymupdf
except ImportError:
    pymupdf = None


DEFAULT_PIPE_PATTERN = r"(TG-PIPE|PIPE|GAS|WATER|FIRE|DRAIN|RISER|喉|煤氣|給水|消防|排水)"
DEFAULT_FITTING_PATTERN = r"(VALVE|GV|TEE|ELBOW|BEND|FLANGE|FITTING|RBOX|TRJW|FILTER|METER|REGULATOR|閥|彎|三通|法蘭|管件|錶)"
PIPE_HIGHLIGHT_COLORS = (
    (0.459, 0.733, 0.247), (0.961, 0.925, 0.008),
    (0.996, 0.667, 0.008), (0.0, 0.631, 0.847),
)
ANNOTATION_RED = (0.882, 0.145, 0.004)
FLOOR_LABEL_PATTERN = re.compile(
    r"(?<![A-Z0-9])("
    r"(?:B(?:ASEMENT)?\s*\d*|G(?:ROUND)?|GF|LG|UG|RF|ROOF|\d{1,3})"
    r"\s*(?:[/\-]\s*F|F|FL|FLOOR|樓(?:層)?)"
    r"|(?:B(?:ASEMENT)?\s*\d*|G(?:ROUND)?|GF|LG|UG|RF|ROOF)\s*/\s*F"
    r")(?![A-Z0-9])",
    re.IGNORECASE,
)


def _pdf_color(value):
    try:
        return tuple(round(float(component), 3) for component in value[:3])
    except (TypeError, ValueError, IndexError):
        return None


def _is_pipe_highlight(value):
    color = _pdf_color(value)
    return color is not None and any(
        sum((color[i] - expected[i]) ** 2 for i in range(3)) <= 0.0025
        for expected in PIPE_HIGHLIGHT_COLORS
    )


def _filled_route_centerline(item):
    """Return a centerline for a filled PDF rectangle/quad route band."""
    kind = item[0] if item else ""
    if kind == "re":
        rect = item[1]
        x0, y0, x1, y1 = float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)
        if abs(x1 - x0) >= abs(y1 - y0):
            return [[x0, (y0 + y1) / 2], [x1, (y0 + y1) / 2]]
        return [[(x0 + x1) / 2, y0], [(x0 + x1) / 2, y1]]
    if kind == "qu":
        points = item[1]
        if not points:
            return None
        xs, ys = [float(point.x) for point in points], [float(point.y) for point in points]
        if max(xs) - min(xs) >= max(ys) - min(ys):
            return [[min(xs), sum(ys) / len(ys)], [max(xs), sum(ys) / len(ys)]]
        return [[sum(xs) / len(xs), min(ys)], [sum(xs) / len(xs), max(ys)]]
    return None


def _pdf_highlight_skeleton(page, scale=0.7):
    """Extract bounded centerline chains from the known highlight colours.

    This intentionally uses only PyMuPDF's pixmap and lists/sets.  A half
    resolution mask is sufficient for plan highlights and keeps memory bounded
    for unusually large sheets.
    """
    mask = set()
    rois = []
    for drawing in page.get_drawings():
        if not _is_pipe_highlight(drawing.get("fill")):
            continue
        rect = drawing.get("rect")
        if rect and rect.width > 0 and rect.height > 0:
            rois.append(pymupdf.Rect(rect.x0 - 3, rect.y0 - 3, rect.x1 + 3, rect.y1 + 3) & page.rect)
    # Filled bands are often only a few points wide.  Rasterize their clips
    # at higher resolution rather than losing them in a full-page thumbnail.
    for roi in rois[:12000]:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                              colorspace=pymupdf.csRGB, clip=roi)
        raw = pix.samples
        for y in range(pix.height):
            for x in range(pix.width):
                at = (y * pix.width + x) * 3
                rgb = tuple(raw[at + i] / 255.0 for i in range(3))
                if any(sum((rgb[i] - colour[i]) ** 2 for i in range(3)) <= 0.10
                       for colour in PIPE_HIGHLIGHT_COLORS):
                    mask.add((round((roi.x0 * scale) + x),
                              round((roi.y0 * scale) + y)))
    if not rois:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB)
        raw = pix.samples
        for y in range(pix.height):
            for x in range(pix.width):
                at = (y * pix.width + x) * 3
                rgb = tuple(raw[at + i] / 255.0 for i in range(3))
                if any(sum((rgb[i] - colour[i]) ** 2 for i in range(3)) <= 0.10
                       for colour in PIPE_HIGHLIGHT_COLORS):
                    mask.add((x, y))
    if not mask:
        return [], 0, set()
    # Close antialias holes and narrow dark centreline gaps before thinning.
    for _ in range(3):
        expanded = set(mask)
        for x, y in mask:
            expanded.update((x + dx, y + dy) for dx, dy in
                            ((1, 0), (-1, 0), (0, 1), (0, -1)))
        mask = {point for point in expanded if all(
            (point[0] + dx, point[1] + dy) in expanded
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
        )}
    components, remaining = [], set(mask)
    while remaining and len(components) < 128:
        seed = remaining.pop()
        queue, component = [seed], {seed}
        while queue and len(component) < 200000:
            x, y = queue.pop()
            for neighbour in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if neighbour in remaining:
                    remaining.remove(neighbour); component.add(neighbour); queue.append(neighbour)
        # Small coloured glyphs/legends are annotation, not route evidence.
        if len(component) >= 40:
            components.append(component)
    skeleton = set()
    for component in components:
        # Full two-pass Zhang-Suen thinning. A single pass leaves wide
        # highlighted bends as parallel rails and creates false branch nodes.
        current = set(component)
        for _ in range(128):
            changed = False
            for second_pass in (False, True):
                remove = set()
                for x, y in current:
                    neighbours = [(x + dx, y + dy) in current for dx, dy in
                                  ((0, -1), (1, -1), (1, 0), (1, 1),
                                   (0, 1), (-1, 1), (-1, 0), (-1, -1))]
                    transitions = sum(
                        not neighbours[i] and neighbours[(i + 1) % 8]
                        for i in range(8)
                    )
                    count = sum(neighbours)
                    if not (2 <= count <= 6 and transitions == 1):
                        continue
                    north, east, south, west = (
                        neighbours[0], neighbours[2], neighbours[4], neighbours[6]
                    )
                    if second_pass:
                        keep = (north and east and west) or (north and south and west)
                    else:
                        keep = (north and east and south) or (east and south and west)
                    if not keep:
                        remove.add((x, y))
                if remove:
                    current -= remove
                    changed = True
            if not changed:
                break
        skeleton.update(current)
    neighbours = lambda point: [other for other in (
        (point[0] - 1, point[1]), (point[0] + 1, point[1]),
        (point[0], point[1] - 1), (point[0], point[1] + 1),
        (point[0] - 1, point[1] - 1), (point[0] + 1, point[1] - 1),
        (point[0] - 1, point[1] + 1), (point[0] + 1, point[1] + 1))
        if other in skeleton]
    keys = {point for point in skeleton if len(neighbours(point)) != 2}
    chains, visited = [], set()
    for start in keys:
        for first in neighbours(start):
            edge = frozenset((start, first))
            if edge in visited:
                continue
            chain, previous, current = [start], start, first
            while True:
                visited.add(frozenset((previous, current))); chain.append(current)
                if current in keys and current != start:
                    break
                options = [p for p in neighbours(current) if p != previous]
                if not options:
                    break
                previous, current = current, options[0]
            if len(chain) > 1:
                chains.append(_simplify_chain(
                    [[point[0] / scale, point[1] / scale] for point in chain],
                    tolerance=15.0 / scale,
                ))
    return chains, len(components), mask


def _simplify_chain(points, tolerance=2.0):
    if len(points) <= 2:
        return points
    first, last = points[0], points[-1]
    dx, dy = last[0] - first[0], last[1] - first[1]
    denominator = math.hypot(dx, dy) or 1.0
    distances = [abs(dy * point[0] - dx * point[1] + last[0] * first[1] -
                     last[1] * first[0]) / denominator for point in points[1:-1]]
    maximum = max(distances, default=0.0)
    if maximum <= tolerance:
        return [first, last]
    index = distances.index(maximum) + 1
    return (_simplify_chain(points[:index + 1], tolerance)[:-1] +
            _simplify_chain(points[index:], tolerance))


def layer_matches(layer, configured, pattern):
    upper = layer.upper()
    if configured:
        return any(upper == item.upper() or upper.startswith(item.upper()) for item in configured)
    return bool(re.search(pattern, layer, re.IGNORECASE))


def pdf_pipe_layer_priority(layer, configured):
    # Some AutoCAD-exported PDFs preserve OCG names even though they are no
    # longer CAD files.  Treat the explicit Towngas pipe layer as authoritative
    # instead of ranking thousands of architectural vectors against it.
    if re.search(r"\bTG[-_ ]?PIPE\b", layer, re.IGNORECASE):
        return 30
    return 1 if layer != "PDF-vector" and layer_matches(
        layer, configured, DEFAULT_PIPE_PATTERN
    ) else 0


def number_from_layer(layer):
    match = re.search(r"\bDN\s*([0-9]+(?:\.[0-9]+)?)\b", layer, re.IGNORECASE)
    return float(match.group(1)) if match else None


def floor_name(filename):
    stem = re.sub(r"\.(dwg|dxf|pdf)$", "", filename, flags=re.IGNORECASE)
    upper = stem.upper().replace(" ", "")
    if re.search(r"(^|[-_])B(?:ASEMENT)?\d*", upper):
        match = re.search(r"B(?:ASEMENT)?(\d*)", upper)
        return "B" + (match.group(1) or "1")
    if "GROUND" in upper or re.search(r"(^|[-_])GF(?:$|[-_])", upper):
        return "G/F"
    if "ROOF" in upper or "天台" in stem:
        return "Roof"
    match = re.search(r"(?:^|[-_])(?:L|F|LEVEL)?(\d{1,3})(?:F|樓)?(?:$|[-_])", upper)
    if match:
        return f"{int(match.group(1))}/F"
    return stem or "未命名樓層"


def normalize_floor_label(value):
    """Normalize a floor title found inside a combined CAD modelspace."""
    raw = re.sub(r"\s+", "", str(value or "")).upper()
    raw = raw.replace("／", "/").replace("－", "-")
    raw = raw.replace("BASEMENT", "B").replace("GROUND", "G")
    if raw in ("ROOF", "RF", "R/F", "天台"):
        return "Roof"
    if raw in ("G", "GF", "G/F", "G-F", "GF/F", "GROUND/F"):
        return "G/F"
    basement = re.fullmatch(r"B(\d*)/?F?", raw)
    if basement:
        return f"B{basement.group(1) or '1'}"
    match = re.fullmatch(
        r"(\d{1,3})/?F|(\d{1,3})FL|(\d{1,3})FLOOR|(\d{1,3})樓層?",
        raw,
    )
    if match:
        number = next(group for group in match.groups() if group is not None)
        return f"{int(number)}/F"
    return None


def floor_label_from_text(value):
    """Return one concise floor marker, ignoring ranges and title prose."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or len(text) > 80:
        return None
    if re.search(r"\b(?:TO|至|～|~)\b", text, re.IGNORECASE):
        return None
    matches = list(FLOOR_LABEL_PATTERN.finditer(text))
    if len(matches) != 1:
        return None
    return normalize_floor_label(matches[0].group(1))


def floor_sort_key(name):
    if name.upper().startswith("B"):
        match = re.search(r"\d+", name)
        return (0, -(int(match.group()) if match else 1))
    if name.upper() in ("G/F", "GF", "GROUND"):
        return (1, 0)
    if name.lower() == "roof":
        return (3, 999)
    match = re.search(r"\d+", name)
    return (2, int(match.group()) if match else 500)


def point_tuple(point):
    return [round(float(point[0]), 3), round(float(point[1]), 3)]


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def polyline_length(points):
    return sum(distance(points[i], points[i + 1]) for i in range(len(points) - 1))


def text_value(entity):
    try:
        if entity.dxftype() == "MTEXT":
            return entity.text.replace("\\P", " ").strip()
        return entity.dxf.text.strip()
    except Exception:
        return ""


def select_pdf_vectors(candidates, rect):
    """Select a bounded, evidence-based subset of PDF vectors.

    A PDF has no dependable CAD layer in many exporters.  In that case a
    length-sorted top-N list is actively dangerous (it usually selects walls
    and borders).  Prefer named pipe layers, then styles which participate in
    a connected network; isolated decorative vectors are not evidence.
    """
    if not candidates:
        return []
    named = [c for c in candidates if c["layerPriority"] > 0]
    pool = named or candidates
    widths = sorted(c["width"] for c in pool if c["width"] > 0)
    typical = widths[len(widths) // 2] if widths else 0.0
    # Endpoint connectivity is a useful style signal, even when the PDF
    # exporter discarded layer names.  Keep the calculation bounded.
    # PDF drawing dictionaries expose color/dash differently by exporter.
    # Preserve both in a stable style key and score styles as groups.
    for candidate in pool:
        color = candidate.get("color", candidate.get("stroke", ""))
        dash = candidate.get("dashes", candidate.get("dash", ""))
        candidate["styleKey"] = f"{candidate['layer']}|{color}|{dash}|{candidate['width']:.2f}"
    style_counts = defaultdict(int)
    for candidate in pool:
        style_counts[candidate["styleKey"]] += 1
    counts = defaultdict(int)
    for candidate in pool[:20000]:
        for point in candidate["points"]:
            key = (round(point[0], 1), round(point[1], 1))
            counts[key] += 1
    for candidate in pool:
        connected = sum(
            counts[(round(point[0], 1), round(point[1], 1))] > 1
            for point in candidate["points"]
        )
        width_score = 1.0 if typical and abs(candidate["width"] - typical) <= max(0.2, typical * 0.35) else 0.0
        candidate["_evidence"] = (
            candidate["layerPriority"] * 5
            + connected * 2
            + width_score
            + min(style_counts[candidate["styleKey"]] / 20.0, 2.0)
            + min(candidate["length"] / 1000.0, 1.0)
        )
    pool = sorted(pool, key=lambda c: c["_evidence"], reverse=True)
    # A named layer is strong evidence; otherwise require network participation
    # and retain only the best style-supported vectors.
    if not named:
        pool = [c for c in pool if c["_evidence"] >= 2.0]
    pool = pool[:5000]
    for candidate in pool:
        candidate.pop("_evidence", None)
    return pool


def parse_cad_file(item, pipe_layers, fitting_layers):
    path = item["path"]
    filename = item["name"]
    if ezdxf is None:
        raise RuntimeError("ezdxf 未安裝，無法建立 CAD schematic")
    doc = ezdxf.readfile(path)
    configured_pipes = []
    automatic_pipes = []
    configured_fittings = []
    automatic_fittings = []
    all_markers = []
    floor_labels = []
    plan_titles = []
    plan_frames = []

    def collect_label(entity):
        value = entity.plain_text() if entity.dxftype() == "MTEXT" else text_value(entity)
        insert = getattr(entity.dxf, "insert", None)
        if insert is None:
            return
        position = point_tuple(insert)
        title_floor = plan_title_floor(value)
        if title_floor:
            plan_titles.append({
                "name": title_floor, "text": value[:160], "position": position,
                "height": float(entity.dxf.get(
                    "char_height" if entity.dxftype() == "MTEXT" else "height", 0)),
            })
        floor = floor_label_from_text(value)
        if floor:
            floor_labels.append({"name": floor, "text": value[:100], "position": position})
        if not floor and not title_floor and re.search(
            r"(RISER|R\/S|T\/A|UP|DOWN|INLET|MAIN|PIPE|LIFT|喉|入口|立管|升降機|電梯|上|下)",
            value, re.IGNORECASE,
        ):
            all_markers.append({"text": value[:100], "position": position})

    for entity in doc.modelspace():
        try:
            kind = entity.dxftype()
            layer = entity.dxf.layer
            frame = rectangular_frame(entity)
            if frame:
                plan_frames.append(frame)
            if kind == "LINE" and layer_matches(layer, [], DEFAULT_PIPE_PATTERN):
                start = point_tuple(entity.dxf.start)
                end = point_tuple(entity.dxf.end)
                if distance(start, end) > 0.01:
                    pipe = {
                        "layer": layer,
                        "diameterMm": number_from_layer(layer),
                        "points": [start, end],
                        "length": round(distance(start, end), 3),
                        "sourceType": "LINE",
                    }
                    automatic_pipes.append(pipe)
                    if layer_matches(layer, pipe_layers, DEFAULT_PIPE_PATTERN):
                        configured_pipes.append(pipe)
            elif kind == "LWPOLYLINE" and layer_matches(layer, [], DEFAULT_PIPE_PATTERN):
                points = [point_tuple(point) for point in entity.get_points("xy")]
                if len(points) >= 2 and polyline_length(points) > 0.01:
                    pipe = {
                        "layer": layer,
                        "diameterMm": number_from_layer(layer),
                        "points": points,
                        "length": round(polyline_length(points), 3),
                        "sourceType": "LWPOLYLINE",
                    }
                    automatic_pipes.append(pipe)
                    if layer_matches(layer, pipe_layers, DEFAULT_PIPE_PATTERN):
                        configured_pipes.append(pipe)
            elif kind == "INSERT":
                position = point_tuple(entity.dxf.insert)
                fitting = {
                    "layer": layer,
                    "block": getattr(entity.dxf, "name", ""),
                    "position": position,
                    "rotation": round(float(getattr(entity.dxf, "rotation", 0.0)), 2),
                }
                if layer_matches(layer, [], DEFAULT_FITTING_PATTERN):
                    automatic_fittings.append(fitting)
                    if layer_matches(layer, fitting_layers, DEFAULT_FITTING_PATTERN):
                        configured_fittings.append(fitting)
                # Floor titles are frequently stored as ATTRIB values inside
                # a title-block INSERT rather than as standalone TEXT.
                for attribute in getattr(entity, "attribs", []) or []:
                    collect_label(attribute)
            elif kind in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF"):
                collect_label(entity)
        except Exception:
            continue

    # Prefer the user's configured layers. If they produce no CAD vectors,
    # retain automatically recognized pipe/fitting layers instead of silently
    # turning a combined drawing into an empty single floor.
    pipes = configured_pipes or automatic_pipes
    fittings = configured_fittings or automatic_fittings
    auto_layer_fallback = not configured_pipes and bool(automatic_pipes)

    # Repeated title-block labels at nearly the same location are one label.
    # Separate labels with the same floor number remain separate regions.
    labels = []
    for label in plan_titles or floor_labels:
        if any(
            existing["name"] == label["name"]
            and distance(existing["position"], label["position"]) <= 150
            for existing in labels
        ):
            continue
        labels.append(label)
    if plan_titles:
        attach_title_frames(labels, plan_frames)
    framed = any("bounds" in label for label in labels)
    full_preview_safe = os.path.getsize(path) <= 30 * 1024 * 1024 and len(doc.modelspace()) <= 8000
    geometry_cache = ezdxf_bbox.Cache() if ezdxf_bbox is not None and full_preview_safe else None

    def nearest_label(position):
        if framed:
            matches = [index for index, label in enumerate(labels)
                       if "bounds" in label and contains(label["bounds"], position)]
            if len(matches) != 1:
                return None
            return matches[0]
        return min(
            range(len(labels)),
            key=lambda index: distance(position, labels[index]["position"]),
        )

    def pipe_center(pipe):
        points = pipe.get("points", [])
        if not points:
            return [0.0, 0.0]
        return [
            sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points),
        ]

    def make_floor(index, name, label_position=None):
        own_frame = bool(labels and "bounds" in labels[index])
        floor_pipes = [
            dict(pipe)
            for pipe in pipes
            if not labels or (
                nearest_label(pipe_center(pipe)) == index
                and (not framed or all(contains(labels[index]["bounds"], point)
                                       for point in pipe.get("points", [])))
            )
        ]
        floor_fittings = [
            dict(fitting)
            for fitting in fittings
            if not labels or nearest_label(fitting["position"]) == index
        ]
        floor_markers = [
            {"floor": name, "text": marker["text"], "position": marker["position"]}
            for marker in all_markers
            if not labels or nearest_label(marker["position"]) == index
        ]
        for pipe_index, pipe in enumerate(floor_pipes, 1):
            pipe["id"] = f"{name}-P{pipe_index:04d}"
            pipe["floor"] = name
        for fitting_index, fitting in enumerate(floor_fittings, 1):
            fitting["id"] = f"{name}-F{fitting_index:04d}"
            fitting["floor"] = name
        warnings = []
        if auto_layer_fallback:
            warnings.append("沒有設定 Layer 找到線段；已按管道 Layer 名稱模式自動識別，請覆核。")
        if label_position is not None:
            warnings.append("此樓層按 CAD 平面圖標題及圖框分區。" if own_frame
                            else "此樓層只按樓層文字的最近位置分區，缺少可靠圖框，請覆核。")
        crossing_count = sum(
            1 for pipe in pipes if framed and nearest_label(pipe_center(pipe)) == index
            and not all(contains(labels[index]["bounds"], point) for point in pipe["points"])
        )
        if crossing_count:
            warnings.append(f"{crossing_count} 段管線跨出圖框，未納入；請覆核圖框及管線。")
        return {
            "_cadDocument": doc if full_preview_safe else None,
            "_cadBboxCache": geometry_cache,
            "_cadPreviewLimited": not full_preview_safe,
            "name": filename,
            "floor": name,
            "pipes": floor_pipes,
            "fittings": floor_fittings,
            "markers": floor_markers,
            "sourceFormat": "CAD",
            "sourceSheet": "combined-modelspace" if labels else "single-modelspace",
            "floorDetection": {
                "method": ("cad-plan-title-frame" if own_frame
                           else "cad-plan-title-unframed" if framed
                           else "cad-floor-label-nearest-region" if label_position is not None else "filename"),
                "label": name if label_position is not None else None,
                "labelPosition": label_position,
                "detectedLabels": [label["name"] for label in labels],
                "bounds": labels[index].get("bounds") if labels else None,
            },
            "sourceReviewRequired": bool(crossing_count or (labels and not own_frame)),
            "warnings": warnings,
        }

    if len(labels) >= 2:
        seen_names = defaultdict(int)
        parsed = []
        for index, label in enumerate(labels):
            seen_names[label["name"]] += 1
            suffix = f" ({seen_names[label['name']]})" if seen_names[label["name"]] > 1 else ""
            parsed.append(make_floor(index, f"{label['name']}{suffix}", label["position"]))
        return parsed

    floor = labels[0]["name"] if labels else floor_name(filename)
    return [make_floor(0, floor, labels[0]["position"] if labels else None)]


def pdf_text_blocks(page):
    blocks = []
    try:
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            text = " ".join(
                span.get("text", "")
                for line in block.get("lines", [])
                for span in line.get("spans", [])
            ).strip()
            if text:
                blocks.append({"text": text, "bbox": block.get("bbox", [0, 0, 0, 0])})
    except Exception:
        pass
    return blocks


def pdf_ocr_text_blocks(page, floor, scale=1.35):
    """Bounded OCR fallback for image-only PDF pages.

    Native PDF text remains the source of truth whenever available. OCR is
    limited to one low-resolution page pass and only feeds engineering-marker
    detection; it never invents pipe geometry.
    """
    if shutil.which("tesseract") is None:
        return []
    try:
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        languages = "eng"
        language_probe = subprocess.run(
            ["tesseract", "--list-langs"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
        available = set(language_probe.stdout.split())
        if "chi_tra" in available:
            languages = "eng+chi_tra"
        result = subprocess.run(
            ["tesseract", "stdin", "stdout", "--psm", "11", "tsv", "-l", languages],
            input=pixmap.tobytes("png"),
            check=False,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []
        groups = {}
        for line in result.stdout.decode("utf-8", errors="ignore").splitlines()[1:]:
            columns = line.split("\t")
            if len(columns) < 12 or not columns[11].strip():
                continue
            try:
                confidence = float(columns[10])
                if confidence < 25:
                    continue
                key = tuple(columns[:4])
                x, y, width, height = (float(columns[i]) / scale for i in (6, 7, 8, 9))
            except (TypeError, ValueError):
                continue
            group = groups.setdefault(key, {"words": [], "bbox": [x, y, x + width, y + height]})
            group["words"].append(columns[11].strip())
            group["bbox"] = [
                min(group["bbox"][0], x), min(group["bbox"][1], y),
                max(group["bbox"][2], x + width), max(group["bbox"][3], y + height),
            ]
        return [
            {"text": " ".join(group["words"]), "bbox": group["bbox"], "ocr": True}
            for group in groups.values()
        ]
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def classify_pdf_marker(text):
    """Classify engineering text without relying on PDF colours."""
    upper = re.sub(r"\s+", " ", text.upper()).strip()
    if re.search(r"\b(?:UP|DN|DOWN)\s*\d+\s*R\b", upper):
        return None
    if re.search(r"\b\d+\s*R\s+UP\b", upper):
        return None
    if re.fullmatch(r"(?:UP|DN|DOWN)", upper):
        return None
    if re.search(r"\b(?:RAMP\s+UP|BUILDING\s+LINE\s+ABOVE|PIPE\s+DUCT)\b", upper):
        return None
    if re.search(r"\b(?:SPRINKLER|F\.?\s*S\.?|FIRE\s+SERVICE)\b", upper) and re.search(r"\bINLET\b", upper):
        return None
    if re.search(r"(LIFT(?:\s+LOBBY|\s+SHAFT|\s+CORE)?|升降機|電梯)", upper):
        return "lift"
    if (re.search(r"\bG\.?V\.?\b", upper)
            and re.search(r"\b(?:RISER|U\/G\s+MAIN)\b", upper)):
        return "lead_in"
    if re.search(r"(LEAD[\s-]*IN|INLET|SERVICE\s+ENTRY|FROM\s+(?:STREET|GAS|MAINS?)|ENTRY\s+POINT|入口|入喉)", upper):
        return "lead_in"
    if re.search(r"\bF\/B\b", upper):
        return "down"
    if re.search(r"(\bRISER\b|\bR\/S\b|\bT\/A\b|\bUP(?:WARD)?\b|\bABOVE\b|\bTO\s+(?:UPPER|ABOVE)\b|上喉|上升|向上)", upper):
        return "up"
    if re.search(r"(\bDOWN(?:WARD)?\b|\bBELOW\b|\bTO\s+(?:LOWER|BELOW)\b|落喉|下降|向下)", upper):
        return "down"
    if re.search(r"(\bMAIN\s+PIPE\b|\bGAS\s+PIPE\b|\bPIPE\s+RISER\b|喉管|喉|立管)", upper):
        return "pipe"
    return None


def marker_diameter(text):
    match = re.search(
        r"DN\s*([0-9]+(?:\.[0-9]+)?)(?![A-Z0-9])|"
        r"(?:DIA\.?|Ø|⌀)\s*([0-9]+(?:\.[0-9]+)?)|"
        r"\b([0-9]+(?:\.[0-9]+)?)\s*(?:MM|毫米)\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    if match.group(3) and not re.search(
        r"(?:\bPIPE\b|\bGAS\b|\bG\.?V\.?\b|\bRISER\b|\bT\/A\b|\bF\/B\b|喉|煤氣)",
        text,
        re.IGNORECASE,
    ):
        return None
    value = float(next(group for group in match.groups() if group is not None))
    return value if 0 < value <= 1000 else None


def pdf_engineering_markers(text_blocks, floor):
    markers = []
    for block in text_blocks:
        value = block["text"]
        kind = classify_pdf_marker(value)
        diameter = marker_diameter(value)
        if kind is None and diameter is None:
            continue
        upper = re.sub(r"\s+", " ", value.upper())
        directions = []
        if re.search(r"\bF\/B\b", upper):
            directions.append("down")
        if re.search(r"\bT\/A\b", upper):
            directions.append("up")
        marker_kinds = []
        if kind == "lead_in":
            marker_kinds.append(kind)
        if kind in ("up", "down") and kind not in directions:
            marker_kinds.append(kind)
        marker_kinds.extend(directions)
        if not marker_kinds:
            marker_kinds.append(kind or "diameter")
        marker_kinds = list(dict.fromkeys(marker_kinds))
        bbox = block.get("bbox", [0, 0, 0, 0])
        position = [
            round((float(bbox[0]) + float(bbox[2])) / 2, 3),
            round((float(bbox[1]) + float(bbox[3])) / 2, 3),
        ]
        for marker_kind in marker_kinds:
            markers.append({
                "floor": floor,
                "text": value[:140],
                "position": position,
                "bbox": [round(float(value), 3) for value in bbox],
                "kind": marker_kind,
                "diameterMm": diameter,
            })
    return markers


def _line_distance_to_point(start, end, point):
    projection = _projection(point, start, end)
    return projection[2] if projection else min(distance(start, point), distance(end, point))


def select_colorless_pdf_vectors(candidates, rect, markers, lift_anchor):
    """Select pipe-like PDF vectors when no coloured route exists.

    This is intentionally conservative: a line must participate in a connected
    component and be supported by an engineering text marker or the Lift core.
    It is preferable to return needs_review than to turn walls/dimension lines
    into a main pipe.
    """
    if not candidates:
        return [], {"componentCount": 0, "supportedComponentCount": 0}
    text_markers = [
        marker for marker in markers
        if marker["kind"] in ("lead_in", "up", "down", "pipe")
    ]
    support_points = [marker["position"] for marker in text_markers]
    if lift_anchor:
        support_points.append(lift_anchor)
    if not support_points:
        return [], {"componentCount": 0, "supportedComponentCount": 0}

    lengths = [candidate["length"] for candidate in candidates]
    typical_length = sorted(lengths)[len(lengths) // 2] if lengths else 1.0
    # Avoid sheet borders and title-block rules before building connectivity.
    usable = []
    margin = min(rect.width, rect.height) * 0.025
    for candidate in candidates[:30000]:
        start, end = candidate["points"]
        if (max(start[0], end[0]) < rect.x0 + margin or
                min(start[0], end[0]) > rect.x1 - margin or
                max(start[1], end[1]) < rect.y0 + margin or
                min(start[1], end[1]) > rect.y1 - margin):
            continue
        usable.append(candidate)
    if not usable:
        return [], {"componentCount": 0, "supportedComponentCount": 0}

    tolerance = max(1.5, min(10.0, typical_length * 0.08))
    parent = list(range(len(usable)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    endpoint_buckets = defaultdict(list)
    cell = tolerance * 2
    for index, candidate in enumerate(usable):
        for point in candidate["points"]:
            endpoint_buckets[(math.floor(point[0] / cell), math.floor(point[1] / cell))].append(index)
    for index, candidate in enumerate(usable):
        for point in candidate["points"]:
            gx, gy = math.floor(point[0] / cell), math.floor(point[1] / cell)
            for x in range(gx - 1, gx + 2):
                for y in range(gy - 1, gy + 2):
                    for other in endpoint_buckets.get((x, y), []):
                        if other == index:
                            continue
                        if any(distance(point, other_point) <= tolerance
                               for other_point in usable[other]["points"]):
                            parent[find(other)] = find(index)
    components = defaultdict(list)
    for index in range(len(usable)):
        components[find(index)].append(index)

    selected = []
    supported_count = 0
    support_radius = max(45.0, min(rect.width, rect.height) * 0.045)
    supported_components = []
    for indexes in components.values():
        component = [usable[index] for index in indexes]
        component_points = [point for item in component for point in item["points"]]
        nearest_support = min(
            (_line_distance_to_point(item["points"][0], item["points"][1], point)
             for item in component for point in support_points),
            default=float("inf"),
        )
        marker_hits = sum(
            1 for marker in text_markers
            if min(distance(marker["position"], point) for point in component_points) <= support_radius
        )
        # A connected Lift-adjacent network is useful evidence even when the
        # text exporter dropped the pipe label. Text hits are stronger.
        supported = nearest_support <= support_radius or marker_hits > 0
        if not supported:
            continue
        supported_count += 1
        component_score = (
            marker_hits * 3
            + (2 if lift_anchor and nearest_support <= support_radius else 0)
            + min(sum(item["length"] for item in component) / max(typical_length, 1.0), 12) / 4
        )
        for item in component:
            item["_colorlessEvidence"] = round(component_score, 3)
        supported_components.append((component_score, component))

    supported_components.sort(key=lambda item: item[0], reverse=True)
    # A handful of connected components is safer than importing every black
    # wall/dimension network near the Lift. Keep complete components.
    for _, component in supported_components[:24]:
        selected.extend(component)
    selected = selected[:5000]
    for item in selected:
        item.pop("_colorlessEvidence", None)
        item["sourceType"] = "PDF_TEXT_LIFT_NETWORK"
        item["layerPriority"] = max(item.get("layerPriority", 0), 5)
    return selected, {
        "componentCount": len(components),
        "supportedComponentCount": supported_count,
    }


def pdf_page_floor(filename, page_number, page_count, text):
    stem_upper = re.sub(r"\.PDF$", "", filename.upper())
    explicit_filename_floor = re.search(
        r"(?:^|[-_])(?:B(?:ASEMENT)?\d*F?|G(?:ROUND)?F|GF|(?:L|LEVEL)?\d{1,3}F)(?:$|[-_])",
        stem_upper,
    )
    if explicit_filename_floor:
        return floor_name(filename)
    text_upper = text.upper()
    # Prefer the sheet's actual plan title over incidental mentions in notes
    # (for example "roof" or "Basement 1" references on an upper-floor plan).
    if re.search(r"\bBASEMENT\s*(\d+)\s+PLAN\b", text_upper):
        basement_title = re.search(r"\bBASEMENT\s*(\d+)\s+PLAN\b", text_upper)
        return f"B{int(basement_title.group(1))}"
    if re.search(r"\bGROUND\s+FLOOR\s+PLAN\b", text_upper):
        return "G/F"
    ordinal_floor_words = {
        "FIRST": 1, "SECOND": 2, "THIRD": 3, "FOURTH": 4, "FIFTH": 5,
        "SIXTH": 6, "SEVENTH": 7, "EIGHTH": 8, "NINTH": 9, "TENTH": 10,
        "ELEVENTH": 11, "TWELFTH": 12, "THIRTEENTH": 13, "FOURTEENTH": 14,
        "FIFTEENTH": 15, "SIXTEENTH": 16, "SEVENTEENTH": 17, "EIGHTEENTH": 18,
        "NINETEENTH": 19, "TWENTIETH": 20, "TWENTY-FIRST": 21,
        "TWENTY-SECOND": 22, "TWENTY-THIRD": 23, "TWENTY-FOURTH": 24,
        "TWENTY-FIFTH": 25, "TWENTY-SIXTH": 26, "TWENTY-SEVENTH": 27,
        "TWENTY-EIGHTH": 28, "TWENTY-NINTH": 29, "THIRTIETH": 30,
    }
    for ordinal, level in ordinal_floor_words.items():
        if re.search(rf"\b{ordinal}\s+FLOOR\s+PLAN\b", text_upper):
            return f"{level}/F"
    ordinal_number = re.search(r"\b(\d{1,3})(?:ST|ND|RD|TH)\s+FLOOR\s+PLAN\b", text_upper)
    if ordinal_number:
        return f"{int(ordinal_number.group(1))}/F"
    numeric_title = re.search(r"\b(?:LEVEL\s+)?(\d{1,3})\s*\/?\s*F\s+PLAN\b", text_upper)
    if numeric_title:
        return f"{int(numeric_title.group(1))}/F"
    basement = re.search(r"\bB(?:ASEMENT)?\s*(\d+)\b", text_upper)
    if basement:
        return f"B{basement.group(1)}"
    if re.search(r"\b(?:G\/?F|GROUND\s+FLOOR)\b", text_upper):
        return "G/F"
    if re.search(r"\bROOF\b", text_upper) or "天台" in text:
        return "Roof"
    level = re.search(r"\b(?:LEVEL|L|F)?\s*(\d{1,3})\s*(?:F|\/F|FLOOR|樓)\b", text_upper)
    if level:
        return f"{int(level.group(1))}/F"
    base = floor_name(filename)
    return base if page_count == 1 else f"{base} P{page_number}"


def parse_pdf_file(item, pipe_layers, include_floors=None):
    if pymupdf is None:
        raise RuntimeError("PyMuPDF 未安裝，無法分析 PDF 圖紙")
    path = item["path"]
    filename = item["name"]
    document = pymupdf.open(path)
    if document.page_count < 1:
        document.close()
        raise RuntimeError(f"{filename} 沒有可分析頁面")
    if document.page_count > 50:
        document.close()
        raise RuntimeError(f"{filename} 超過50頁，請分拆後再分析")

    parsed_pages = []
    requested_floors = {
        normalize_floor_label(floor) or str(floor).strip()
        for floor in (include_floors or [])
    }
    try:
        for page_index, page in enumerate(document):
            page_number = page_index + 1
            text_blocks = pdf_text_blocks(page)
            ocr_used = False
            if not text_blocks:
                ocr_blocks = pdf_ocr_text_blocks(page, floor_name(filename))
                if ocr_blocks:
                    text_blocks = ocr_blocks
                    ocr_used = True
            page_text = " ".join(block["text"] for block in text_blocks)
            floor = pdf_page_floor(filename, page_number, document.page_count, page_text)
            canonical_floor = normalize_floor_label(floor) or floor.strip()
            if requested_floors and canonical_floor not in requested_floors:
                continue
            pipes = []
            markers = pdf_engineering_markers(text_blocks, floor)
            lift_markers = [marker["position"] for marker in markers if marker["kind"] == "lift"]
            lift_anchor = (
                [sum(point[0] for point in lift_markers) / len(lift_markers),
                 sum(point[1] for point in lift_markers) / len(lift_markers)]
                if lift_markers else None
            )
            seen_lines = set()
            vector_candidates = []
            core_points = []

            drawings = page.get_drawings()
            explicit_pipe_drawings = [
                drawing for drawing in drawings
                if pdf_pipe_layer_priority(str(drawing.get("layer") or "PDF-vector"), pipe_layers) >= 30
                and any(
                    item and item[0] == "l" and distance(item[1], item[2]) > 1.0
                    for item in drawing.get("items", [])
                )
            ]
            # Named source geometry takes precedence over raster highlights.
            # Do not scan architectural lines or rasterize the whole sheet
            # when the exporter has already identified the pipe layer.
            for drawing in explicit_pipe_drawings or drawings:
                layer = str(drawing.get("layer") or "PDF-vector")
                fill = drawing.get("fill")
                is_highlight = _is_pipe_highlight(fill)
                rect_for_core = drawing.get("rect")
                color_for_core = _pdf_color(drawing.get("color"))
                if (rect_for_core and color_for_core in ((0.0, 0.0, 0.0), (0.4, 0.4, 0.4))
                        and rect_for_core.width < page.rect.width * 0.45
                        and rect_for_core.height < page.rect.height * 0.45):
                    core_points.append([float((rect_for_core.x0 + rect_for_core.x1) / 2),
                                        float((rect_for_core.y0 + rect_for_core.y1) / 2)])
                highlight_added = False
                # Filled highlight bands are the authoritative pipe evidence
                # in tender PDFs.  Red filled annotations are deliberately not
                # admitted, even if their geometry resembles a route.
                for item_data in drawing.get("items", []):
                    if is_highlight and not highlight_added and item_data and item_data[0] in ("l", "re", "qu"):
                        rect = drawing.get("rect")
                        centerline = None
                        if rect and max(float(rect.width), float(rect.height)) > 1.0:
                            if rect.width >= rect.height:
                                centerline = [[float(rect.x0), float(rect.y0 + rect.height / 2)],
                                              [float(rect.x1), float(rect.y0 + rect.height / 2)]]
                            else:
                                centerline = [[float(rect.x0 + rect.width / 2), float(rect.y0)],
                                              [float(rect.x0 + rect.width / 2), float(rect.y1)]]
                        if centerline is None:
                            centerline = _filled_route_centerline(item_data)
                        if centerline and distance(centerline[0], centerline[1]) > 1.0:
                            vector_candidates.append({
                                "layer": layer, "layerPriority": 10,
                                "width": float(drawing.get("width") or 0.0),
                                "color": _pdf_color(fill), "dashes": "",
                                "points": centerline,
                                "length": round(distance(centerline[0], centerline[1]), 3),
                                "sourceType": "PDF_FILLED_PIPE_HIGHLIGHT",
                            })
                            highlight_added = True
                        continue
                    if not item_data or item_data[0] != "l":
                        continue
                    start = point_tuple(item_data[1])
                    end = point_tuple(item_data[2])
                    length = distance(start, end)
                    if length <= 1.0:
                        continue
                    line_key = tuple(sorted((tuple(start), tuple(end))))
                    if line_key in seen_lines:
                        continue
                    seen_lines.add(line_key)
                    named_layer = layer != "PDF-vector"
                    layer_priority = pdf_pipe_layer_priority(layer, pipe_layers)
                    vector_candidates.append({
                        "layer": layer,
                        "layerPriority": layer_priority,
                        "width": float(drawing.get("width") or 0.0),
                        "color": drawing.get("color", drawing.get("colorSpace", "")),
                        "dashes": drawing.get("dashes", drawing.get("dash", "")),
                        "points": [start, end],
                        "length": round(length, 3),
                        "sourceType": "PDF_TG_PIPE_LAYER" if layer_priority >= 30 else "PDF_VECTOR",
                    })

            skeleton_chains, colored_components, colored_mask = (
                ([], 0, set()) if explicit_pipe_drawings
                else _pdf_highlight_skeleton(page)
            )
            line_candidates = [candidate for candidate in vector_candidates
                               if candidate.get("sourceType") == "PDF_VECTOR"
                               and candidate["length"] >= 15]
            covered_lines = []
            for candidate in line_candidates:
                points = candidate["points"]
                samples = max(3, min(24, int(candidate["length"] / 8)))
                covered = 0
                for sample in range(samples + 1):
                    ratio = sample / samples
                    x = points[0][0] + (points[1][0] - points[0][0]) * ratio
                    y = points[0][1] + (points[1][1] - points[0][1]) * ratio
                    gx, gy = round(x * 0.7), round(y * 0.7)
                    if any((gx + dx, gy + dy) in colored_mask
                           for dx in (-2, -1, 0, 1, 2) for dy in (-2, -1, 0, 1, 2)):
                        covered += 1
                if covered / (samples + 1) >= 0.75:
                    candidate["layerPriority"] = 30
                    candidate["sourceType"] = "PDF_PIPE_CENTERLINE"
                    covered_lines.append(candidate)
            if covered_lines and len(covered_lines) >= 8:
                # Dark CAD centerlines are authoritative inside a colored
                # band; this prevents label glyphs and mask holes becoming
                # artificial tees.
                vector_candidates = covered_lines
            elif skeleton_chains:
                vector_candidates = [
                    candidate for candidate in vector_candidates
                    if candidate.get("layerPriority", 0) < 10
                ]
                for chain in skeleton_chains[:12000]:
                    if len(chain) >= 2:
                        vector_candidates.append({
                            "layer": "PDF-highlight-skeleton", "layerPriority": 20,
                            "width": 0.0, "color": "pipe-highlight", "dashes": "",
                            "points": chain, "length": round(polyline_length(chain), 3),
                            "sourceType": "PDF_HIGHLIGHT_SKELETON",
                        })
            colorless_meta = {"componentCount": 0, "supportedComponentCount": 0}
            original_vector_count = len(vector_candidates)
            all_vector_candidates = list(vector_candidates)
            highlight_vectors = [candidate for candidate in vector_candidates
                                 if candidate.get("layerPriority", 0) >= 10]
            # Once colored route evidence exists, black/gray architectural
            # vectors are not allowed to compete with it.
            if highlight_vectors:
                vector_candidates = select_pdf_vectors(highlight_vectors, page.rect)
            else:
                vector_candidates, colorless_meta = select_colorless_pdf_vectors(
                    vector_candidates, page.rect, markers, lift_anchor
                )
                if not vector_candidates:
                    # Keep a bounded, explicitly low-confidence geometry
                    # candidate so the App can show the user what evidence was
                    # found instead of silently claiming that no pipe exists.
                    vector_candidates = select_pdf_vectors(
                        all_vector_candidates,
                        page.rect,
                    )
            for pipe_index, candidate in enumerate(vector_candidates, 1):
                pipes.append({
                    "id": f"{floor}-P{pipe_index:04d}",
                    "floor": floor,
                    "layer": candidate["layer"],
                    "diameterMm": number_from_layer(candidate["layer"]),
                    "points": candidate["points"],
                    "length": candidate["length"],
                    "sourceType": candidate.get("sourceType", "PDF_VECTOR"),
                })

            if not pipes:
                raise RuntimeError(
                    f"{filename} 第{page_number}頁沒有可分析的PDF向量線；"
                    "請確認PDF包含文字層及可提取的管線向量；掃描圖片需要先OCR"
                )
            parsed_pages.append({
                "name": filename if document.page_count == 1 else f"{filename}（第{page_number}頁）",
                "floor": floor,
                "path": path,
                "pipes": pipes,
                "fittings": [],
                "markers": markers,
                "sourceFormat": "PDF",
                "warnings": ([
                    f"{filename} 第{page_number}頁共有{original_vector_count:,}條向量線；"
                    f"已按圖層、線寬及連通性選取{len(pipes):,}條候選，未能確認的線段未作主喉管。"
                ] if original_vector_count > len(pipes) else []) + ([
                    f"已從明確的 TG Pipe 圖層抽取{sum(pipe.get('sourceType') == 'PDF_TG_PIPE_LAYER' for pipe in pipes):,}條候選線段。"
                ] if any(pipe.get("sourceType") == "PDF_TG_PIPE_LAYER" for pipe in pipes) else []) + ([
                    f"已由四種彩色喉管遮罩追蹤{colored_components}個連通色帶組件；"
                    "中心線保留轉彎及分支，紅色標註已排除。"
                ] if skeleton_chains else []) + ([
                    f"此PDF沒有可靠顏色喉管標記；已使用文字、Lift位置及連通線段找出"
                    f"{colorless_meta['supportedComponentCount']}個候選網絡，仍需覆核。"
                ] if not highlight_vectors else []) + ([
                    "此頁沒有PDF文字層，已用低倍率OCR讀取入口、上喉、落喉及Lift文字；OCR只用於文字證據，沒有用來捏造管線。"
                ] if ocr_used else []),
                "pageRect": [0, 0, float(page.rect.width), float(page.rect.height)],
                "corePoints": core_points[:20000],
                "_liftAnchor": point_tuple(lift_anchor) if lift_anchor else None,
            })
    finally:
        document.close()
    return parsed_pages


def parse_input(item, pipe_layers, fitting_layers, include_floors=None):
    if re.search(r"\.pdf$", item["name"], re.IGNORECASE):
        return parse_pdf_file(item, pipe_layers, include_floors)
    parsed = parse_cad_file(item, pipe_layers, fitting_layers)
    for file in parsed:
        file["sourceFormat"] = "CAD"
    return parsed


def filter_schematic_floors(files, include_floors):
    """Keep only explicitly requested floor labels; fail if any are absent."""
    if not include_floors:
        return files
    requested = {
        normalize_floor_label(floor) or str(floor).strip()
        for floor in include_floors
        if str(floor).strip()
    }
    available = {
        normalize_floor_label(file.get("floor", "")) or str(file.get("floor", "")).strip()
        for file in files
    }
    missing = sorted(requested - available, key=floor_sort_key)
    if missing:
        found = ", ".join(sorted(available, key=floor_sort_key)) or "沒有辨識到樓層"
        raise RuntimeError(
            f"指定範圍缺少樓層：{'、'.join(missing)}；目前辨識到：{found}。"
            "請確認 PDF 每頁是一個樓層，或檔案有清楚的樓層標籤。"
        )
    return [
        file for file in files
        if (normalize_floor_label(file.get("floor", "")) or str(file.get("floor", "")).strip()) in requested
    ]


def _projection(point, start, end):
    dx, dy = end[0] - start[0], end[1] - start[1]
    length2 = dx * dx + dy * dy
    if length2 <= 1e-12:
        return None
    t = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length2
    t = max(0.0, min(1.0, t))
    projected = [start[0] + t * dx, start[1] + t * dy]
    return t, projected, distance(point, projected)


def build_floor_network(file):
    """Build a snapped planar graph, including endpoint-to-segment tees."""
    pipes = file.get("pipes", [])
    raw_segments = []
    endpoints = []
    for pipe in pipes:
        points = pipe.get("points", [])
        for index in range(len(points) - 1):
            if distance(points[index], points[index + 1]) > 0.01:
                raw_segments.append((points[index], points[index + 1], pipe["id"]))
                endpoints.extend((points[index], points[index + 1]))
    if not raw_segments:
        return {"nodes": [], "edges": [], "inlet": None, "status": "needs_review",
                "confidence": 0.0, "diagnostics": ["此樓層沒有可建立網絡的線段"]}
    tolerance = max(0.5, min(12.0 if file.get("sourceFormat") == "PDF" else 5.0, max(
        (distance(a, b) for a, b, _ in raw_segments[:1000]), default=1.0
    ) * 0.01))
    cell = tolerance * 2.0
    buckets = defaultdict(list)
    for index, (start, end, _) in enumerate(raw_segments):
        samples = (start, end, [(start[0] + end[0]) / 2, (start[1] + end[1]) / 2])
        for point in samples:
            buckets[(math.floor(point[0] / cell), math.floor(point[1] / cell))].append(index)
    parent = list(range(len(raw_segments)))
    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index
    for (gx, gy), indexes in list(buckets.items()):
        nearby = sum((buckets[(x, y)] for x in (gx - 1, gx, gx + 1)
                      for y in (gy - 1, gy, gy + 1)), [])
        for other in nearby:
            # Bucket proximity is only a candidate; verify endpoint distance.
            base = raw_segments[indexes[0]]
            candidate = raw_segments[other]
            touches = any(distance(a, b) <= tolerance
                          for a in (base[0], base[1])
                          for b in (candidate[0], candidate[1]))
            touches = touches or any(
                (_projection(a, candidate[0], candidate[1]) or (0, None, 999999))[2] <= tolerance
                for a in (base[0], base[1])
            ) or any(
                (_projection(a, base[0], base[1]) or (0, None, 999999))[2] <= tolerance
                for a in (candidate[0], candidate[1])
            )
            if touches:
                parent[find(other)] = find(indexes[0])
    components = defaultdict(list)
    for index in range(len(raw_segments)):
        components[find(index)].append(index)
    ranked = sorted(components.values(), key=lambda group: sum(
        distance(raw_segments[i][0], raw_segments[i][1]) for i in group), reverse=True)
    review_override = file.get("_reviewOverride") or {}
    requested_component = review_override.get("componentIndex")
    component_index = (
        requested_component
        if isinstance(requested_component, int) and 0 <= requested_component < len(ranked)
        else 0
    )
    selected = set(ranked[component_index])
    alternatives = [{"segmentCount": len(group), "length": round(sum(
        distance(raw_segments[i][0], raw_segments[i][1]) for i in group), 3)}
        for group in ranked[:4]]
    raw_segments = [segment for index, segment in enumerate(raw_segments) if index in selected]
    endpoints = [point for segment in raw_segments for point in (segment[0], segment[1])]
    splits = [[(0.0, segment[0]), (1.0, segment[1])] for segment in raw_segments]
    # Endpoint-to-segment snapping identifies tees drawn with a dangling end.
    segment_buckets = defaultdict(list)
    for index, (start, end, _) in enumerate(raw_segments):
        gx = range(math.floor((min(start[0], end[0]) - tolerance) / cell),
                   math.floor((max(start[0], end[0]) + tolerance) / cell) + 1)
        gy = range(math.floor((min(start[1], end[1]) - tolerance) / cell),
                   math.floor((max(start[1], end[1]) + tolerance) / cell) + 1)
        for x in gx:
            for y in gy:
                segment_buckets[(x, y)].append(index)
    for endpoint in endpoints:
        nearby = segment_buckets.get((math.floor(endpoint[0] / cell), math.floor(endpoint[1] / cell)), [])
        for index in nearby:
            start, end, _ = raw_segments[index]
            projection = _projection(endpoint, start, end)
            if projection and tolerance >= projection[2] and 1e-5 < projection[0] < 1 - 1e-5:
                splits[index].append((projection[0], projection[1]))
    node_points = []
    node_buckets = defaultdict(list)
    def node_for(point):
        gx, gy = math.floor(point[0] / cell), math.floor(point[1] / cell)
        nearby = sum((node_buckets[(x, y)] for x in (gx - 1, gx, gx + 1)
                      for y in (gy - 1, gy, gy + 1)), [])
        for index in nearby:
            existing = node_points[index]
            if distance(existing, point) <= tolerance:
                node_points[index] = [(existing[0] + point[0]) / 2, (existing[1] + point[1]) / 2]
                return index
        node_points.append(list(point))
        node_buckets[(gx, gy)].append(len(node_points) - 1)
        return len(node_points) - 1
    edges = []
    seen = set()
    for index, split_points in enumerate(splits):
        split_points.sort(key=lambda item: item[0])
        for first, second in zip(split_points, split_points[1:]):
            if distance(first[1], second[1]) <= 0.01:
                continue
            a, b = node_for(first[1]), node_for(second[1])
            if a == b:
                continue
            # Multiple PDF vectors can describe the same visible centreline.
            # Topology is based on unique node adjacency, not source object IDs.
            key = (min(a, b), max(a, b))
            if key in seen:
                continue
            seen.add(key)
            edges.append({"id": f"{file['floor']}-E{len(edges) + 1:04d}",
                          "from": f"N{a + 1:04d}", "to": f"N{b + 1:04d}",
                          "pipeId": raw_segments[index][2],
                          "length": round(distance(first[1], second[1]), 3)})
    degree = defaultdict(int)
    for edge in edges:
        degree[edge["from"]] += 1
        degree[edge["to"]] += 1
    nodes = [{"id": f"N{index + 1:04d}", "position": point_tuple(point),
              "degree": degree[f"N{index + 1:04d}"]}
             for index, point in enumerate(node_points)]
    inlet, diagnostics = rank_inlet(file, nodes, edges)
    confidence = inlet["confidence"] if inlet else 0.0
    status = "ready" if inlet and confidence >= 0.75 else "needs_review"
    component_confirmed = isinstance(requested_component, int) and component_index == requested_component
    if (not component_confirmed and len(alternatives) > 1
            and alternatives[1]["length"] >= alternatives[0]["length"] * 0.5):
        status = "needs_review"
        confidence = min(confidence, 0.69)
        diagnostics.append("存在接近同等規模的分離線段組件，不能安全判定主喉管")
    if not inlet:
        diagnostics.append("未找到有足夠文字、邊界或連通性證據的主喉管入口")
    elif status == "needs_review":
        diagnostics.append("入口證據含糊，請人工確認；系統沒有猜測固定入口")
    if len(alternatives) > 1 and not component_confirmed:
        diagnostics.append("已排除不相連的平面圖組件；其他組件候選需人工確認")
    endpoint_options = [
        {"nodeId": node["id"], "position": node["position"], "degree": node["degree"]}
        for node in nodes if node.get("degree") == 1
    ]
    flow_markers = []
    for marker in file.get("markers", []):
        if marker.get("kind") not in ("lead_in", "up", "down", "pipe", "diameter"):
            continue
        nearest = min(nodes, key=lambda node: distance(node["position"], marker["position"]))
        flow_markers.append({
            "kind": marker["kind"],
            "text": marker["text"],
            "position": marker["position"],
            "nodeId": nearest["id"],
            "distance": round(distance(nearest["position"], marker["position"]), 3),
            "diameterMm": marker.get("diameterMm"),
        })
    return {"nodes": nodes, "edges": edges, "inlet": inlet, "status": status,
            "confidence": confidence, "diagnostics": diagnostics,
            "componentAlternatives": alternatives,
            "selectedComponentIndex": component_index,
            "endpointOptions": endpoint_options,
            "flowMarkers": flow_markers[:80],
            "liftAnchor": file.get("_liftAnchor"),
            "evidenceMode": "text-lift-network" if file.get("sourceFormat") == "PDF"
            else "cad-layer-network"}


def rank_inlet(file, nodes, edges):
    if not nodes:
        return None, []
    requested_node = (file.get("_reviewOverride") or {}).get("inletNodeId")
    if isinstance(requested_node, str):
        confirmed = next((node for node in nodes if node["id"] == requested_node), None)
        if confirmed:
            return {
                "nodeId": confirmed["id"],
                "position": confirmed["position"],
                "confidence": 1.0,
                "basis": "App內人工確認入口",
            }, []
    xs = [node["position"][0] for node in nodes]
    ys = [node["position"][1] for node in nodes]
    bounds = (min(xs), max(xs), min(ys), max(ys))
    candidates = []
    markers = [marker for marker in file.get("markers", [])
               if marker.get("kind") in ("lead_in", "up", "down", "pipe", "diameter")
               or re.search(r"(RISER|R\/S|T\/A|UP|DOWN|MAIN|PIPE|喉|立管|入口|INLET)", marker["text"], re.I)]
    for node in nodes:
        x, y = node["position"]
        boundary = min(x - bounds[0], bounds[1] - x, y - bounds[2], bounds[3] - y)
        boundary_score = 1.0 if boundary <= max(2.0, (bounds[1] - bounds[0] + bounds[3] - bounds[2]) * 0.03) else 0.0
        marker_scores = [
            (
                1.0 - distance(node["position"], marker["position"]) /
                max(5.0, (bounds[1] - bounds[0] + bounds[3] - bounds[2]) * 0.1),
                marker,
            )
            for marker in markers
        ]
        marker_score, marker = max(marker_scores, default=(0.0, None), key=lambda item: item[0])
        marker_weight = 1.0
        if marker and marker.get("kind") == "lead_in":
            marker_weight = 1.35
        score = min(1.0, 0.55 * max(0.0, marker_score) * marker_weight
                    + 0.25 * boundary_score + 0.2 * min(node["degree"] / 3.0, 1.0))
        candidates.append((score, node, marker_score > 0.5, boundary_score > 0,
                           marker.get("kind") if marker else None))
    candidates.sort(key=lambda item: item[0], reverse=True)
    best = candidates[0]
    if best[0] < 0.35:
        return None, ["主喉管入口沒有明顯邊界或文字標記證據"]
    inlet = {"nodeId": best[1]["id"], "position": best[1]["position"],
             "confidence": round(best[0], 3),
             "basis": ("Lead-in／入口文字及邊界證據" if best[2] and best[4] == "lead_in" else
                       "圖紙文字及邊界證據" if best[2] else
                       "邊界及網絡連通性證據" if best[3] else "網絡連通性證據")}
    diagnostics = []
    if len(candidates) > 1 and best[0] - candidates[1][0] < 0.12:
        diagnostics.append("最高入口候選與次高候選接近，入口需要覆核")
        inlet["confidence"] = min(inlet["confidence"], 0.69)
    return inlet, diagnostics


def attach_networks(files):
    for file in files:
        file["network"] = build_floor_network(file)
        if file.get("sourceReviewRequired"):
            file["network"]["status"] = "needs_review"
            file["network"]["diagnostics"].append("CAD 分區或跨圖框管線尚待覆核。")
    return files


def _lift_anchor(file):
    matches = []
    for marker in file.get("markers", []):
        if re.search(r"(LIFT|LIFT\s*LOBBY|升降機|電梯)", marker["text"], re.I):
            matches.append(marker["position"])
    if not matches:
        rect = file.get("pageRect")
        points = file.get("corePoints", [])
        if points:
            # Robust median of the dense paired-shaft/core vector geometry,
            # rather than the sheet centre.
            xs = sorted(point[0] for point in points)
            ys = sorted(point[1] for point in points)
            return [xs[len(xs) // 2], ys[len(ys) // 2]]
        if rect:
            # The paired lift core is the normalized central service core on
            # these tender sheets; use the page-normalized origin only when
            # no extractable label survives PDF text extraction.
            return [(rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2]
        return None
    return [sum(point[0] for point in matches) / len(matches),
            sum(point[1] for point in matches) / len(matches)]


def register_cross_floor_risers(files):
    """Register sheets on Lift anchors with rotation and page-scale correction."""
    registrations = []
    page_diagonals = {
        id(file): math.hypot(
            max(1.0, (file.get("pageRect") or [0, 0, 1, 1])[2]),
            max(1.0, (file.get("pageRect") or [0, 0, 1, 1])[3]),
        )
        for file in files
    }
    base_file = next(
        (file for file in sorted(files, key=lambda item: floor_sort_key(item["floor"]))
         if file["floor"].upper().startswith("B") and _lift_anchor(file)),
        None,
    )
    base_diagonal = page_diagonals.get(id(base_file), 1.0) if base_file else 1.0

    def registered_point(file, point):
        anchor = file["registration"]["anchor"]
        if not anchor:
            return list(point)
        angle = math.radians(file["registration"]["rotationDegrees"])
        scale = file["registration"]["scale"]
        dx, dy = (point[0] - anchor[0]) * scale, (point[1] - anchor[1]) * scale
        return [
            round(dx * math.cos(angle) - dy * math.sin(angle) + file["registration"]["translation"][0], 3),
            round(dx * math.sin(angle) + dy * math.cos(angle) + file["registration"]["translation"][1], 3),
        ]

    for file in files:
        anchor = _lift_anchor(file)
        # Tender sheets are consistently upright only at basement; the upper
        # plans are printed from the opposite orientation.
        rotate = 0 if file["floor"].upper().startswith("B") else 180
        explicit_anchor = any(re.search(r"(LIFT|LOBBY|升降機|電梯)", marker["text"], re.I)
                              for marker in file.get("markers", []))
        scale = base_diagonal / page_diagonals[id(file)] if base_file else 1.0
        file["registration"] = {"anchor": point_tuple(anchor) if anchor else None,
                                "translation": [0.0, 0.0],
                                "rotationDegrees": rotate,
                                "scale": round(scale, 6),
                                "basis": "Lift文字錨點" if explicit_anchor else "正規化中央Lift核心候選",
                                "status": "registered" if explicit_anchor else "needs_review"}
        if anchor:
            for node in file["network"]["nodes"]:
                node["registeredPosition"] = registered_point(file, node["position"])
        registrations.append({"floor": file["floor"], "anchor": file["registration"]["anchor"],
                              "rotationDegrees": rotate, "status": file["registration"]["status"],
                              "basis": file["registration"]["basis"],
                              "translation": file["registration"]["translation"],
                              "scale": file["registration"]["scale"],
                              "alignmentConfidence": file["registration"].get("alignmentConfidence", 0.0)})
    links, diagnostics = [], []
    ordered = sorted(files, key=lambda item: floor_sort_key(item["floor"]))
    base = next((item for item in ordered if item["floor"].upper().startswith("B")), None)
    if base and base["registration"]["anchor"]:
        for item in ordered:
            # Every sheet is expressed relative to its own Lift anchor, so
            # translation is intentionally zero after rotation and scaling.
            item["registration"]["alignmentConfidence"] = round(
                min(1.0, item["registration"]["scale"]) if item["registration"]["anchor"] else 0.0, 3)
            for node in item["network"]["nodes"]:
                if item["registration"]["anchor"]:
                    node["registeredPosition"] = registered_point(item, node["position"])
            item_registration = next(
                registration for registration in registrations
                if registration["floor"] == item["floor"]
            )
            item_registration["translation"] = item["registration"]["translation"]
            item_registration["alignmentConfidence"] = item["registration"]["alignmentConfidence"]
    for lower, upper in zip(ordered, ordered[1:]):
        if not lower["registration"]["anchor"] or not upper["registration"]["anchor"]:
            diagnostics.append(f"{lower['floor']}↔{upper['floor']}: 找不到Lift/Lift Lobby錨點，未配對立管")
            continue
        def endpoint_clusters(item):
            points = []
            for pipe in item.get("pipes", []):
                if len(pipe.get("points", [])) < 2:
                    continue
                for index in (0, -1):
                    point = pipe["points"][index]
                    other = pipe["points"][1 if index == 0 else -2]
                    relative = registered_point(item, point)
                    other_registered = registered_point(item, other)
                    tangent = [other[0] - point[0], other[1] - point[1]]
                    tangent = [other_registered[0] - relative[0], other_registered[1] - relative[1]]
                    length = math.hypot(*tangent) or 1.0
                    points.append({"position": relative, "tangent": [tangent[0] / length, tangent[1] / length],
                                   "pipeId": pipe["id"], "raw": point})
            clusters = []
            for point in points:
                match = next((cluster for cluster in clusters
                              if distance(cluster["position"], point["position"]) <= 8), None)
                if match:
                    match["points"].append(point)
                    count = len(match["points"])
                    match["position"] = [sum(p["position"][0] for p in match["points"]) / count,
                                         sum(p["position"][1] for p in match["points"]) / count]
                    continue
                clusters.append({"position": point["position"], "points": [point]})
            return clusters
        a, b = endpoint_clusters(lower), endpoint_clusters(upper)
        candidates = []
        for x in a:
            for y in b:
                direction = max(abs(p["tangent"][0] * q["tangent"][0] +
                                    p["tangent"][1] * q["tangent"][1])
                                for p in x["points"] for q in y["points"])
                score = distance(x["position"], y["position"]) + (1.0 - direction) * 30
                candidates.append((score, x, y, direction))
        candidates.sort(key=lambda item: item[0])
        if not candidates:
            diagnostics.append(f"{lower['floor']}↔{upper['floor']}: 沒有端點可供Lift配對")
            continue
        best = candidates[0]
        # Distinct clusters, not repeated vectors, define score separation.
        second = next((candidate[0] for candidate in candidates[1:]
                       if distance(candidate[1]["position"], best[1]["position"]) > 12
                       or distance(candidate[2]["position"], best[2]["position"]) > 12), float("inf"))
        separation = second - best[0]
        if best[0] > 250 or separation < 10 or best[3] < 0.5:
            diagnostics.append(f"{lower['floor']}↔{upper['floor']}: 配對不唯一（距離={best[0]:.1f}，分離={separation:.1f}）")
            continue
        def nearest_node(item, position):
            return min(item["network"]["nodes"], key=lambda node: distance(
                node.get("registeredPosition", node["position"]), position))
        lower_node = nearest_node(lower, best[1]["position"])
        upper_node = nearest_node(upper, best[2]["position"])
        upper["network"]["inlet"] = {"nodeId": upper_node["id"], "position": upper_node["position"],
                                     "confidence": round(min(1.0, separation / 50), 3),
                                     "basis": "Lift配對端點"}
        upper["network"]["status"] = "ready"
        links.append({"fromFloor": lower["floor"], "fromNode": lower_node["id"],
                      "toFloor": upper["floor"], "toNode": upper_node["id"],
                      "fromPosition": best[1]["position"], "toPosition": best[2]["position"],
                      "distance": round(best[0], 3), "scoreSeparation": round(separation, 3),
                      "directionScore": round(best[3], 3),
                      "basis": "Lift/Lift Lobby相對座標"})
    return registrations, links, diagnostics


def endpoint_records(files):
    result = []
    for file in files:
        # PDF page coordinates commonly align even when no physical riser exists.
        # Only CAD endpoints are safe enough for medium-confidence inference.
        if file.get("sourceFormat") == "PDF":
            continue
        for pipe in file["pipes"]:
            for index, point in enumerate((pipe["points"][0], pipe["points"][-1])):
                result.append({
                    "floor": pipe["floor"],
                    "point": point,
                    "pipeId": pipe["id"],
                    "end": "start" if index == 0 else "end",
                })
    return result


def find_risers(files):
    """Find strong candidates from explicit markers or repeated endpoint coordinates."""
    candidates = []
    explicit = []
    for file in files:
        for marker in file["markers"]:
            if re.search(r"(RISER|R\/S|T\/A|立管)", marker["text"], re.IGNORECASE):
                explicit.append(marker)

    explicit_groups = {}
    for marker in explicit:
        identity = re.search(
            r"(RISER|R\/S|T\/A|立管)\s*[:#-]?\s*([A-Z0-9][A-Z0-9._/-]*)?",
            marker["text"],
            re.IGNORECASE,
        )
        marker_type = identity.group(1).upper() if identity else "RISER"
        marker_number = identity.group(2).upper() if identity and identity.group(2) else ""
        if marker_number:
            key = re.sub(r"[^A-Z0-9]", "", f"{marker_type}{marker_number}")
            label = f"{identity.group(1)} {identity.group(2)}"
        else:
            grid_x = round(marker["position"][0] / 250)
            grid_y = round(marker["position"][1] / 250)
            key = f"{marker_type}@{grid_x},{grid_y}"
            label = marker["text"]
        group = explicit_groups.setdefault(key, {
            "label": label,
            "positions": [],
            "floors": set(),
        })
        group["positions"].append(marker["position"])
        group["floors"].add(marker["floor"])

    if explicit_groups:
        for index, group in enumerate(explicit_groups.values(), 1):
            position = [
                sum(point[0] for point in group["positions"]) / len(group["positions"]),
                sum(point[1] for point in group["positions"]) / len(group["positions"]),
            ]
            candidates.append({
                "id": f"R-{index:02d}",
                "label": group["label"],
                "position": [round(position[0], 3), round(position[1], 3)],
                "floors": sorted(group["floors"], key=floor_sort_key),
                "confidence": "high",
                "basis": "圖紙文字標記",
            })

    endpoints = endpoint_records(files)
    clusters = []
    tolerance = 250.0
    for endpoint in endpoints:
        match = next(
            (cluster for cluster in clusters
             if distance(cluster["position"], endpoint["point"]) <= tolerance),
            None,
        )
        if match is None:
            clusters.append({
                "position": endpoint["point"][:],
                "floors": {endpoint["floor"]},
                "pipeIds": [endpoint["pipeId"]],
            })
        else:
            count = len(match["pipeIds"])
            match["position"] = [
                (match["position"][0] * count + endpoint["point"][0]) / (count + 1),
                (match["position"][1] * count + endpoint["point"][1]) / (count + 1),
            ]
            match["floors"].add(endpoint["floor"])
            match["pipeIds"].append(endpoint["pipeId"])

    floor_count = len(files)
    inferred = [
        cluster for cluster in clusters
        if len(cluster["floors"]) >= 2 and len(cluster["floors"]) < floor_count + 1
        and not any(distance(cluster["position"], item["position"]) <= tolerance for item in candidates)
    ]
    for index, cluster in enumerate(inferred, len(candidates) + 1):
        candidates.append({
            "id": f"R-{index:02d}",
            "label": "座標重疊候選",
            "position": [round(cluster["position"][0], 3), round(cluster["position"][1], 3)],
            "floors": sorted(cluster["floors"], key=floor_sort_key),
            "pipeIds": cluster["pipeIds"],
            "confidence": "medium",
            "basis": "相鄰樓層端點座標接近，需人工覆核",
        })
    return candidates


def esc(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def schematic_geometry(file, center, row, span=600.0, vertical=72.0):
    network = file.get("network") or {}
    nodes = network.get("nodes", [])
    if not nodes:
        return [], []
    xs = [node["position"][0] for node in nodes]
    ys = [node["position"][1] for node in nodes]
    lo_x, hi_x = min(xs), max(xs)
    lo_y, hi_y = min(ys), max(ys)
    scale = span / max(hi_x - lo_x, 1.0)
    positions = {}
    for node in nodes:
        x = center + (node["position"][0] - (lo_x + hi_x) / 2) * scale
        y = row - ((node["position"][1] - (lo_y + hi_y) / 2) /
                   max(hi_y - lo_y, 1.0)) * vertical
        positions[node["id"]] = (x, y)
    lines = [(positions[edge["from"]], positions[edge["to"]]) for edge in network.get("edges", [])
             if edge["from"] in positions and edge["to"] in positions]
    degrees = {node["id"]: node.get("degree", 0) for node in nodes}
    return lines, [(position[0], position[1], degrees[node_id])
                   for node_id, position in positions.items()
                   if degrees[node_id] == 1 or degrees[node_id] >= 3]


def build_svg(files, risers, cross_floor_links=None):
    floors = sorted({file["floor"] for file in files}, key=floor_sort_key)
    width = 1100
    row_height = 105
    height = 180 + max(1, len(floors)) * row_height
    # Draw higher floors above lower floors, matching conventional building
    # services riser diagrams and the supplied AutoCAD reference.
    rows = {floor: height - 90 - index * row_height for index, floor in enumerate(floors)}
    riser_x = 520
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="40" y="42" fill="#0f172a" font-family="Arial, sans-serif" font-size="24" font-weight="700">主喉管樓層 Schematic</text>',
        '<text x="40" y="68" fill="#475569" font-family="Arial, sans-serif" font-size="12">垂直主立管圖；由 CAD 圖層、Polyline 及管件圖塊建立，連接關係須覆核</text>',
    ]
    for index, floor in enumerate(floors):
        y = rows[floor]
        file = next(item for item in files if item["floor"] == floor)
        total = sum(pipe["length"] for pipe in file["pipes"])
        parts.append(f'<line x1="80" y1="{y + 38}" x2="1020" y2="{y + 38}" stroke="#cbd5e1" stroke-width="1"/>')
        lines, points = schematic_geometry(file, riser_x, y)
        for start, end in lines:
            parts.append(f'<line x1="{start[0]:.1f}" y1="{start[1]:.1f}" x2="{end[0]:.1f}" y2="{end[1]:.1f}" stroke="#2563eb" stroke-width="6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>')
        for point in points:
            parts.append(f'<circle cx="{point[0]:.1f}" cy="{point[1]:.1f}" r="7" fill="#ffffff" stroke="#2563eb" stroke-width="3"/>')
        label_x = 85 if index % 2 == 0 else 850
        parts.append(f'<text x="{label_x}" y="{y - 16}" fill="#0f172a" font-family="Arial, sans-serif" font-size="16" font-weight="700">{esc(floor)}</text>')
        parts.append(f'<text x="{label_x}" y="{y + 5}" fill="#475569" font-family="Arial, sans-serif" font-size="12">{len(file["pipes"])} 段 · {total:.1f} 圖紙單位 · {len(file["fittings"])} 件管件</text>')
        diameters = sorted({pipe["diameterMm"] for pipe in file["pipes"] if pipe.get("diameterMm")})
        if diameters:
            pipe_label = " / ".join(f"DN{value:g}" for value in diameters[:4])
            parts.append(f'<text x="{riser_x - 160}" y="{y - 26}" fill="#475569" font-family="Arial, sans-serif" font-size="12">{esc(pipe_label)}</text>')
    if floors:
        parts.append(f'<text x="{riser_x + 14}" y="{rows[floors[-1]] - 28}" fill="#2563eb" font-family="Arial, sans-serif" font-size="12" font-weight="700">MAIN RISER</text>')
    if cross_floor_links:
        for link in cross_floor_links:
            if link["fromFloor"] in rows and link["toFloor"] in rows:
                parts.append(f'<path d="M{riser_x} {rows[link["fromFloor"]]} V{rows[link["toFloor"]]}" stroke="#2563eb" stroke-width="7" fill="none" stroke-linecap="round"/>')
    parts.append("</svg>")
    return "".join(parts)


def build_full_cad_review_svg(file, options, flow_markers, *, editor_preview=False, render_bounds=None):
    """Render the complete CAD Model Space and overlay review markers.

    The old review image was reconstructed from the already-selected pipe
    vectors, which made the preview look like a cropped fragment of the
    drawing.  Use ezdxf's drawing backend here so text, blocks, dimensions,
    hatches, fittings and non-pipe layers remain visible in their original
    positions.
    """
    if (
        ezdxf is None
        or ezdxf_bbox is None
        or ezdxf_recover is None
        or BoundingBox2d is None
        or Frontend is None
        or RenderContext is None
        or ezdxf_drawing_config is None
        or ezdxf_drawing_layout is None
        or ezdxf_svg is None
    ):
        return None

    # DWG conversion can leave one incomplete polyline boundary while the
    # remaining Model Space is valid. Use ezdxf recovery for the full preview
    # so that one malformed entity does not reject the complete drawing.
    document = file.get("_cadDocument")
    if document is None:
        document, _auditor = ezdxf_recover.readfile(file["path"])
    if render_bounds is None and not editor_preview:
        render_bounds = (file.get("floorDetection") or {}).get("bounds")
    bbox_cache = file.get("_cadBboxCache")

    # A few DWG files converted by LibreDWG contain table records with a
    # missing name. ezdxf can load those records, but its drawing backend
    # rejects them while building the layer/text-style lookup tables. Repair
    # only the in-memory preview document; the source DXF/DWG is untouched.
    for table_name, fallback_name in (
        ("layers", "0"),
        ("styles", "Standard"),
    ):
        table = getattr(document, table_name, None)
        if table is None:
            continue
        for record in table:
            try:
                name = record.dxf.name
            except Exception:
                continue
            if not isinstance(name, str) or not name:
                try:
                    record.dxf.name = fallback_name
                except Exception:
                    pass

    entities = list(document.modelspace())
    if render_bounds is not None:
        min_x, min_y, max_x, max_y = map(float, render_bounds)
    else:
        bounds = ezdxf_bbox.extents(entities, fast=True)
        if not bounds.has_data:
            return None
        min_x = float(bounds.extmin.x)
        max_x = float(bounds.extmax.x)
        min_y = float(bounds.extmin.y)
        max_y = float(bounds.extmax.y)
    source_width = max(max_x - min_x, 1.0)
    source_height = max(max_y - min_y, 1.0)
    render_box = BoundingBox2d([(min_x, min_y), (max_x, max_y)])

    backend = ezdxf_svg.SVGBackend()
    configuration = ezdxf_drawing_config.Configuration(
        background_policy=(
            ezdxf_drawing_config.BackgroundPolicy.MODELSPACE
            if editor_preview else ezdxf_drawing_config.BackgroundPolicy.WHITE
        ),
        color_policy=(
            ezdxf_drawing_config.ColorPolicy.COLOR_SWAP_BW
            if editor_preview else ezdxf_drawing_config.ColorPolicy.MONOCHROME_LIGHT_BG
        ),
        show_defpoints=True,
    )
    def renderable(entity):
        if editor_preview and entity.dxftype() in {"LINE", "LWPOLYLINE", "POLYLINE"}:
            # These top-level entities are drawn by the editor so moving or
            # deleting them never leaves the old geometry behind.
            return False
        # A missing INSERT block name cannot be resolved by the drawing
        # backend. Keep the rest of Model Space renderable instead of failing
        # the complete preview because of one malformed object.
        try:
            if render_bounds is not None:
                entity_box = ezdxf_bbox.extents([entity], fast=True, cache=bbox_cache)
                if entity_box.has_data and (
                    entity_box.extmax.x < min_x or entity_box.extmin.x > max_x
                    or entity_box.extmax.y < min_y or entity_box.extmin.y > max_y
                ):
                    return False
            if entity.dxftype() == "INSERT":
                name = entity.dxf.get("name")
                return isinstance(name, str) and bool(name)
            return True
        except Exception:
            return False

    Frontend(
        RenderContext(document),
        backend,
        configuration,
    ).draw_layout(document.modelspace(), filter_func=renderable)
    svg = backend.get_string(
        ezdxf_drawing_layout.Page(
            0,
            0,
            units=ezdxf_drawing_layout.Units.mm,
        ),
        render_box=render_box,
        xml_declaration=False,
    )

    if editor_preview:
        # ezdxf puts paint in CSS class rules. SvgXml on native devices cannot
        # reliably resolve those classes, so inline attributes for every path.
        # Its default 1-2 unit strokes are also sub-pixel at a 1M-unit
        # viewBox: enforce a readable *screen* hairline without altering data.
        styles = {}
        min_stroke = 700.0
        for name, css in re.findall(r"<style>\.(C[0-9A-F]+) \{([^}]+)\}</style>", svg):
            attributes = {}
            for part in css.split(";"):
                if ":" in part:
                    key, value = part.split(":", 1)
                    attributes[key.strip()] = value.strip()
            if attributes.get("stroke") not in (None, "none"):
                if attributes["stroke"].lower() == "#000000":
                    attributes["stroke"] = "#dbeafe"
                try:
                    attributes["stroke-width"] = str(max(float(attributes.get("stroke-width", "0")), min_stroke))
                except ValueError:
                    attributes["stroke-width"] = str(min_stroke)
            styles[name] = " ".join(
                f'{key}="{value}"' for key, value in attributes.items()
            )
        svg = re.sub(
            r' class="(C[0-9A-F]+)"',
            lambda match: " " + styles[match.group(1)] if match.group(1) in styles else match.group(0),
            svg,
        )
        svg = re.sub(r"<defs>(?:<style>.*?</style>)+</defs>", "", svg, count=1)

    viewbox_match = re.search(
        r'viewBox="0 0 ([0-9.eE+-]+) ([0-9.eE+-]+)"',
        svg,
    )
    if not viewbox_match:
        return svg
    viewbox_width = float(viewbox_match.group(1))
    viewbox_height = float(viewbox_match.group(2))
    x_scale = viewbox_width / source_width
    y_scale = viewbox_height / source_height

    def project(point):
        return (
            (float(point[0]) - min_x) * x_scale,
            (max_y - float(point[1])) * y_scale,
        )

    def esc_svg(value):
        return (str(value).replace("&", "&amp;")
                .replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))

    labels = {
        "lead_in": "LEAD-IN",
        "up": "上喉",
        "down": "落喉",
        "pipe": "喉管",
        "diameter": "管徑",
    }
    overlay = ['<g id="schematic-review-markers" font-family="Arial,sans-serif">']
    for index, option in enumerate(options, 1):
        x, y = project(option["position"])
        overlay.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="12000" fill="none" stroke="#dc2626" stroke-width="3000"/>')
        overlay.append(f'<text x="{x + 16000:.2f}" y="{y - 10000:.2f}" font-size="12000" fill="#dc2626">接點 {index}</text>')
    for index, marker in enumerate(flow_markers, 1):
        x, y = project(marker["position"])
        label = labels.get(marker.get("kind"), "文字")
        text = esc_svg(marker.get("text", "")[:40])
        overlay.append(f'<rect x="{x - 12000:.2f}" y="{y - 12000:.2f}" width="24000" height="24000" fill="none" stroke="#2563eb" stroke-width="2200"/>')
        overlay.append(f'<text x="{x + 16000:.2f}" y="{y + 5000:.2f}" font-size="9000" fill="#2563eb">{label} [{index}] {text}</text>')
    overlay.append("</g>")
    marker_svg = "".join(overlay)
    close_index = svg.rfind("</svg>")
    return f"{svg[:close_index]}{marker_svg}{svg[close_index:]}" if close_index >= 0 else svg


def build_review_previews(files):
    """Render the source CAD/PDF with in-context candidate markers for App review.

    CAD is the primary Schematic source.  CAD previews are deliberately
    generated from the parsed coordinates instead of converting the file to a
    screenshot, so review markers remain tied to the same geometry used by the
    network builder.
    """
    if pymupdf is None:
        return []
    previews = []
    for file in files:
        network = file.get("network") or {}
        if network.get("status") != "needs_review":
            continue
        if file.get("sourceFormat") == "CAD":
            try:
                options = network.get("endpointOptions", [])[:8]
                flow_markers = network.get("flowMarkers", [])[:24]
                if file.get("_cadPreviewLimited"):
                    file.setdefault("warnings", []).append(
                        "大型 CAD 僅顯示本層候選管線預覽，沒有建築背景；請與原圖核對。"
                    )
                    full_svg = candidate_review_svg(file, options, flow_markers)
                else:
                    full_svg = build_full_cad_review_svg(file, options, flow_markers)
                if not full_svg:
                    continue
                svg_bytes = full_svg.encode("utf-8")
                previews.append({
                    "floor": file["floor"],
                    "fileName": file["name"],
                    "mimeType": "image/svg+xml",
                    "base64": base64.b64encode(svg_bytes).decode("ascii"),
                    "markerCount": len(options),
                    "flowMarkerCount": len(flow_markers),
                })
            except Exception as exc:
                file.setdefault("warnings", []).append(f"無法建立{file['floor']} CAD候選預覽：{exc}")
            continue
        try:
            document = pymupdf.open(file["path"])
            page = document[0]
            options = network.get("endpointOptions", [])[:8]
            flow_markers = network.get("flowMarkers", [])[:24]
            for index, option in enumerate(options, 1):
                point = option["position"]
                page.draw_circle(point, 18, color=(0.86, 0.08, 0.08), fill=None, width=3)
                page.insert_text(
                    (point[0] + 20, point[1] - 12),
                    f"接點 {index}",
                    fontname="helv",
                    fontsize=14,
                    color=(0.86, 0.08, 0.08),
                    fill=(1, 1, 1),
                    render_mode=0,
                )
            flow_labels = {"lead_in": "LEAD-IN", "up": "上喉", "down": "落喉",
                           "pipe": "喉管", "diameter": "管徑"}
            for index, marker in enumerate(flow_markers, 1):
                point = marker["position"]
                label = flow_labels.get(marker.get("kind"), "文字")
                diameter = marker.get("diameterMm")
                suffix = f" DN{diameter:g}" if diameter else ""
                page.draw_rect(
                    pymupdf.Rect(point[0] - 14, point[1] - 14, point[0] + 14, point[1] + 14),
                    color=(0.06, 0.32, 0.76),
                    fill=None,
                    width=2,
                )
                page.insert_text(
                    (point[0] + 18, point[1] + 4),
                    f"{label}{suffix} [{index}]",
                    fontname="helv",
                    fontsize=10,
                    color=(0.06, 0.32, 0.76),
                    fill=(1, 1, 1),
                    render_mode=0,
                )
            page.insert_textbox(
                (24, 24, page.rect.width - 24, 58),
                 f"{file['floor']}：紅圈為線段端點候選；藍框為PDF文字判讀（入口／上喉／落喉／管徑）",
                fontname="helv",
                fontsize=16,
                color=(0.75, 0.02, 0.02),
                fill=(1, 0.96, 0.96),
                align=0,
            )
            # Upper tender sheets are printed opposite to B1.  Match the
            # lift-registration orientation so the in-app review image is
            # readable and the red markers stay on their source locations.
            if not file["floor"].upper().startswith("B"):
                page.set_rotation(180)
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(0.7, 0.7), alpha=False)
            previews.append({
                "floor": file["floor"],
                "fileName": file["name"],
                "mimeType": "image/png",
                "base64": base64.b64encode(pixmap.tobytes("png")).decode("ascii"),
                "markerCount": len(options),
                "flowMarkerCount": len(flow_markers),
            })
            document.close()
        except Exception as exc:
            file.setdefault("warnings", []).append(f"無法建立{file['floor']}原圖候選標記：{exc}")
    return previews


def dxf_text(value):
    """Keep generated DXF text fields on one safe ASCII/control-free line."""
    return re.sub(r"[\r\n\x00-\x1f]", " ", str(value)).strip()


def dxf_pair(code, value):
    return f"{code}\n{value}\n"


def build_dxf(files, risers, cross_floor_links=None):
    """Build an editable DXF schematic from the analyzed floor graph.

    This is intentionally a schematic export rather than a copy of the
    original drawing: the analyzer keeps lengths and connectivity, not every
    original CAD coordinate.
    """
    floors = sorted({file["floor"] for file in files}, key=floor_sort_key)
    row_height = 1000.0
    layers = [
        ("SCHEMATIC_PIPE", 5),
        ("SCHEMATIC_RISER", 6),
        ("SCHEMATIC_TEXT", 7),
    ]
    parts = [
        dxf_pair(0, "SECTION"), dxf_pair(2, "HEADER"),
        dxf_pair(9, "$ACADVER"), dxf_pair(1, "AC1015"),
        dxf_pair(0, "ENDSEC"),
        dxf_pair(0, "SECTION"), dxf_pair(2, "TABLES"),
        dxf_pair(0, "TABLE"), dxf_pair(2, "LAYER"), dxf_pair(70, len(layers)),
    ]
    for name, color in layers:
        parts.extend([
            dxf_pair(0, "LAYER"), dxf_pair(2, name), dxf_pair(70, 0),
            dxf_pair(62, color), dxf_pair(6, "CONTINUOUS"),
        ])
    parts.extend([
        dxf_pair(0, "ENDTAB"), dxf_pair(0, "ENDSEC"),
        dxf_pair(0, "SECTION"), dxf_pair(2, "ENTITIES"),
    ])

    def add_line(x1, y1, x2, y2, layer):
        parts.extend([
            dxf_pair(0, "LINE"), dxf_pair(8, layer),
            dxf_pair(10, round(x1, 3)), dxf_pair(20, round(y1, 3)), dxf_pair(30, 0),
            dxf_pair(11, round(x2, 3)), dxf_pair(21, round(y2, 3)), dxf_pair(31, 0),
        ])

    def add_text(value, x, y, height=40.0):
        parts.extend([
            dxf_pair(0, "TEXT"), dxf_pair(8, "SCHEMATIC_TEXT"),
            dxf_pair(10, round(x, 3)), dxf_pair(20, round(y, 3)), dxf_pair(30, 0),
            dxf_pair(40, height), dxf_pair(1, dxf_text(value)),
        ])

    rows = {floor: 100.0 + index * row_height for index, floor in enumerate(floors)}
    riser_x = 5000.0
    title_y = (max(rows.values()) if rows else 100.0) + 1150.0
    add_text("MAIN PIPE FLOOR SCHEMATIC", 40, title_y, 60.0)
    add_text("VERTICAL RISER DIAGRAM - VERIFY CONNECTIONS BEFORE CONSTRUCTION", 40, title_y - 65.0, 25.0)
    for index, floor in enumerate(floors):
        file = next(item for item in files if item["floor"] == floor)
        y = rows[floor]
        add_line(300.0, y - 350.0, 9700.0, y - 350.0, "SCHEMATIC_TEXT")
        lines, points = schematic_geometry(file, riser_x, y, 3200.0, 300.0)
        for start, end in lines:
            add_line(start[0], start[1], end[0], end[1], "SCHEMATIC_PIPE")
        label_x = 350.0 if index % 2 == 0 else 8250.0
        total = sum(pipe["length"] for pipe in file["pipes"])
        add_text(floor, label_x, y + 230.0, 42.0)
        add_text(f"{len(file['pipes'])} SEG  LEN {total:.1f}  {len(file['fittings'])} FITTINGS", label_x, y + 170.0, 22.0)
        diameters = sorted({pipe["diameterMm"] for pipe in file["pipes"] if pipe.get("diameterMm")})
        if diameters:
            add_text(" / ".join(f"DN{value:g}" for value in diameters[:4]), riser_x - 500.0, y + 230.0, 22.0)
    if floors:
        add_text("MAIN RISER", riser_x + 100.0, rows[floors[-1]] + 420.0, 25.0)
    for link in cross_floor_links or []:
        if link["fromFloor"] in rows and link["toFloor"] in rows:
            add_line(riser_x, rows[link["fromFloor"]], riser_x, rows[link["toFloor"]], "SCHEMATIC_RISER")

    parts.extend([dxf_pair(0, "ENDSEC"), dxf_pair(0, "EOF")])
    return "".join(parts)


def build_pdf(files, risers, cross_floor_links=None):
    """Build a compact vector PDF using the same schematic geometry as the SVG."""
    if pymupdf is None:
        raise RuntimeError("PyMuPDF 未安裝，無法建立 PDF schematic")
    floors = sorted({file["floor"] for file in files}, key=floor_sort_key)
    row_height = 105.0
    width = 1100.0
    height = 180.0 + max(1, len(floors)) * row_height
    document = pymupdf.open()
    page = document.new_page(width=width, height=height)
    page.insert_text((40, 42), "主喉管樓層 Schematic", fontname="china-t", fontsize=24, color=(0.06, 0.09, 0.15))
    page.insert_text(
        (40, 68),
        "垂直主立管圖；由 CAD 圖層、Polyline 及管件圖塊建立，連接關係須覆核",
        fontname="china-t",
        fontsize=12,
        color=(0.28, 0.33, 0.4),
    )
    rows = {floor: height - 90.0 - index * row_height for index, floor in enumerate(floors)}
    riser_x = 520.0
    for index, floor in enumerate(floors):
        y = rows[floor]
        file = next(item for item in files if item["floor"] == floor)
        page.draw_line(pymupdf.Point(80, y + 38), pymupdf.Point(1020, y + 38), color=(0.8, 0.84, 0.89), width=1)
        lines, points = schematic_geometry(file, riser_x, y)
        for start, end in lines:
            page.draw_line(pymupdf.Point(*start), pymupdf.Point(*end), color=(0.15, 0.39, 0.92), width=4)
        for point in points:
            page.draw_circle(pymupdf.Point(point[0], point[1]), 5, color=(0.15, 0.39, 0.92), fill=(1, 1, 1), width=2)
        label_x = 85 if index % 2 == 0 else 850
        page.insert_text((label_x, y - 16), str(floor), fontname="china-t", fontsize=16, color=(0.06, 0.09, 0.15))
        page.insert_text(
            (label_x, y + 5),
            f"{len(file['pipes'])} 段 · {sum(pipe['length'] for pipe in file['pipes']):.1f} 圖紙單位 · {len(file['fittings'])} 件管件",
            fontname="china-t",
            fontsize=9,
            color=(0.28, 0.33, 0.4),
        )
        diameters = sorted({pipe["diameterMm"] for pipe in file["pipes"] if pipe.get("diameterMm")})
        if diameters:
            page.insert_text(
                (riser_x - 160, y - 26),
                " / ".join(f"DN{value:g}" for value in diameters[:4]),
                fontname="helv",
                fontsize=8,
                color=(0.28, 0.33, 0.4),
            )
    if floors:
        page.insert_text((riser_x + 14, rows[floors[-1]] - 28), "MAIN RISER", fontname="helv", fontsize=8, color=(0.15, 0.39, 0.92))
    for link in cross_floor_links or []:
        if link["fromFloor"] in rows and link["toFloor"] in rows:
            page.draw_line(pymupdf.Point(riser_x, rows[link["fromFloor"]]),
                           pymupdf.Point(riser_x, rows[link["toFloor"]]),
                           color=(0.15, 0.39, 0.92), width=5)
    pdf_bytes = document.tobytes(garbage=4, deflate=True)
    document.close()
    return pdf_bytes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--files-json", required=True)
    parser.add_argument("--pipe-layers-json", default="[]")
    parser.add_argument("--fitting-layers-json", default="[]")
    parser.add_argument("--review-overrides-json", default="{}")
    parser.add_argument("--include-floors-json", default="[]")
    args = parser.parse_args()
    try:
        items = json.loads(args.files_json)
        pipe_layers = json.loads(args.pipe_layers_json)
        fitting_layers = json.loads(args.fitting_layers_json)
        review_overrides = json.loads(args.review_overrides_json)
        include_floors = json.loads(args.include_floors_json)
        if not isinstance(include_floors, list) or any(not isinstance(floor, str) for floor in include_floors):
            raise ValueError("樓層範圍格式無效")
        files = [
            parsed
            for item in items
            for parsed in parse_input(item, pipe_layers, fitting_layers, include_floors)
        ]
        files = filter_schematic_floors(files, include_floors)
        if not files:
            raise RuntimeError("沒有可輸出的樓層圖")
        files.sort(key=lambda item: floor_sort_key(item["floor"]))
        for file in files:
            override = review_overrides.get(file["floor"]) if isinstance(review_overrides, dict) else None
            if isinstance(override, dict):
                file["_reviewOverride"] = override
        attach_networks(files)
        registrations, cross_floor_links, registration_diagnostics = register_cross_floor_risers(files)
        risers = find_risers(files)
        review_previews = build_review_previews(files)
        floors = [{
            "name": file["floor"],
            "fileName": file["name"],
            "pipeCount": len(file["pipes"]),
            "fittingCount": len(file["fittings"]),
            "pipeLength": round(sum(pipe["length"] for pipe in file["pipes"]), 3),
            "sourceSheet": file.get("sourceSheet", "single-modelspace"),
            "floorDetection": file.get("floorDetection"),
            "warnings": file.get("warnings", []),
            "network": file["network"],
            "status": file["network"]["status"],
            "confidence": file["network"]["confidence"],
            "diagnostics": file["network"]["diagnostics"],
            "registration": file["registration"],
        } for file in files]
        output = {
            "version": 2,
            "status": ("ready" if files and all(file["network"]["status"] == "ready" for file in files)
                       else "needs_review"),
            "source": "CAD/PDF engineering drawings",
            "floors": floors,
            "pipes": [pipe for file in files for pipe in file["pipes"]],
            "fittings": [fitting for file in files for fitting in file["fittings"]],
            "risers": risers,
            "registration": registrations,
            "crossFloorLinks": cross_floor_links,
            "reviewPreviews": review_previews,
            "svg": build_svg(files, risers, cross_floor_links),
            "dxf": build_dxf(files, risers, cross_floor_links),
            "pdfBase64": base64.b64encode(build_pdf(files, risers, cross_floor_links)).decode("ascii"),
            "warnings": [
                "紫色立管只在CAD有文字標記或跨樓層端點座標接近時建立；請在匯出前覆核。",
                "圖紙單位沿用CAD原始單位；如未設定為毫米，管長及管徑不可直接當作毫米。",
                "Schematic只顯示主喉管及主要管件，不代表牆內或被遮蔽路線已被確認。",
            ] + ([
                "PDF沒有可靠CAD Layer或Block時，系統會把頁面向量線列為候選；請覆核哪些線段屬於主喉管。",
                "PDF長度使用頁面座標點（72點＝1吋），不可直接當作毫米。",
            ] if any(file.get("sourceFormat") == "PDF" for file in files) else [])
            + [
                f"{file['floor']}: {diagnostic}"
                for file in files
                if file["network"]["status"] == "needs_review"
                for diagnostic in file["network"]["diagnostics"]
            ] + [
                warning
                for warning in registration_diagnostics
            ] + [
                warning
                for file in files
                for warning in file.get("warnings", [])
            ],
        }
        print(json.dumps(output, ensure_ascii=False))
    except Exception as exc:
        print(f"圖紙分析失敗：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())