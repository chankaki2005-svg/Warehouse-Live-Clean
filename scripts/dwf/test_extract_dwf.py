import tempfile
import unittest
from pathlib import Path

from extract_dwf import (
    extract_sheet_geometry,
    geometry_compatible_source,
    legacy_dwf_preview,
    legacy_dwf_text,
    normalize_uniform_quarter_turn_text,
    preview_swipe_bridge,
)


class FakePoint:
    def __init__(self, x: float, y: float) -> None:
        self.x = x
        self.y = y


class FakeEntity:
    def __init__(
        self,
        kind: str,
        bounds: tuple[float, float, float, float],
        points: list[FakePoint] | None = None,
        closed: bool = False,
    ) -> None:
        self.kind = kind
        self._bounds = bounds
        self.points = points or []
        self.closed = closed
        self.center = FakePoint(
            (bounds[0] + bounds[2]) / 2,
            (bounds[1] + bounds[3]) / 2,
        ) if kind == "CIRCLE" else None

    def bbox(self) -> tuple[float, float, float, float]:
        return self._bounds


class FakeSheet:
    def __init__(self, units: str | None) -> None:
        self.title = "Equipment plan"
        self.name = "Sheet 1"
        self.units = units
        self.content_bounds = (0.0, 0.0, 100.0, 200.0)
        self.paper_bounds = None
        self.all_entities = [
            FakeEntity("CIRCLE", (10.0, 20.0, 30.0, 40.0), closed=True),
            FakeEntity(
                "POLYLINE",
                (50.0, 60.0, 70.0, 100.0),
                [
                    FakePoint(50.0, 60.0),
                    FakePoint(70.0, 60.0),
                    FakePoint(70.0, 100.0),
                    FakePoint(50.0, 100.0),
                    FakePoint(50.0, 60.0),
                ],
            ),
            FakeEntity(
                "POLYLINE",
                (0.0, 0.0, 100.0, 200.0),
                [
                    FakePoint(0.0, 0.0),
                    FakePoint(100.0, 0.0),
                    FakePoint(100.0, 200.0),
                    FakePoint(0.0, 200.0),
                    FakePoint(0.0, 0.0),
                ],
            ),
        ]

    def bbox(self) -> tuple[float, float, float, float]:
        return self.content_bounds


class RevitGeometryExtractionTests(unittest.TestCase):
    def test_normalizes_closed_geometry_and_rejects_page_border(self) -> None:
        result = extract_sheet_geometry(FakeSheet("mm"), 1)

        self.assertEqual(result["unitScaleMm"], 1.0)
        self.assertEqual(result["candidateCount"], 2)
        self.assertEqual(
            {component["kind"] for component in result["components"]},
            {"circle", "rect"},
        )
        circle = next(component for component in result["components"] if component["kind"] == "circle")
        self.assertAlmostEqual(circle["centerX"], 0.2)
        self.assertAlmostEqual(circle["centerY"], 0.15)

    def test_unknown_units_do_not_claim_millimetres(self) -> None:
        result = extract_sheet_geometry(FakeSheet(None), 1)
        self.assertIsNone(result["unitScaleMm"])

    def test_standalone_full_bounds_circle_is_retained(self) -> None:
        sheet = FakeSheet(None)
        sheet.content_bounds = (0.0, 0.0, 100.0, 100.0)
        sheet.all_entities = [FakeEntity("CIRCLE", (0.0, 0.0, 100.0, 100.0), closed=True)]
        result = extract_sheet_geometry(sheet, 1)
        self.assertEqual(result["candidateCount"], 1)
        self.assertEqual(result["components"][0]["kind"], "circle")

    def test_standalone_full_bounds_rectangle_is_retained(self) -> None:
        sheet = FakeSheet(None)
        sheet.content_bounds = (0.0, 0.0, 100.0, 100.0)
        sheet.all_entities = [
            FakeEntity(
                "POLYLINE",
                (0.0, 0.0, 100.0, 100.0),
                [
                    FakePoint(0.0, 0.0),
                    FakePoint(100.0, 0.0),
                    FakePoint(100.0, 100.0),
                    FakePoint(0.0, 100.0),
                    FakePoint(0.0, 0.0),
                ],
            ),
        ]
        result = extract_sheet_geometry(sheet, 1)
        self.assertEqual(result["candidateCount"], 1)
        self.assertEqual(result["components"][0]["kind"], "rect")

    def test_rotated_or_concave_polyline_is_not_invented_as_rectangle(self) -> None:
        sheet = FakeSheet(None)
        sheet.all_entities = [
            FakeEntity(
                "POLYLINE",
                (10.0, 10.0, 50.0, 50.0),
                [
                    FakePoint(30.0, 10.0),
                    FakePoint(50.0, 30.0),
                    FakePoint(30.0, 50.0),
                    FakePoint(10.0, 30.0),
                    FakePoint(30.0, 10.0),
                ],
            ),
        ]
        result = extract_sheet_geometry(sheet, 1)
        self.assertEqual(result["candidateCount"], 0)

    def test_closed_rectangle_without_repeated_start_is_retained(self) -> None:
        sheet = FakeSheet(None)
        sheet.all_entities = [
            FakeEntity(
                "POLYLINE",
                (10.0, 10.0, 50.0, 50.0),
                [
                    FakePoint(10.0, 10.0),
                    FakePoint(50.0, 10.0),
                    FakePoint(50.0, 50.0),
                    FakePoint(10.0, 50.0),
                ],
                closed=True,
            ),
        ]
        result = extract_sheet_geometry(sheet, 1)
        self.assertEqual(result["candidateCount"], 1)


