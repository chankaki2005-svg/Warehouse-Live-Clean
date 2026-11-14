"""Conservative orthographic box measurements, never automatic size approval.

Only active Model Space primitives are considered. Anonymous dimension graphics,
sheet bounds, INSERT definitions and unreferenced blocks are deliberately ignored.
"""
import itertools
import math

from ezdxf import bbox

MAX_MEASUREMENT_ENTITIES = 2000


def _rectangle(entity):
    if entity.dxftype() != "LWPOLYLINE" or not entity.closed or len(entity) != 4:
        return None
    points = list(entity.get_points("xyb"))
    if len(points) != 4 or any(abs(p[2]) > 1e-9 for p in points):
        return None
    if abs(entity.dxf.get("elevation", 0)) > 1e-6:
        return None
    if tuple(entity.dxf.get("extrusion", (0, 0, 1))) != (0, 0, 1):
        return None
    xs, ys = [float(p[0]) for p in points], [float(p[1]) for p in points]
    if not all(math.isfinite(v) and abs(v) <= 1e9 for v in xs + ys):
        return None
    bounds = (min(xs), min(ys), max(xs), max(ys))
    w, h = bounds[2] - bounds[0], bounds[3] - bounds[1]
    if min(w, h) <= 0:
        return None
    tolerance = max(w, h) * 1e-6
    corners = {(round((x - bounds[0]) / w), round((y - bounds[1]) / h)) for x, y, _ in points}
    if len(corners) != 4:
        return None
    for p, q in zip(points, points[1:] + points[:1]):
        if abs(p[0] - q[0]) > tolerance and abs(p[1] - q[1]) > tolerance:
            return None
    return bounds


def _size(r):
    return r[2] - r[0], r[3] - r[1]


def _near(a, b):
    return abs(a - b) <= max(0.01, max(abs(a), abs(b)) * 0.001)


def _touch(a, b, tolerance=0):
    return a[0] <= b[2] + tolerance and a[2] >= b[0] - tolerance and a[1] <= b[3] + tolerance and a[3] >= b[1] - tolerance


