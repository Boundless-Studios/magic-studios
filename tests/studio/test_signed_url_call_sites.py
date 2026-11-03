"""Every signed-URL mint in `StudioService` must sit behind an ownership check.

A signed URL is a bearer capability handed to a browser, so a new call site that
skips `_require_owned_project` / `_require_manifest_asset` would silently hand one
actor another actor's bytes. This AST scan makes the next omission loud: adding a
mint in a method that is not on the allowlist fails here until it is reviewed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import magic_video_editor.studio.service as service_module

# `_upload_target` mints on behalf of its caller, so calling it counts as a mint
# and each caller must be reviewed here too.
_MINTING_METHODS = frozenset(
    {"create_read_url", "create_upload_url", "create_output_upload_url", "_upload_target"}
)

# method name -> how ownership of the key is proven before the mint.
_ALLOWED_CALL_SITES: dict[str, str] = {
    "create_subject_upload": "_require_owned_project, key derived from project_id",
    "create_subject_reference_upload": (
        "_require_owned_project, key derived from project_id"
    ),
    "create_motion_reference_upload": (
        "_require_owned_project, key derived from project_id"
    ),
    "create_view_reference_upload": (
        "_require_owned_project(project_id, actor_id, expected_revision) precedes "
        "layout validation and mint; key derives only from that owned project_id, "
        "validated ViewId, and a server-generated UUID"
    ),
    "create_candidate_upload": "_require_owned_project, key derived from project_id",
    "_upload_target": "scanned as a mint itself, so every caller is listed here",
}


def _service_tree() -> ast.Module:
    return ast.parse(Path(service_module.__file__).read_text())


def _minting_call_sites() -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in ast.walk(_service_tree()):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr in _MINTING_METHODS
            ):
                counts[node.name] = counts.get(node.name, 0) + 1
    return counts


def test_no_unreviewed_signed_url_call_site_exists() -> None:
    unexpected = sorted(set(_minting_call_sites()) - set(_ALLOWED_CALL_SITES))

    assert unexpected == [], (
        "new signed-URL call site(s) in studio/service.py: "
        f"{unexpected}. Prove the key belongs to the caller's project "
        "(_require_owned_project / _require_manifest_asset), then add the method "
        "to _ALLOWED_CALL_SITES with the reason."
    )


def test_allowlist_has_no_stale_entries() -> None:
    stale = sorted(set(_ALLOWED_CALL_SITES) - set(_minting_call_sites()))

    assert stale == [], f"_ALLOWED_CALL_SITES names methods that no longer mint: {stale}"


def test_the_scan_actually_finds_the_known_call_sites() -> None:
    # Guards the guard: a scan that silently matches nothing would always pass.
    assert sum(_minting_call_sites().values()) >= len(_ALLOWED_CALL_SITES)


def test_the_studio_never_hands_out_a_bearer_read_url() -> None:
    """Reads stream through owner-authorized routes (`/files`, `/exports/{id}/file`),
    so a link only works for the person it was given to."""
    reads = [
        node
        for node in ast.walk(_service_tree())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_read_url"
    ]

    assert reads == []
