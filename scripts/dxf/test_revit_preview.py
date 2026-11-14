import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET
import ezdxf
from cad_editor import revit_dwg_dimensions, revit_preview
from revit_geometry_measurements import measure_orthographic_box


class RevitPreviewTest(unittest.TestCase):
    def box_views(self, scale=1):
        doc = ezdxf.new("R2000")
        m = doc.modelspace()
        def rectangle(x, y, w, h):
            m.add_lwpolyline([(x*scale,y*scale),((x+w)*scale,y*scale),
                              ((x+w)*scale,(y+h)*scale),(x*scale,(y+h)*scale)], close=True)
        rectangle(0, 320, 527, 403)
        rectangle(0, 0, 527, 231)
        rectangle(620, 0, 403, 231)
        for y in (100, 500):
            rectangle(527, y, 20, 100)
            rectangle(547, y+20, 20, 60)
        return doc, m

    def test_separate_views_and_chained_protrusions_are_measured_not_sheet_extents(self):
        doc, m = self.box_views()
        orphan = doc.blocks.new("*D99")
        orphan.add_mtext("99999")
        result = measure_orthographic_box(list(m), 1)
        self.assertEqual(result["bodyEnvelope"], {"lengthMm": 527, "widthMm": 231, "heightMm": 403})
        self.assertEqual(result["overallEnvelope"], {"lengthMm": 567, "widthMm": 231, "heightMm": 403})
        self.assertEqual(len(result["views"]), 3)

    def test_unknown_units_and_ambiguous_views_are_not_guessed(self):
        _, m = self.box_views()
        self.assertIsNone(measure_orthographic_box(list(m), None))
        m.add_lwpolyline([(1200, 0), (1603, 0), (1603, 231), (1200, 231)], close=True)
        self.assertIsNone(measure_orthographic_box(list(m), 1))

    def test_units_are_converted_and_oversized_connected_lines_fail_closed(self):
        _, m = self.box_views(scale=1/25.4)
        result = measure_orthographic_box(list(m), 25.4)
        self.assertEqual(result["overallEnvelope"]["lengthMm"], 567)
        _, m = self.box_views()
        m.add_line((0, 320), (-1000, 320))
        self.assertIsNone(measure_orthographic_box(list(m), 1))

    def test_non_matching_projections_do_not_claim_complete_protrusion_dimensions(self):
        _, m = self.box_views()
        m.add_lwpolyline([(567, 525), (577, 525), (577, 545), (567, 545)], close=True)
        self.assertIsNone(measure_orthographic_box(list(m), 1))

    def test_complete_preview_keeps_top_level_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lines.dxf"
            document = ezdxf.new("R2000")
            model = document.modelspace()
            model.add_line((0, 0), (1000, 500))
            model.add_lwpolyline([(0, 0), (0, 500), (1000, 500)])
            document.saveas(path)
            preview = revit_preview(str(path))
            self.assertTrue(preview["fullPreviewAvailable"])
            root = ET.fromstring(preview["svg"])
            paths = root.findall(".//{http://www.w3.org/2000/svg}path")
            self.assertGreaterEqual(len(paths), 2, "Editor overlays must not disappear from the Revit source image")

    def test_dwg_dimension_values_are_cross_checked_against_cad_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dimensions.dxf"
            document = ezdxf.new("R2000")
            document.header["$INSUNITS"] = 4
            model = document.modelspace()
            model.add_line((0, 0), (100, 0))
            first = model.add_linear_dim(base=(0, 20), p1=(0, 0), p2=(100, 0)).render()
            first.dimension.dxf.text = "100 mm"
            second = model.add_linear_dim(base=(0, 40), p1=(0, 0), p2=(50, 0)).render()
            second.dimension.dxf.text = "70 mm"
            third = model.add_linear_dim(base=(0, 60), p1=(0, 0), p2=(80, 0)).render()
            third.dimension.dxf.text = "Overall=80"
            document.saveas(path)

            result = revit_dwg_dimensions(str(path))
            self.assertEqual(result["unitName"], "Millimeters")
            self.assertEqual(result["unitToMm"], 1.0)
            self.assertEqual(len(result["dimensions"]), 3)
            self.assertEqual(result["dimensions"][0]["measurementMm"], 100.0)
            self.assertTrue(result["dimensions"][0]["consistentWithGeometry"])
            self.assertFalse(result["dimensions"][1]["consistentWithGeometry"])
            self.assertIsNone(result["dimensions"][2]["consistentWithGeometry"])

    def test_dwg_with_unknown_units_is_not_converted_to_millimetres(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unitless.dxf"
            document = ezdxf.new("R2000")
            document.header["$INSUNITS"] = 0
            model = document.modelspace()
            model.add_linear_dim(base=(0, 20), p1=(0, 0), p2=(100, 0)).render()
            document.saveas(path)

            result = revit_dwg_dimensions(str(path))
            self.assertIsNone(result["unitToMm"])
            self.assertIsNone(result["dimensions"][0]["measurementMm"])


if __name__ == "__main__":
    unittest.main()
