import os
import tempfile

import pymupdf

from analyze_pdf_valve_symbols import analyze_pdf


def draw_symbol(page, x, y):
    page.draw_polyline([(x, y + 13), (x + 10, y), (x, y), (x + 10, y + 13)])


def draw_decoy(page, x, y):
    page.draw_polyline([(x, y), (x + 10, y), (x + 10, y + 13), (x, y + 13)])


def analyze_document(document):
    with tempfile.TemporaryDirectory(prefix="pdf-valve-symbol-test-") as temp_dir:
        pdf_path = os.path.join(temp_dir, "drawing.pdf")
        with open(pdf_path, "wb") as pdf_file:
            pdf_file.write(document.tobytes())
        document.close()
        return analyze_pdf(pdf_path)


def test_repeated_vector_symbols_are_counted():
    document = pymupdf.open()
    page = document.new_page(width=500, height=300)
    for index in range(8):
        draw_symbol(page, 20 + index * 28, 100)
    draw_decoy(page, 300, 100)
    result = analyze_document(document)
    assert result["candidateCount"] == 8, result
    assert result["confidence"] == "medium", result
    assert result["pages"][0]["pageNumber"] == 1, result
    assert len(result["candidateMarks"]) == 8, result
    assert all(mark["width"] > 0 and mark["height"] > 0 for mark in result["candidateMarks"]), result


def test_sparse_shapes_are_not_reported_as_a_confident_family():
    document = pymupdf.open()
    page = document.new_page(width=500, height=300)
    for index in range(3):
        draw_symbol(page, 20 + index * 28, 100)
    result = analyze_document(document)
    assert result["candidateCount"] == 0, result
    assert result["confidence"] == "none", result


def test_rotated_page_candidate_boxes_use_visible_page_coordinates():
    document = pymupdf.open()
    page = document.new_page(width=500, height=300)
    for index in range(8):
        draw_symbol(page, 20 + index * 28, 100)
    page.set_rotation(270)
    result = analyze_document(document)

    page_info = result["pages"][0]
    assert page_info["pageWidthPt"] == 300, result
    assert page_info["pageHeightPt"] == 500, result
    assert result["candidateCount"] == 8, result
    assert all(
        0 <= mark["x"]
        and 0 <= mark["y"]
        and mark["x"] + mark["width"] <= page_info["pageWidthPt"] + 1
        and mark["y"] + mark["height"] <= page_info["pageHeightPt"] + 1
        for mark in result["candidateMarks"]
    ), result


if __name__ == "__main__":
    test_repeated_vector_symbols_are_counted()
    test_sparse_shapes_are_not_reported_as_a_confident_family()
    test_rotated_page_candidate_boxes_use_visible_page_coordinates()
    print("PDF valve vector symbol tests passed")
