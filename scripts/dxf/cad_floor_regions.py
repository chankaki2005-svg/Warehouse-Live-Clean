"""Evidence-based plan titles and rectangular drawing boundaries for CAD."""

import re
from html import escape


def plan_title_floor(text):
    text = re.sub(r"%%[UuOo]", "", text).strip().upper()
    text = re.sub(r"\s+", " ", text)
    # A typical-floor range is not one uniquely identified floor.
    if re.search(r"\d+(?:ST|ND|RD|TH)?\s*[-~至]\s*\d+", text):
        return None
    text = re.sub(r"^GAS INSTALLATION (?:AT|ON)\s+", "", text)
    match = re.fullmatch(
        r"(GROUND|FIRST|SECOND|THIRD|FOURTH|FIFTH|"
        r"\d{1,3}(?:ST|ND|RD|TH)|\d{1,3}/F|G/F|"
        r"BASEMENT\s*\d+|B\d+|ROOF)\s+(?:FLOOR\s+)?PLAN", text
    )
    if not match:
        return None
    floor = match.group(1)
    if floor in ("GROUND", "G/F"):
        return "G/F"
    if floor == "ROOF":
        return "Roof"
    if floor.startswith(("BASEMENT", "B")):
        return "B" + re.search(r"\d+", floor).group()
    words = {"FIRST": 1, "SECOND": 2, "THIRD": 3, "FOURTH": 4, "FIFTH": 5}
    number = words.get(floor)
    if number is None:
        number = int(re.search(r"\d+", floor).group())
    return f"{number}/F"


def rectangular_frame(entity):
    if entity.dxftype() != "LWPOLYLINE" or not entity.closed:
        return None
    points = list(entity.get_points("xyb"))
    if len(points) not in (4, 5) or any(abs(point[2]) > 1e-9 for point in points):
        return None
    xs, ys = [point[0] for point in points], [point[1] for point in points]
    bounds = [float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))]
    if bounds[0] == bounds[2] or bounds[1] == bounds[3]:
        return None
    corners = {(round(x, 3), round(y, 3)) for x in (bounds[0], bounds[2])
               for y in (bounds[1], bounds[3])}
    if {(round(p[0], 3), round(p[1], 3)) for p in points} != corners:
        return None
    # A diagonal/bow-tie through the same corners is not a frame.
    if any(abs(a[0] - b[0]) > 0.001 and abs(a[1] - b[1]) > 0.001
           for a, b in zip(points, points[1:] + points[:1])):
        return None
    return {"bounds": bounds, "layer": entity.dxf.layer}


def contains(bounds, point, tolerance=0.001):
    return (bounds[0] - tolerance <= point[0] <= bounds[2] + tolerance
            and bounds[1] - tolerance <= point[1] <= bounds[3] + tolerance)


def attach_title_frames(labels, frames):
    for label in labels:
        height = max(label.get("height", 0), 0.001)
        candidates = [
            frame for frame in frames
            if contains(frame["bounds"], label["position"], height * 2)
            and frame["bounds"][2] - frame["bounds"][0] >= height * 40
            and frame["bounds"][3] - frame["bounds"][1] >= height * 40
        ]
        if candidates:
            def score(frame):
                bounds = frame["bounds"]
                preferred = bool(re.search(r"REF|FRAME|BORDER|DEFPOINTS|圖框",
                                           frame["layer"], re.I))
                return (not preferred, (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]))
            chosen = min(candidates, key=score)
            label["bounds"] = chosen["bounds"]
    return labels


def candidate_review_svg(file, options, flow_markers):
    """Bounded geometry-only review; explicitly not a full drawing rendering."""
    bounds = (file.get("floorDetection") or {}).get("bounds")
    points = [point for pipe in file["pipes"] for point in pipe.get("points", [])]
    if not bounds:
        if not points:
            return None
        bounds = [min(p[0] for p in points), min(p[1] for p in points),
                  max(p[0] for p in points), max(p[1] for p in points)]
    scale = min(1120 / max(bounds[2] - bounds[0], 1),
                800 / max(bounds[3] - bounds[1], 1))
    width, height = 1200, 900

    def project(point):
        return (40 + (point[0] - bounds[0]) * scale,
                75 + (bounds[3] - point[1]) * scale)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="1200" height="900" fill="white"/>',
        f'<text x="30" y="26" font-size="18" fill="#0f172a">{escape(file["floor"])} — 候選管線預覽</text>',
        '<text x="30" y="50" font-size="15" fill="#b45309">大型 CAD：未顯示建築背景；請與原圖核對，並非完整平面圖。</text>',
    ]
    for pipe in file["pipes"]:
        coordinates = " ".join(f"{x:.2f},{y:.2f}" for x, y in
                               (project(point) for point in pipe.get("points", [])))
        if coordinates:
            parts.append(f'<polyline points="{coordinates}" stroke="#2563eb" stroke-width="2" fill="none"/>')
    for number, option in enumerate(options, 1):
        x, y = project(option["position"])
        parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="8" fill="#dc2626"/>')
        parts.append(f'<text x="{x + 11:.2f}" y="{y:.2f}" font-size="16" fill="#dc2626">入口候選 {number}</text>')
    for marker in flow_markers:
        x, y = project(marker["position"])
        text = escape(str(marker.get("text", marker.get("kind", "")))[:100])
        parts.append(f'<text x="{x:.2f}" y="{y:.2f}" font-size="13" fill="#15803d">{text}</text>')
    return "".join(parts) + "</svg>"
