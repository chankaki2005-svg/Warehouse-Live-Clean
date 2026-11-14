import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import ezdxf

from dwf_to_dxf import convert_entity


class Point:
    def __init__(self, x, y):
        self.x, self.y = x, y


class Entity:
    def __init__(self, kind, points=(), **kwargs):
        self.kind = kind
        self.points = points
        self.style = SimpleNamespace(layer_name="A", layer_number=None, color=(1, 2, 3, 255),
                                     font_height=2, font_rotation_degrees=0)
        self.layer = "A"
        for key, value in kwargs.items():
            setattr(self, key, value)


class DwfToDxfTests(unittest.TestCase):
    def setUp(self):
        self.doc = ezdxf.new()
        self.layers = set()

    def test_supported_entities_preserve_coordinates_and_style(self):
        space = self.doc.modelspace()
        line = Entity("LINE", [Point(10, 20), Point(30, 40)])
        self.assertEqual(convert_entity(space, line, self.layers), "LINE")
        circle = Entity("CIRCLE", center=Point(5, 6), x_axis=Point(3, 0), y_axis=Point(0, 3))
        self.assertEqual(convert_entity(space, circle, self.layers), "CIRCLE")
        text = Entity("TEXT", [Point(7, 8)], text="Label")
        self.assertEqual(convert_entity(space, text, self.layers), "TEXT")
        self.assertEqual(tuple(space[0].dxf.start), (10, 20, 0))
        self.assertEqual(tuple(space[1].dxf.center), (5, 6, 0))
        self.assertEqual(space[2].dxf.text, "Label")
        self.assertEqual(space[0].dxf.layer, "A")

    def test_unsupported_entity_is_reported(self):
        self.assertIsNone(convert_entity(self.doc.modelspace(), Entity("IMAGE"), self.layers))

    def test_triangle_and_rotated_arc_preserve_vector_geometry(self):
        space = self.doc.modelspace()
        triangle = Entity("POLYTRIANGLE", [Point(0, 0), Point(2, 0), Point(1, 2)])
        self.assertEqual(convert_entity(space, triangle, self.layers), "POLYLINE")
        arc = Entity("ARC", center=Point(0, 0), x_axis=Point(0, 2),
                     y_axis=Point(-2, 0), start_angle_degrees=0, end_angle_degrees=90)
        self.assertEqual(convert_entity(space, arc, self.layers), "ARC")
        self.assertAlmostEqual(space[1].dxf.start_angle, 90)
        self.assertAlmostEqual(space[1].dxf.end_angle, 180)

    def test_triangle_strip_becomes_independent_triangle_outlines(self):
        space = self.doc.modelspace()
        strip = Entity("POLYTRIANGLE", [
            Point(0, 0), Point(2, 0), Point(0, 2), Point(2, 2),
        ])
        self.assertEqual(convert_entity(space, strip, self.layers), "POLYLINE")
        self.assertEqual(len(space), 2)
        vertices = [
            [tuple(point[:2]) for point in entity.get_points("xy")]
            for entity in space
        ]
        self.assertEqual(vertices, [
            [(0, 0), (2, 0), (0, 2)],
            [(2, 0), (0, 2), (2, 2)],
        ])
        self.assertTrue(all(entity.closed for entity in space))

    def test_triangle_strip_expansion_respects_entity_budget(self):
        space = self.doc.modelspace()
        strip = Entity("POLYTRIANGLE", [Point(0, 0), Point(2, 0), Point(0, 2), Point(2, 2)])
        self.assertIsNone(convert_entity(space, strip, self.layers, max_output_entities=1))
        self.assertEqual(len(space), 0)

    def test_skewed_arc_is_not_converted_as_a_circle(self):
        arc = Entity("ARC", center=Point(0, 0), x_axis=Point(2, 0),
                     y_axis=Point(0, 3), start_angle_degrees=0, end_angle_degrees=90)
        self.assertIsNone(convert_entity(self.doc.modelspace(), arc, self.layers))

    def test_bezier_geometry_is_reported_instead_of_replaced_by_chords(self):
        bezier = Entity("POLYBEZIER", [Point(0, 0), Point(1, 1), Point(2, 0), Point(3, 1)])
        self.assertIsNone(convert_entity(self.doc.modelspace(), bezier, self.layers))

        figure = SimpleNamespace(
            start=Point(0, 0),
            closed=False,
            segments=[SimpleNamespace(kind="cubic_bezier", end=Point(3, 0))],
        )
        path = Entity("PATH", path=[figure])
        self.assertIsNone(convert_entity(self.doc.modelspace(), path, self.layers))

    def test_available_fixture_can_be_parsed_when_present(self):
        fixture = next(Path("attached_assets").glob("*.dwf"), None)
        if fixture is None:
            self.skipTest("no attached DWF fixture")
        from dwf_to_dxf import convert
        with tempfile.TemporaryDirectory() as directory:
            result = convert(fixture, Path(directory) / "out.dxf")
            self.assertEqual(result["sheets"][0]["skippedEntityCount"], 0)
            self.assertTrue(Path(directory, "out.dxf").is_file())


if __name__ == "__main__":
    unittest.main()