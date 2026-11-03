import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("onboarding_funnel", ROOT / "scripts" / "onboarding_funnel.py")
funnel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(funnel)

ART = {"revision_id": "art", "key": "k", "sha256": "0" * 64, "width": 512, "height": 512}


def _manifest(layout, headings, *, art=(), generated=()):
    return {
        "sprite_layout": layout,
        "views": [
            {"view_id": heading or "single", "heading": heading, "setup_version": 1, "name": heading or "Original view",
             "reference": ART if (heading or "single") in art else None}
            for heading in headings
        ],
        "motions": [
            {"specification": {"view_id": view_id}, "candidates": [{"candidate_id": "c"}]} for view_id in generated
        ],
    }


EIGHT = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def test_templates_are_inferred_from_the_first_setup():
    assert funnel.template_for(_manifest("single_view", [None])) == "single"
    assert funnel.template_for(_manifest("side_view", ["left", "right"])) == "side"
    assert funnel.template_for(_manifest("isometric", ["N", "E", "S", "W"])) == "four"
    assert funnel.template_for(_manifest("isometric", EIGHT)) == "eight"
    assert funnel.template_for(_manifest("isometric", ["NE"])) == "compass_edited"
    # A legacy project with views but no onboarding setup is not counted.
    assert funnel.template_for({"sprite_layout": "side_view", "views": [{"view_id": "left", "name": "Left"}]}) is None


def test_only_setups_applied_to_a_fresh_project_count_as_onboarding():
    empty = {"subject": {"revisions": []}, "motions": []}
    assert funnel.came_from_onboarding(empty, 2)
    # A layout set first, then setup applied from the workspace editor.
    assert not funnel.came_from_onboarding(empty, 3)
    # A project created around an existing subject or motions.
    assert not funnel.came_from_onboarding({"subject": {"revisions": [{"revision_id": "s"}]}}, 2)
    assert not funnel.came_from_onboarding({"motions": [{"specification": {}}]}, 2)
    assert not funnel.came_from_onboarding({"sprite_layout": "side_view"}, 2)


def test_an_eight_direction_project_stuck_on_its_uploaded_direction_is_visible():
    first = _manifest("isometric", EIGHT, art={"S"})
    current = _manifest("isometric", EIGHT, art={"S"}, generated={"S"})
    summary = funnel.summarize([(first, current, False)])["eight"]
    assert summary == {
        "projects": 1,
        "directions": 8,
        "directions_with_art": 1,
        "every_direction_has_art": 0,
        "generated_any": 1,
        "generated_beyond_one_direction": 0,
        "released": 0,
    }


def test_a_completed_side_project_reaches_every_stage():
    first = _manifest("side_view", ["left", "right"], art={"left"})
    current = _manifest("side_view", ["left", "right"], art={"left", "right"}, generated={"left", "right"})
    summary = funnel.summarize([(first, current, True)])
    assert summary["side"]["every_direction_has_art"] == 1
    assert summary["side"]["generated_beyond_one_direction"] == 1
    assert summary["side"]["released"] == 1
    assert summary["eight"]["projects"] == 0


def test_main_requires_a_database_url(monkeypatch, capsys):
    monkeypatch.delenv(funnel.DATABASE_URL_VARIABLE, raising=False)
    assert funnel.main([]) == 2
    assert funnel.DATABASE_URL_VARIABLE in capsys.readouterr().err