def measure_orthographic_box(entities, unit_to_mm):
    if unit_to_mm is None or not math.isfinite(unit_to_mm) or unit_to_mm <= 0 or len(entities) > MAX_MEASUREMENT_ENTITIES:
        return None
    vertex_counts = [len(e) for e in entities if e.dxftype() == "LWPOLYLINE"]
    if any(count > 256 for count in vertex_counts) or sum(vertex_counts) > 10000:
        return None
    rectangles = [(i, r) for i, e in enumerate(entities) if (r := _rectangle(e)) is not None]
    if len(rectangles) < 3 or len(rectangles) > 60:
        return None
    largest = max(_size(r)[0] * _size(r)[1] for _, r in rectangles)
    bodies = [(i, r) for i, r in rectangles if _size(r)[0] * _size(r)[1] >= largest * 0.1]
    if len(bodies) > 20:
        return None
    matches = []
    for first, second in itertools.combinations(bodies, 2):
        a, b = first[1], second[1]
        aw, ah = _size(a)
        bw, bh = _size(b)
        # Stacked views must have the same horizontal limits, and not overlap.
        if _touch(a, b) or not (_near(aw, bw) and _near(a[0], b[0]) and _near(a[2], b[2])):
            continue
        if abs(a[0] - b[0]) > aw * 0.001 or abs(a[2] - b[2]) > aw * 0.001 or _near(ah, bh):
            continue
        # Treat the taller view as the suggested front. User must confirm axes.
        front, plan = (first, second) if ah > bh else (second, first)
        height, width = _size(front[1])[1], _size(plan[1])[1]
        for side in bodies:
            if side[0] in (front[0], plan[0]) or _touch(side[1], a) or _touch(side[1], b):
                continue
            sw, sh = _size(side[1])
            regular = _near(sw, width) and _near(sh, height)
            rotated = _near(sw, height) and _near(sh, width)
            if regular == rotated:
                continue
            matches.append((front, plan, side, rotated))
    if len(matches) != 1:
        return None  # Repeated equipment or ambiguous view mappings need review, not a random pick.
    front, plan, side, rotated = matches[0]
    chosen = {front[0], plan[0], side[0]}
    usable = []
    for index, entity in enumerate(entities):
        if index in chosen or entity.dxftype() not in ("LINE", "LWPOLYLINE", "CIRCLE", "ARC"):
            continue
        # Do not silently measure 3D / non-planar primitives in a 2D view.
        try:
            box = bbox.extents([entity])
            if not box.has_data or abs(box.extmin.z) > 1e-6 or abs(box.extmax.z) > 1e-6:
                continue
            r = (box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y)
            if all(math.isfinite(v) and abs(v) <= 1e9 for v in r):
                usable.append(r)
        except Exception:
            continue
    measured = []
    for label, (_, body) in zip(("建議主視圖（長×高）", "建議平面圖（長×闊）", "側視圖（旋轉）" if rotated else "側視圖（闊×高）"), (front, plan, side)):
        w, h = _size(body)
        expanded = body
        pending = usable[:]
        for _ in range(8):
            added = False
            for r in pending[:]:
                # A primitive must touch exactly one view and remain near that body.
                if any(_touch(r, v[1], 0.1 / unit_to_mm) for v in (front, plan, side) if v[1] != body):
                    continue
                union = (min(expanded[0], r[0]), min(expanded[1], r[1]), max(expanded[2], r[2]), max(expanded[3], r[3]))
                if _touch(expanded, r, 0.1 / unit_to_mm):
                    if _size(union)[0] > w * 1.5 or _size(union)[1] > h * 1.5:
                        return None
                    expanded = union
                    pending.remove(r)
                    added = True
            if not added:
                break
        # A chain longer than the bounded pass must not silently undercount.
        for r in pending:
            if any(_touch(r, v[1], 0.1 / unit_to_mm) for v in (front, plan, side) if v[1] != body):
                continue
            if _touch(expanded, r, 0.1 / unit_to_mm) and (
                r[0] < expanded[0] or r[1] < expanded[1] or r[2] > expanded[2] or r[3] > expanded[3]
            ):
                return None
        measured.append({
            "label": label, "horizontalMm": round(w * unit_to_mm, 3), "verticalMm": round(h * unit_to_mm, 3),
            "overallHorizontalMm": round(_size(expanded)[0] * unit_to_mm, 3),
            "overallVerticalMm": round(_size(expanded)[1] * unit_to_mm, 3), "bounds": list(body),
        })
    f, p, s = measured
    side_width = s["overallVerticalMm"] if rotated else s["overallHorizontalMm"]
    side_height = s["overallHorizontalMm"] if rotated else s["overallVerticalMm"]
    # Both projections of each axis must agree, including the protrusions.
    if not all((_near(f["overallHorizontalMm"], p["overallHorizontalMm"]),
                _near(p["overallVerticalMm"], side_width),
                _near(f["overallVerticalMm"], side_height))):
        return None
    body = {"lengthMm": f["horizontalMm"], "widthMm": p["verticalMm"], "heightMm": f["verticalMm"]}
    overall = {"lengthMm": max(f["overallHorizontalMm"], p["overallHorizontalMm"]),
               "widthMm": max(p["overallVerticalMm"], side_width),
               "heightMm": max(f["overallVerticalMm"], side_height)}
    if any(not 0 < v <= 100000 for v in list(body.values()) + list(overall.values())):
        return None
    return {
        "source": "cad_geometry_review", "bodyEnvelope": body, "overallEnvelope": overall, "views": measured,
        "axisConvention": "長＝上下視圖共用的水平方向；高＝較高主視圖的垂直方向；闊＝另一視圖的垂直方向。此軸向只是建議，套用前請確認。",
        "warnings": [
            "這些是 CAD 活動幾何的量度，不是原生 DIMENSION 標註或實物量度；尚未套用。",
            "伸出部件只計入與箱身接觸的附近二維圖元；隱藏、分離、旋轉或不支援的部件可能未涵蓋，模型外形仍須覆核。",
            "不同視圖已分開量度；未使用整張圖紙範圍，也未採用殘留尺寸圖塊。",
        ],
    }
