"""Count dominant repeated vector marks in a PDF schematic.

The result is a candidate count, not semantic proof that every repeated mark is
a valve. OCR remains responsible for reading valve type and size labels.
"""

import json
import os
import sys

import pymupdf


MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 250
MAX_DRAWINGS_PER_PAGE = 100_000
MAX_CANDIDATE_MARKS = 10_000
MIN_SYMBOL_EDGE_PT = 6.5
MAX_SYMBOL_EDGE_PT = 18.5


def _canonical_signature(drawing):
    rect = drawing["rect"]
    items = drawing["items"]
    if len(items) != 3 or any(item[0] != "l" for item in items):
        return None
    if min(rect.width, rect.height) < MIN_SYMBOL_EDGE_PT:
        return None
    if max(rect.width, rect.height) > MAX_SYMBOL_EDGE_PT:
        return None

    center_x = (rect.x0 + rect.x1) / 2
    center_y = (rect.y0 + rect.y1) / 2
    scale = max(rect.width, rect.height)
    transforms = (
        lambda x, y: (x, y),
        lambda x, y: (-y, x),
        lambda x, y: (-x, -y),
        lambda x, y: (y, -x),
        lambda x, y: (-x, y),
        lambda x, y: (x, -y),
        lambda x, y: (y, x),
        lambda x, y: (-y, -x),
    )

    possible_signatures = []
    for transform in transforms:
        segments = []
        for item in items:
            endpoints = []
            for point in (item[1], item[2]):
                x, y = transform(
                    (point.x - center_x) / scale,
                    (point.y - center_y) / scale,
                )
                endpoints.append((round(x, 1), round(y, 1)))
            segments.append(tuple(sorted(endpoints)))
        possible_signatures.append(tuple(sorted(segments)))
    return min(possible_signatures)


def _page_result(page, page_number):
    drawings = page.get_drawings()
    if len(drawings) > MAX_DRAWINGS_PER_PAGE:
        raise ValueError(f"第 {page_number} 頁向量圖形過多")

    signatures = {}
    for drawing in drawings:
        signature = _canonical_signature(drawing)
        if signature is not None:
            signatures.setdefault(signature, []).append(drawing)

    total_small_paths = sum(len(items) for items in signatures.values())
    page_width = float(page.rect.width)
    page_height = float(page.rect.height)
    base_result = {
        "pageNumber": page_number,
        "pageWidthPt": round(page_width, 2),
        "pageHeightPt": round(page_height, 2),
    }
    if not total_small_paths:
        return {**base_result, "candidateCount": 0, "confidence": "none", "candidateMarks": []}

    repeated_signature = max(signatures, key=lambda item: len(signatures[item]))
    repeated_drawings = signatures[repeated_signature]
    repeated_count = len(repeated_drawings)
    repeated_share = repeated_count / total_small_paths
    if repeated_count < 5 or repeated_share < 0.6:
        return {**base_result, "candidateCount": 0, "confidence": "none", "candidateMarks": []}

    confidence = (
        "high" if repeated_count >= 10 and repeated_share >= 0.8 else "medium"
    )
    ordered_drawings = sorted(
        repeated_drawings,
        key=lambda drawing: (
            round(float((drawing["rect"] * page.rotation_matrix).y0), 2),
            round(float((drawing["rect"] * page.rotation_matrix).x0), 2),
        ),
    )
    candidate_marks = []
    for index, drawing in enumerate(ordered_drawings, start=1):
        # get_drawings() reports unrotated page-space coordinates; PDF.js renders
        # the visible, rotated page. Transform boxes so overlays align with it.
        rect = drawing["rect"] * page.rotation_matrix
        candidate_marks.append({
            "id": f"p{page_number}-{index}",
            "pageNumber": page_number,
            "x": round(float(rect.x0), 2),
            "y": round(float(rect.y0), 2),
            "width": round(float(rect.width), 2),
            "height": round(float(rect.height), 2),
        })

    return {
        **base_result,
        "candidateCount": repeated_count,
        "confidence": confidence,
        "candidateMarks": candidate_marks,
    }


def analyze_pdf(pdf_path):
    if os.path.getsize(pdf_path) > MAX_PDF_BYTES:
        raise ValueError("PDF 超過向量分析上限")

    document = pymupdf.open(pdf_path)
    try:
        page_count = document.page_count
        if page_count <= 0:
            raise ValueError("PDF 沒有可處理的頁面")
        if page_count > MAX_PDF_PAGES:
            raise ValueError(f"PDF 頁數超過向量分析上限（最多 {MAX_PDF_PAGES} 頁）")

        pages = [
            _page_result(document[index], index + 1)
            for index in range(page_count)
        ]
    finally:
        document.close()

    detected_pages = [page for page in pages if page["candidateCount"] > 0]
    candidate_count = sum(page["candidateCount"] for page in detected_pages)
    candidate_marks = [
        mark
        for page in detected_pages
        for mark in page["candidateMarks"]
    ]
    if len(candidate_marks) > MAX_CANDIDATE_MARKS:
        raise ValueError("PDF 向量候選標記過多，無法提供逐個覆核")
    confidence = (
        "high"
        if detected_pages and all(page["confidence"] == "high" for page in detected_pages)
        else "medium" if detected_pages else "none"
    )
    return {
        "candidateCount": candidate_count,
        "candidateMarks": candidate_marks,
        "confidence": confidence,
        "pageCount": page_count,
        "pages": pages,
    }


def main():
    if len(sys.argv) != 2:
        raise ValueError("請提供 PDF 路徑")
    print(json.dumps(analyze_pdf(sys.argv[1]), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
