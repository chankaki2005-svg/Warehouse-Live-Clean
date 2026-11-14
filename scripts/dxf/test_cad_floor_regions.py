"""Regression tests for combined-modelspace floor-title and frame partitioning."""

import tempfile
import base64
from xml.etree import ElementTree
from pathlib import Path

import ezdxf

from build_schematic import attach_networks, build_review_previews, filter_schematic_floors, parse_cad_file
from cad_floor_regions import plan_title_floor


def test_title_normalization():
    assert plan_title_floor("%%UGROUND FLOOR PLAN") == "G/F"
    assert plan_title_floor("%%U1ST FLOOR PLAN") == "1/F"
    assert plan_title_floor("2ND FLOOR PLAN") == "2/F"
    assert plan_title_floor("THIRD FLOOR PLAN") == "3/F"
    assert plan_title_floor("BASEMENT 1 PLAN") == "B1"
    assert plan_title_floor("%%U5TH - 28TH FLOOR PLAN") is None
    assert plan_title_floor("BUILDING LINE ABOVE (4/F)") is None
    assert plan_title_floor("1/F") is None


def test_titles_frames_and_attributes_override_incidental_floor_notes():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "all-floors.dxf"
        doc = ezdxf.new("R2000")
        doc.layers.new("0-TG-Pipe(Manifold)")
        doc.layers.new("FRAME")
        model = doc.modelspace()
        doc.blocks.new("TITLE")
        titles = ["GROUND FLOOR PLAN", "1ST FLOOR PLAN", "2ND FLOOR PLAN",
                  "3RD FLOOR PLAN", "4TH FLOOR PLAN", "BASEMENT 1 PLAN"]
        for index, title in enumerate(titles):
            x = index * 1100
            model.add_lwpolyline(
                [(x, 0), (x + 1000, 0), (x + 1000, 800), (x, 800)],
                close=True, dxfattribs={"layer": "FRAME"},
            )
            if index == 1:
                block = model.add_blockref("TITLE", (x + 100, 10))
                block.add_attrib("FLOOR_TITLE", title, (x + 100, 10),
                                 dxfattribs={"height": 10})
            elif index == 2:
                model.add_mtext(title, dxfattribs={
                    "insert": (x + 100, 10), "char_height": 10,
                })
            else:
                model.add_text("%%U" + title, dxfattribs={
                    "insert": (x + 100, 10), "height": 10,
                })
            model.add_line((x + 200, 400), (x + 600, 400),
                           dxfattribs={"layer": "0-TG-Pipe(Manifold)"})
            # These previously split the plan into fake floors.
            model.add_text("60/F", dxfattribs={"insert": (x + 400, 400), "height": 5})
            model.add_text("2/F", dxfattribs={"insert": (x + 500, 400), "height": 5})
        # Never assign unrelated geometry to the nearest title outside its frame.
        model.add_line((-2000, 400), (-1500, 400),
                       dxfattribs={"layer": "0-TG-Pipe(Manifold)"})
        doc.saveas(path)
        floors = filter_schematic_floors(
            parse_cad_file({"path": str(path), "name": path.name}, [], []),
            ["G/F", "1/F", "2/F", "3/F"],
        )
        assert [floor["floor"] for floor in floors] == ["G/F", "1/F", "2/F", "3/F"]
        assert [len(floor["pipes"]) for floor in floors] == [1, 1, 1, 1]
        assert all(floor["floorDetection"]["method"] == "cad-plan-title-frame" for floor in floors)
        assert all("60/F" not in floor["floorDetection"]["detectedLabels"] for floor in floors)
        for floor in floors:
            floor["_cadPreviewLimited"] = True
        attach_networks(floors)
        previews = build_review_previews(floors)
        assert len(previews) == 4
        for preview in previews:
            svg = base64.b64decode(preview["base64"]).decode("utf-8")
            ElementTree.fromstring(svg)
            assert "未顯示建築背景" in svg
            assert len(svg) < 10_000


def test_cross_frame_pipes_are_not_silently_counted():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "crossing.dxf"
        doc = ezdxf.new()
        model = doc.modelspace()
        model.add_lwpolyline([(0, 0), (1000, 0), (1000, 800), (0, 800)], close=True)
        model.add_text("GROUND FLOOR PLAN", dxfattribs={"insert": (100, 10), "height": 10})
        model.add_line((900, 400), (1050, 400), dxfattribs={"layer": "TG-PIPE"})
        doc.saveas(path)
        floors = parse_cad_file({"path": str(path), "name": path.name}, [], [])
        assert floors[0]["pipes"] == []
        attach_networks(floors)
        assert floors[0]["network"]["status"] == "needs_review"
        assert any("跨出圖框" in warning for warning in floors[0]["warnings"])


test_title_normalization()
test_titles_frames_and_attributes_override_incidental_floor_notes()
test_cross_frame_pipes_are_not_silently_counted()
print("CAD floor-region tests passed")
