"""Measure how each art-first onboarding template works out in practice.

The onboarding offers four starting templates (single direction, side-scrolling,
four- and eight-direction). Studio does not draw missing directions, so every
direction beyond the uploaded one needs its own artwork before it can generate.
This report answers whether people who pick a template get through it: how many
supply art for every direction, generate at all, generate beyond the uploaded
direction, and reach a release.

Only projects created through onboarding are counted. The workspace editor
applies the same setup schema to existing projects, so the schema version alone
does not identify onboarding. Onboarding creates an empty project (revision 1)
and applies its setup as the very next revision; staging the artwork writes no
revision. The template is inferred from that setup revision; progress comes
from the current revision. Only aggregate counts are printed: no project IDs, names or
owners. The query runs in a read-only transaction.

    MAGIC_VIDEO_EDITOR_DATABASE_URL=... python3 scripts/onboarding_funnel.py --since 2026-10-01
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time
from typing import Any

DATABASE_URL_VARIABLE = "MAGIC_VIDEO_EDITOR_DATABASE_URL"
TEMPLATES = ("single", "side", "four", "eight", "compass_edited")
CARDINALS = {"N", "E", "S", "W"}

QUERY = """
SELECT DISTINCT ON (setup.project_id)
       created.manifest AS created,
       setup.revision AS setup_revision,
       setup.manifest AS first_setup,
       current.manifest AS current,
       EXISTS (SELECT 1 FROM studio_releases release WHERE release.project_id = setup.project_id) AS released
  FROM studio_manifest_revisions setup
  JOIN studio_projects project ON project.project_id = setup.project_id
  JOIN studio_manifest_revisions created
    ON created.project_id = project.project_id AND created.revision = 1
  JOIN studio_manifest_revisions current
    ON current.project_id = project.project_id AND current.revision = project.current_revision
 WHERE project.created_at >= $1
   AND jsonb_path_exists(setup.manifest, '$.views[*] ? (@.setup_version == 1)')
 ORDER BY setup.project_id, setup.revision
"""


def _setup_views(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [view for view in manifest.get("views") or () if view.get("setup_version") == 1]


def came_from_onboarding(created: Mapping[str, Any], setup_revision: int) -> bool:
    """Whether a project's first setup was applied by onboarding, not a later workspace edit."""
    subject = created.get("subject") or {}
    empty = not (created.get("sprite_layout") or created.get("views") or created.get("motions") or subject.get("revisions"))
    return setup_revision == 2 and empty


def template_for(manifest: Mapping[str, Any]) -> str | None:
    """The template a first setup revision corresponds to, or None if it has no setup.

    Directions removed during onboarding make a compass layout match neither
    four- nor eight-direction; those are counted as ``compass_edited``.
    """
    views = _setup_views(manifest)
    if not views:
        return None
    layout = manifest.get("sprite_layout")
    if layout == "single_view":
        return "single"
    if layout == "side_view":
        return "side"
    headings = {view.get("heading") or view.get("view_id") for view in views}
    if len(headings) == 8:
        return "eight"
    if headings == CARDINALS:
        return "four"
    return "compass_edited"


def progress(manifest: Mapping[str, Any], released: bool) -> dict[str, bool]:
    """Funnel stages reached by one project's current manifest."""
    views = _setup_views(manifest)
    view_ids = {view.get("view_id") for view in views}
    generated_views = {
        motion.get("specification", {}).get("view_id")
        for motion in manifest.get("motions") or ()
        if motion.get("candidates") and motion.get("specification", {}).get("view_id") in view_ids
    }
    return {
        "every_direction_has_art": bool(views) and all(view.get("reference") for view in views),
        "generated_any": bool(generated_views),
        "generated_beyond_one_direction": len(generated_views) > 1,
        "released": released,
    }


def summarize(rows: Iterable[tuple[Mapping[str, Any], Mapping[str, Any], bool]]) -> dict[str, dict[str, Any]]:
    """Aggregate (first setup, current manifest, released) rows by template."""
    summary: dict[str, dict[str, Any]] = {
        template: {
            "projects": 0,
            "directions": 0,
            "directions_with_art": 0,
            "every_direction_has_art": 0,
            "generated_any": 0,
            "generated_beyond_one_direction": 0,
            "released": 0,
        }
        for template in TEMPLATES
    }
    for first_setup, current, released in rows:
        template = template_for(first_setup)
        if template is None:
            continue
        counts = summary[template]
        counts["projects"] += 1
        views = _setup_views(current)
        counts["directions"] += len(views)
        counts["directions_with_art"] += sum(1 for view in views if view.get("reference"))
        for stage, reached in progress(current, released).items():
            counts[stage] += int(reached)
    return summary


async def _fetch(database_url: str, since: datetime) -> list[tuple[Mapping[str, Any], Mapping[str, Any], bool]]:
    import asyncpg

    connection = await asyncpg.connect(database_url)
    try:
        async with connection.transaction(readonly=True):
            records = await connection.fetch(QUERY, since)
    finally:
        await connection.close()
    return [
        (_json(record["first_setup"]), _json(record["current"]), record["released"])
        for record in records
        if came_from_onboarding(_json(record["created"]), record["setup_revision"])
    ]


def _json(value: Any) -> Mapping[str, Any]:
    return json.loads(value) if isinstance(value, str) else value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", type=date.fromisoformat, default=date(2026, 1, 1), help="count projects created on or after this date")
    args = parser.parse_args(argv)
    database_url = os.environ.get(DATABASE_URL_VARIABLE)
    if not database_url:
        print(f"{DATABASE_URL_VARIABLE} is not set", file=sys.stderr)
        return 2
    since = datetime.combine(args.since, time.min, tzinfo=UTC)
    rows = asyncio.run(_fetch(database_url, since))
    json.dump({"since": args.since.isoformat(), "templates": summarize(rows)}, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
