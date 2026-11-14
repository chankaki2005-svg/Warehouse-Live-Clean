import importlib.util
import tempfile
from pathlib import Path

module_path = Path(__file__).with_name("build_schematic.py")
spec = importlib.util.spec_from_file_location("build_schematic", module_path)
schematic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schematic)


def test_keeps_only_ground_to_third_and_preserves_order():
    floors = [{"floor": name} for name in ("B1", "G/F", "1/F", "2/F", "3/F", "4/F", "28/F")]
    result = schematic.filter_schematic_floors(floors, ["G/F", "1/F", "2/F", "3/F"])
    assert [item["floor"] for item in result] == ["G/F", "1/F", "2/F", "3/F"]


def test_reports_missing_requested_floor_instead_of_silently_omitting():
    floors = [{"floor": name} for name in ("G/F", "1/F", "3/F", "28/F")]
    try:
        schematic.filter_schematic_floors(floors, ["G/F", "1/F", "2/F", "3/F"])
    except RuntimeError as error:
        assert "缺少樓層：2/F" in str(error)
    else:
        raise AssertionError("missing floor must fail explicitly")


def test_empty_filter_keeps_every_uploaded_floor():
    floors = [{"floor": "G/F"}, {"floor": "28/F"}]
    assert schematic.filter_schematic_floors(floors, []) == floors


def test_pdf_plan_title_beats_incidental_roof_and_basement_mentions():
    assert schematic.pdf_page_floor(
        "source.pdf", 3, 18,
        "ROOF ACCESS NOTE; 1ST FLOOR PLAN; BASEMENT 1 REFERENCE",
    ) == "1/F"
    assert schematic.pdf_page_floor(
        "source.pdf", 4, 18,
        "GROUND FLOOR PLAN; ROOF DRAINAGE NOTE; BASEMENT 1 REFERENCE",
    ) == "G/F"
    assert schematic.pdf_page_floor(
        "source.pdf", 5, 18,
        "2ND FLOOR PLAN; ROOF ACCESS NOTE",
    ) == "2/F"


def test_pdf_highlight_scanner_returns_empty_mask_when_no_highlights_exist():
    document = schematic.pymupdf.open()
    try:
        page = document.new_page(width=120, height=120)
        chains, component_count, mask = schematic._pdf_highlight_skeleton(page, scale=0.1)
        assert chains == []
        assert component_count == 0
        assert mask == set()
    finally:
        document.close()


def test_autocad_tg_pipe_layer_overrides_configured_layer_names():
    assert schematic.pdf_pipe_layer_priority("0-TG-Pipe(Manifold)", ["TG-PIPE DN50"]) == 30
    assert schematic.pdf_pipe_layer_priority("0-TG-Valve", ["TG-PIPE DN50"]) == 0


def test_stair_riser_labels_do_not_become_pipe_evidence():
    assert schematic.classify_pdf_marker("UP 28R DN 26R") is None
    assert schematic.classify_pdf_marker("36R UP ST-6") is None
    assert schematic.classify_pdf_marker("UP") is None
    assert schematic.marker_diameter("DN26R") is None
    assert schematic.classify_pdf_marker("BUILDING LINE ABOVE (4/F)") is None
    assert schematic.classify_pdf_marker("GOVERNMENT ACCOMMODATION (SUPPORTED HOSTEL)") is None
    assert schematic.classify_pdf_marker("2 NOS. OF SPRINKLER INLET & 4 NOS. OF F.S. INLET") is None


def test_pdf_towngas_pipe_diameter_markers_are_retained():
    assert schematic.marker_diameter("DN25") == 25
    assert schematic.marker_diameter("DN25R") is None
    assert schematic.marker_diameter("Ø100 F/B & T/A") == 100
    assert schematic.marker_diameter("& 2 DRAIN OUTLETS CHANNEL W/. COVER 225mm SURFACE") is None
    assert schematic.marker_diameter("GAS PIPE 25mm") == 25
    assert schematic.classify_pdf_marker("Ø100 F/B & T/A") == "down"
    markers = schematic.pdf_engineering_markers(
        [{"text": "Ø100 F/B & T/A", "bbox": [0, 0, 10, 10]}], "2/F"
    )
    assert {marker["kind"] for marker in markers} == {"down", "up"}


def test_explicit_pdf_pipe_layer_skips_raster_and_architecture():
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source.pdf"
        document = schematic.pymupdf.open()
        page = document.new_page(width=600, height=600)
        page.insert_text((30, 30), "GROUND FLOOR PLAN")
        pipe_layer = document.add_ocg("0-TG-Pipe(Manifold)")
        page.draw_line((100, 100), (200, 100), oc=pipe_layer)
        page.draw_line((100, 150), (400, 150))
        page.draw_rect((250, 250, 400, 260), fill=(0.459, 0.733, 0.247))
        document.save(source)
        document.close()
        original = schematic._pdf_highlight_skeleton

        def unexpected_raster(*args, **kwargs):
            raise AssertionError("Named pipe layers must bypass raster scanning")

        schematic._pdf_highlight_skeleton = unexpected_raster
        try:
            floors = schematic.parse_pdf_file(
                {"path": str(source), "name": "source.pdf"}, ["TG-PIPE DN50"], ["G/F"]
            )
        finally:
            schematic._pdf_highlight_skeleton = original
        assert len(floors) == 1
        assert len(floors[0]["pipes"]) == 1
        assert floors[0]["pipes"][0]["sourceType"] == "PDF_TG_PIPE_LAYER"
        assert floors[0]["pipes"][0]["length"] == 100


test_keeps_only_ground_to_third_and_preserves_order()
test_reports_missing_requested_floor_instead_of_silently_omitting()
test_empty_filter_keeps_every_uploaded_floor()
test_pdf_plan_title_beats_incidental_roof_and_basement_mentions()
test_pdf_highlight_scanner_returns_empty_mask_when_no_highlights_exist()
test_autocad_tg_pipe_layer_overrides_configured_layer_names()
test_stair_riser_labels_do_not_become_pipe_evidence()
test_pdf_towngas_pipe_diameter_markers_are_retained()
test_explicit_pdf_pipe_layer_skips_raster_and_architecture()
print("Schematic floor filter tests passed")
