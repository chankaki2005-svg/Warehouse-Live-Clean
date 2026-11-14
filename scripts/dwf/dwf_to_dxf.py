#!/usr/bin/env python3
"""Bounded conversion of 2D DWF/DWFx sheets to a multi-space DXF."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable

import ezdxf
import ezdwf

from extract_dwf import geometry_compatible_source


MAX_SHEETS = 60
MAX_ENTITIES_PER_SHEET = 500_000
MAX_OUTPUT_ENTITIES_TOTAL = 500_000
MAX_DXF_LAYERS = 10_000
LIMITS = ezdwf.ParseLimits(
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


def _point(value: Any) -> tuple[float, float]:
    x, y = float(value.x), float(value.y)
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("DWF contains non-finite 2D coordinates")
    return x, y


def _color(entity: Any) -> tuple[int, int, int] | None:
    value = getattr(getattr(entity, "style", None), "color", None)
    if value is None:
        value = getattr(getattr(entity, "style", None), "stroke_color", None)
    try:
        return tuple(int(value[i]) for i in range(3))  # type: ignore[index]
    except (TypeError, IndexError, ValueError):
        return None


def _layer(entity: Any) -> str:
    value = getattr(entity, "layer", None)
    if value is None:
        value = getattr(getattr(entity, "style", None), "layer", "0")
    return str(value or "0")[:255]


def _configure(target: Any, source: Any, layers: set[str]) -> None:
    layer = _layer(source)
    if layer not in layers:
        if len(layers) >= MAX_DXF_LAYERS:
            raise ValueError(f"DWF layer count exceeds safety limit ({MAX_DXF_LAYERS})")
        if layer not in target.doc.layers:
            target.doc.layers.add(layer)
        layers.add(layer)
    target.dxf.layer = layer
    color = _color(source)
    if color is not None:
        target.dxf.true_color = (color[0] << 16) | (color[1] << 8) | color[2]


def _add_polyline(layout: Any, entity: Any, points: Iterable[Any], layers: set[str], closed: bool = False) -> bool:
    points = tuple(points)
    if len(points) < 2:
        return False
    target = layout.add_lwpolyline([_point(p) for p in points], close=closed)
    _configure(target, entity, layers)
    return True


def _path_points(figure: Any) -> list[tuple[float, float]] | None:
    """Return exact linear figure vertices; curved segments must not be flattened silently."""
    start = getattr(figure, "start", None)
    if start is None:
        return None
    points = [_point(start)]
    for segment in getattr(figure, "segments", ()):
        if str(getattr(segment, "kind", "")).lower() != "line":
            return None
        end = getattr(segment, "end", None)
        if end is None:
            return None
        points.append(_point(end))
    return points


def _arc_dxf_geometry(entity: Any) -> tuple[tuple[float, float], float, float, float] | None:
    """Map a DWF circular basis to DXF's counter-clockwise WCS ARC.

    A skewed or non-uniform basis describes an ellipse, which cannot be
    represented by ARC without changing geometry and is therefore rejected.
    Reflected bases are reversed so the same arc locus is retained.
    """
    center = getattr(entity, "center", None)
    x_axis = getattr(entity, "x_axis", None)
    y_axis = getattr(entity, "y_axis", None)
    start = getattr(entity, "start_angle_degrees", None)
    end = getattr(entity, "end_angle_degrees", None)
    if center is None or x_axis is None or y_axis is None or start is None or end is None:
        return None
    xx, xy = float(x_axis.x), float(x_axis.y)
    yx, yy = float(y_axis.x), float(y_axis.y)
    radius = math.hypot(xx, xy)
    other_radius = math.hypot(yx, yy)
    dot = xx * yx + xy * yy
    determinant = xx * yy - xy * yx
    tolerance = max(radius, other_radius, 1.0) * 1e-7
    if (
        not math.isfinite(radius)
        or not math.isfinite(other_radius)
        or radius <= 0
        or abs(radius - other_radius) > tolerance
        or abs(dot) > tolerance
    ):
        return None
    offset = math.degrees(math.atan2(xy, xx))
    first = offset + float(start)
    second = offset + float(end)
    if determinant < 0:
        first, second = second, first
    return _point(center), radius, first % 360.0, second % 360.0


def convert_entity(
    layout: Any,
    entity: Any,
    layers: set[str],
    max_output_entities: int = MAX_OUTPUT_ENTITIES_TOTAL,
) -> str | None:
    """Convert one normalized ezdwf entity without changing its coordinates."""
    kind = str(getattr(entity, "kind", "")).upper()
    if kind == "LINE":
        points = getattr(entity, "points", ())
        if len(points) >= 2:
            target = layout.add_line(_point(points[0]), _point(points[1]))
            _configure(target, entity, layers)
            return "LINE"
    if kind == "POLYTRIANGLE":
        points = tuple(getattr(entity, "points", ()))
        triangle_count = max(0, len(points) - 2)
        if triangle_count == 0 or triangle_count > max_output_entities:
            return None
        # DWF POLYTRIANGLE uses a sliding triangle strip. Treating the whole
        # vertex list as one closed polyline invents diagonals and drops strip
        # triangles (notably for four-vertex strips).
        for index in range(triangle_count):
            triangle = points[index : index + 3]
            target = layout.add_lwpolyline([_point(point) for point in triangle], close=True)
            _configure(target, entity, layers)
        return "POLYLINE"
    if kind == "POLYLINE":
        closed = bool(getattr(entity, "closed", False))
        if _add_polyline(layout, entity, getattr(entity, "points", ()), layers, closed):
            return "POLYLINE"
    if kind == "POLYBEZIER":
        # DXF SPLINE conversion needs curve-specific knot/control-point data.
        # Report it as unsupported instead of joining Bézier control points.
        return None
    if kind == "CIRCLE":
        center = getattr(entity, "center", None)
        axes = (getattr(entity, "x_axis", None), getattr(entity, "y_axis", None))
        if center is not None and axes[0] is not None and axes[1] is not None:
            xx, xy = float(axes[0].x), float(axes[0].y)
            yx, yy = float(axes[1].x), float(axes[1].y)
            radius = math.hypot(xx, xy)
            other_radius = math.hypot(yx, yy)
            tolerance = max(radius, other_radius, 1.0) * 1e-7
            if (
                radius > 0
                and math.isfinite(radius)
                and math.isfinite(other_radius)
                and abs(radius - other_radius) <= tolerance
                and abs(xx * yx + xy * yy) <= tolerance
            ):
                target = layout.add_circle(_point(center), radius)
                _configure(target, entity, layers)
                return "CIRCLE"
    if kind == "ARC":
        geometry = _arc_dxf_geometry(entity)
        if geometry is not None:
            center, radius, start, end = geometry
            target = layout.add_arc(center, radius, start, end)
            _configure(target, entity, layers)
            return "ARC"
    if kind in {"TEXT", "TEXTURE"} and getattr(entity, "text", None):
        points = getattr(entity, "points", ())
        style = getattr(entity, "style", None)
        if points:
            target = layout.add_text(str(entity.text), dxfattribs={
                "insert": _point(points[0]),
                "height": float(getattr(style, "font_height", None) or 1.0),
                "rotation": float(getattr(style, "font_rotation_degrees", None) or 0.0),
            })
            _configure(target, entity, layers)
            return "TEXT"
    # Contours and glyph/path outlines are already source geometry; retain them
    # as polylines rather than fabricating curves or rasterizing them.
    figures = (
        getattr(entity, "contours", ())
        or getattr(entity, "path", ())
        or getattr(entity, "glyph_outline", ())
    )
    converted_paths: list[tuple[list[tuple[float, float]], bool]] = []
    for figure in figures:
        if hasattr(figure, "segments"):
            points = _path_points(figure)
            if points is None:
                return None
            closed = bool(getattr(figure, "closed", False))
        else:
            try:
                points = [_point(point) for point in figure]
            except (TypeError, AttributeError, ValueError):
                return None
            closed = bool(getattr(entity, "closed", False))
        if len(points) >= 2:
            converted_paths.append((points, closed))
    if len(converted_paths) > max_output_entities:
        return None
    for points, closed in converted_paths:
        if not _add_polyline(layout, entity, points, layers, closed):
            return None
    if converted_paths:
        return "POLYLINE"
    return None


def _sheet_name(sheet: Any, index: int) -> str:
    return str(getattr(sheet, "title", None) or getattr(sheet, "name", None) or f"Sheet {index + 1}").strip()[:255]


def _dxf_units(value: Any) -> int:
    normalized = str(value or "").strip().lower().replace(" ", "")
    return {"in": 1, "inch": 1, "inches": 1, "ft": 2, "foot": 2, "feet": 2,
            "mm": 4, "millimeter": 4, "millimeters": 4, "cm": 5,
            "centimeter": 5, "centimeters": 5, "m": 6, "meter": 6,
            "meters": 6}.get(normalized, 0)


def convert(source: Path, output: Path, selected_sheet: int | None = None) -> dict[str, Any]:
    if not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("DWF file does not exist or is empty")
    drawing = ezdwf.read(geometry_compatible_source(source), limits=LIMITS)
    sheets = list(drawing.sheets)
    if not sheets:
        raise ValueError("DWF contains no readable 2D sheets")
    if len(sheets) > MAX_SHEETS:
        raise ValueError(f"DWF sheet count exceeds safety limit ({MAX_SHEETS})")
    if selected_sheet is not None and not 0 <= selected_sheet < len(sheets):
        raise ValueError("selected sheet index is out of range")
    indices = [selected_sheet] if selected_sheet is not None else list(range(len(sheets)))
    doc = ezdxf.new("R2013")
    # DXF has one drawing-wide insertion-unit header. Use the first selected
    # sheet when known; individual sheet units remain visible in the manifest.
    doc.units = _dxf_units(getattr(sheets[indices[0]], "units", None))
    manifest: dict[str, Any] = {"input": str(source), "output": str(output), "sheets": [], "warnings": []}
    layers: set[str] = set()
    total_output_entities = 0
    for position, index in enumerate(indices):
        sheet = sheets[index]
        name = _sheet_name(sheet, index)
        layout = doc.modelspace() if position == 0 else doc.layouts.new(f"Sheet_{index + 1}_{name}"[:255])
        converted = 0
        skipped = 0
        entities = list(getattr(sheet, "all_entities", ()))
        if len(entities) > MAX_ENTITIES_PER_SHEET:
            raise ValueError(f"sheet {index} exceeds entity safety limit")
        for entity in entities:
            remaining_output_entities = MAX_OUTPUT_ENTITIES_TOTAL - total_output_entities
            if remaining_output_entities <= 0:
                raise ValueError(f"DWF output entity safety limit exceeded ({MAX_OUTPUT_ENTITIES_TOTAL})")
            before_count = len(layout)
            kind = convert_entity(layout, entity, layers, remaining_output_entities)
            total_output_entities += len(layout) - before_count
            if kind is None:
                skipped += 1
            else:
                converted += 1
        units = getattr(sheet, "units", None)
        manifest["sheets"].append({"index": index, "name": name, "units": units, "layout": layout.name,
                                   "sourceEntityCount": len(entities), "convertedEntityCount": converted,
                                   "skippedEntityCount": skipped})
        if skipped:
            manifest["warnings"].append(f"{name}: skipped {skipped} unsupported or unsafe entities")
    if not any(item["convertedEntityCount"] for item in manifest["sheets"]):
        raise ValueError("no safely representable 2D vector geometry found")
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(output)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_path")
    parser.add_argument("output_path")
    parser.add_argument("--sheet-index", type=int, help="0-based sheet index; omit to convert all sheets")
    args = parser.parse_args()
    try:
        result = convert(Path(args.input_path), Path(args.output_path), args.sheet_index)
    except Exception as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()