class LegacyDwfCompatibilityTests(unittest.TestCase):
    def write_dwf(self, content: bytes) -> Path:
        handle = tempfile.NamedTemporaryFile(suffix=".dwf", delete=False)
        handle.write(content)
        handle.close()
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return Path(handle.name)

    def test_accepts_valid_v0034_whip_stream(self) -> None:
        source = self.write_dwf(
            b"(DWF V00.34)\x00TS076-11\x00Drawing title\x00(EndOfDWF)\r\n",
        )

        parsed = legacy_dwf_text(source, 10_000)

        self.assertIsNotNone(parsed)
        version, strings = parsed or ("", [])
        self.assertEqual(version, "V00.34")
        self.assertIn("TS076-11", strings)
        self.assertIn("V00.34", legacy_dwf_preview(version, strings))

    def test_v0030_geometry_uses_compatible_decoder_without_changing_size(self) -> None:
        source = self.write_dwf(
            b"(DWF V00.30)L 0,0 5,5\n(EndOfDWF)",
        )

        compatible = geometry_compatible_source(source)

        self.assertIsInstance(compatible, bytes)
        self.assertEqual(len(compatible), source.stat().st_size)
        self.assertTrue(bytes(compatible).startswith(b"(DWF V00.34)"))
        self.assertTrue(bytes(compatible).endswith(b"(EndOfDWF)"))

    def test_truncated_v0030_is_not_rewritten(self) -> None:
        source = self.write_dwf(b"(DWF V00.30)L 0,0 5,5")
        self.assertEqual(geometry_compatible_source(source), source)

    def test_legacy_preview_includes_swipe_bridge(self) -> None:
        preview = legacy_dwf_preview("V00.34", ["Drawing title"])
        self.assertIn("reference-preview-swipe", preview)
        self.assertIn("script-src 'unsafe-inline'", preview)
        self.assertIn(preview_swipe_bridge(), preview)

    def test_rejects_missing_end_marker(self) -> None:
        source = self.write_dwf(b"(DWF V00.34)\x00TS076-11")
        self.assertIsNone(legacy_dwf_text(source, 10_000))

    def test_rejects_non_legacy_header(self) -> None:
        source = self.write_dwf(b"(DWF V06.00)\x00TS076-11\x00(EndOfDWF)")
        self.assertIsNone(legacy_dwf_text(source, 10_000))


class SvgTextRotationTests(unittest.TestCase):
    def test_uniform_quarter_turn_text_is_normalized(self) -> None:
        texts = "".join(
            f'<text x="{index}" y="{index}" transform="rotate(-90 {index} {index})">Label {index}</text>'
            for index in range(8)
        )
        normalized = normalize_uniform_quarter_turn_text(f"<svg>{texts}</svg>")
        self.assertNotIn("rotate(-90", normalized)
        self.assertEqual(normalized.count("<text"), 8)

    def test_mixed_text_rotations_are_preserved(self) -> None:
        texts = "".join(
            f'<text transform="rotate({-90 if index < 7 else 0} {index} {index})">Label {index}</text>'
            for index in range(8)
        )
        svg = f"<svg>{texts}</svg>"
        self.assertEqual(normalize_uniform_quarter_turn_text(svg), svg)

    def test_small_vertical_label_set_is_preserved(self) -> None:
        texts = "".join(
            f'<text transform="rotate(-90 {index} {index})">Label {index}</text>'
            for index in range(3)
        )
        svg = f"<svg>{texts}</svg>"
        self.assertEqual(normalize_uniform_quarter_turn_text(svg), svg)


if __name__ == "__main__":
    unittest.main()