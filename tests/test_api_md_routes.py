"""Every route the API document names is one the application serves."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from fastapi.routing import APIRoute

sys.path.insert(0, str(Path(__file__).parent))
from test_api_reference import full_app

DOCUMENT = Path(__file__).resolve().parents[1] / "src/magic_video_editor/api.md"
# `POST /studio/movies/{id}/build`, or shortened, `POST …/build`.
MENTION = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/studio/[^\s`)?]*|…/[^\s`)?]*)")


# Example values the document puts where a route takes a parameter. Anything else
# that isn't ID-like must be one of the route's own words, so a static route such as
# `/studio/movies/capabilities` can't pass by matching `/studio/movies/{id}`.
EXAMPLE_VALUES = {"a", "idle", "higgsfield"}


def _example_value(segment: str) -> bool:
    return segment in EXAMPLE_VALUES or any(c.isdigit() for c in segment) or "…" in segment


def _matches(named: list[str], route: list[str], *, concrete: bool) -> bool:
    """Segment by segment. A named `{param}` matches a route parameter. In a full path a
    route parameter also takes an example value such as `s1` or a UUID; a shortened path
    names only placeholders. A static segment always needs an exact match."""
    def same(a: str, b: str) -> bool:
        if b.startswith("{"):
            return a.startswith("{") or (concrete and _example_value(a))
        return a == b
    return len(named) == len(route) and all(same(a, b) for a, b in zip(named, route))


def _named_routes(text: str) -> list[tuple[str, str]]:
    return [(method, path.rstrip(".,;:")) for method, path in MENTION.findall(text)]


def _missing(text: str) -> list[str]:
    routes = [(method, route.path_format.strip("/").split("/"))
              for route in full_app().routes if isinstance(route, APIRoute) for method in route.methods]
    missing = []
    for method, path in _named_routes(text):
        if path.startswith("…/"):
            tail = path[2:].split("/")
            found = any(m == method and _matches(tail, segments[-len(tail):], concrete=False) for m, segments in routes)
        else:
            found = any(m == method and _matches(path.strip("/").split("/"), segments, concrete=True) for m, segments in routes)
        if not found:
            missing.append(f"{method} {path}")
    return missing


def test_every_route_api_md_names_exists() -> None:
    assert _missing(DOCUMENT.read_text()) == []


def test_the_check_catches_a_route_that_does_not_exist() -> None:
    assert _missing("`POST /studio/movies/{id}/publish` and `PUT …/legs/{leg_id}/rename`") == [
        "POST /studio/movies/{id}/publish",
        "PUT …/legs/{leg_id}/rename",
    ]
    assert _missing("`POST /studio/scene-flights/{flight_id}/legs/a/select` and `GET …/releases/{release_id}/download`") == []
    # A misspelled static route can't hide behind a parameter.
    assert _missing("`GET /studio/movies/capabilites`") == ["GET /studio/movies/capabilites"]


def test_the_writes_without_a_revision_are_exactly_the_ones_api_md_lists() -> None:
    """Derived from the request models, so a new unrevisioned movie or flight write
    fails here until the document lists it."""
    unrevisioned = set()
    for route in full_app().routes:
        if not isinstance(route, APIRoute) or not route.path_format.startswith(("/studio/movies", "/studio/scene-flights")):
            continue
        body = route.body_field.type_ if route.body_field else None
        if "expected_revision" in (getattr(body, "model_fields", None) or {}):
            continue
        unrevisioned |= {f"{method} {route.path_format}" for method in route.methods if method != "GET"}
    text = DOCUMENT.read_text()
    table = text.split("<!-- writes-without-revision -->")[1].split("<!-- /writes-without-revision -->")[0]
    listed = {f"{method} {path}" for method, path in re.findall(r"`(GET|POST|PUT|PATCH|DELETE) (/studio/[^`]+)`", table)}
    assert listed == unrevisioned
