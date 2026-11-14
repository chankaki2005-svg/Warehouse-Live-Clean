import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import ezdxf

sys.path.insert(0, str(Path(__file__).parent))
import cad_editor
from cad_editor import fitting_candidates


class CadFittingReviewTest(unittest.TestCase):
    def test_fast_inspection_does_not_recover_rejected_writer_output(self):
        with patch.object(cad_editor.ezdxf, "readfile", side_effect=ValueError("broken BLOCKS")), \
                patch.object(cad_editor, "read_document_for_inspection") as recovery_reader:
            with self.assertRaisesRegex(ValueError, "broken BLOCKS"):
                cad_editor.inspect("candidate.dxf", fast_only=True)
        recovery_reader.assert_not_called()

    def test_fast_validation_does_not_recover_rejected_writer_output(self):
        with patch.object(cad_editor.ezdxf, "readfile", side_effect=ValueError("broken BLOCKS")), \
                patch.object(cad_editor, "read_document_for_inspection") as recovery_reader:
            with self.assertRaisesRegex(ValueError, "broken BLOCKS"):
                cad_editor.validate("candidate.dxf", fast_only=True)
        recovery_reader.assert_not_called()

    def test_fast_validation_requires_nonempty_modelspace(self):
        doc = ezdxf.new("R2010")
        with patch.object(cad_editor.ezdxf, "readfile", return_value=doc):
            with self.assertRaisesRegex(ValueError, "Model Space"):
                cad_editor.validate("empty.dxf", fast_only=True)
            doc.modelspace().add_line((0, 0), (1, 1))
            self.assertEqual(cad_editor.validate("valid.dxf", fast_only=True), {"entityCount": 1})

    def test_inspection_uses_normal_reader_for_valid_drawings(self):
        doc = ezdxf.new("R2010")
        with patch.object(cad_editor.ezdxf, "readfile", return_value=doc) as fast_reader, \
                patch.object(cad_editor, "read_document") as recovery_reader:
            self.assertIs(cad_editor.read_document_for_inspection("valid.dxf"), doc)
        fast_reader.assert_called_once_with("valid.dxf")
        recovery_reader.assert_not_called()

    def test_inspection_falls_back_to_recovery_reader_for_malformed_drawings(self):
        doc = ezdxf.new("R2010")
        with patch.object(cad_editor.ezdxf, "readfile", side_effect=ValueError("invalid")) as fast_reader, \
                patch.object(cad_editor, "read_document", return_value=doc) as recovery_reader:
            self.assertIs(cad_editor.read_document_for_inspection("damaged.dxf"), doc)
        fast_reader.assert_called_once_with("damaged.dxf")
        recovery_reader.assert_called_once_with("damaged.dxf")

    def test_classifies_explicit_blocks_and_nested_tee_without_counting_other_blocks(self):
        doc = ezdxf.new("R2010")
        model = doc.modelspace()
        doc.layers.new("TG-PIPE")
        doc.layers.new("TG")
        doc.layers.new("LWK-LINE-EDGE")

        valve = doc.blocks.new("80 G.V.END", base_point=(100, 200))
        valve.add_line((100, 200), (120, 220))
        model.add_blockref("80 G.V.END", (1000, 2000), dxfattribs={"layer": "TG-PIPE"})

        tee = doc.blocks.new("1234")
        tee.add_line((0, 0), (10, 10), dxfattribs={"layer": "TG - Dia.100mm TEE"})
        model.add_blockref("1234", (5000, 6000), dxfattribs={"layer": "TG"})

        generic = doc.blocks.new("TG_MISC")
        generic.add_line((0, 0), (10, 10))
        model.add_blockref("TG_MISC", (8000, 9000), dxfattribs={"layer": "TG"})

        nested_false_positive = doc.blocks.new("Unrelated cabinet")
        nested_false_positive.add_blockref("80 G.V.END", (0, 0))
        model.add_blockref("Unrelated cabinet", (20000, 30000), dxfattribs={"layer": "LWK-LINE-EDGE"})

        candidates = fitting_candidates(doc, list(model))
        by_name = {candidate["blockName"]: candidate for candidate in candidates}
        self.assertEqual(len(candidates), 3)
        self.assertEqual(by_name["80 G.V.END"]["category"], "GV")
        self.assertEqual(by_name["80 G.V.END"]["nominalSizeMm"], 80)
        self.assertEqual(by_name["80 G.V.END"]["anchorMethod"], "transformed-block-bounds")
        self.assertEqual(by_name["80 G.V.END"]["x"], 1010)
        self.assertEqual(by_name["80 G.V.END"]["y"], 2010)
        self.assertEqual(by_name["1234"]["category"], "TEE")
        self.assertEqual(by_name["1234"]["nominalSizeMm"], 100)
        self.assertEqual(by_name["TG_MISC"]["category"], "UNKNOWN")
        self.assertEqual(by_name["TG_MISC"]["id"], "C001")

    def test_candidate_ids_are_stable_in_spatial_order(self):
        doc = ezdxf.new("R2010")
        model = doc.modelspace()
        doc.layers.new("TG-PIPE")
        for name, x in (("80ELBOW", 20), ("80 UNION", 10)):
            block = doc.blocks.new(name)
            block.add_circle((0, 0), 1)
            model.add_blockref(name, (x, 0), dxfattribs={"layer": "TG-PIPE"})
        first = fitting_candidates(doc, list(model))
        second = fitting_candidates(doc, list(model))
        self.assertEqual([(row["id"], row["blockName"]) for row in first],
                         [(row["id"], row["blockName"]) for row in second])
        self.assertEqual([row["id"] for row in first], ["C001", "C002"])
        self.assertEqual(first[0]["blockName"], "80 UNION")

    def test_takeoff_measures_line_and_bulged_polyline_geometry_and_units(self):
        doc = ezdxf.new("R2010")
        doc.header["$INSUNITS"] = ezdxf.units.MM
        model = doc.modelspace()
        model.add_line((0, 0), (3, 4), dxfattribs={"layer": "TG-PIPE"})
        model.add_lwpolyline(
            [(0, 0, 1), (2, 0, 0)],
            format="xyb",
            dxfattribs={"layer": "TG-PIPE"},
        )
        self.assertAlmostEqual(cad_editor.entity_measure_length(model[0]), 5)
        self.assertAlmostEqual(cad_editor.entity_measure_length(model[1]), 3.141592653589793)
        self.assertEqual(cad_editor.drawing_unit_info(doc), (ezdxf.units.MM, "Millimeters", 1.0))

    def test_takeoff_unknown_unit_is_not_silently_converted(self):
        doc = ezdxf.new("R2010")
        doc.header["$INSUNITS"] = 0
        self.assertEqual(cad_editor.drawing_unit_info(doc), (0, "未指定", None))


if __name__ == "__main__":
    unittest.main()
