"""Refuse to persist a manifest revision that could not be read back.

Production wrote revisions whose ``motions`` serialised as ``null`` entries
(Sentry MAGIC-VIDEO-EDITOR-BACKEND-5/-B), which made every later read of the
project fail. The stores now serialise, re-validate, and only then write; a
failure is logged with the runtime types of the offending values so the cause
is visible the next time it happens, and the write is refused.
"""

import json
import logging

from pydantic import ValidationError

from magic_video_editor.studio.models import AnimationProjectManifest

logger = logging.getLogger(__name__)

_MAX_REPR = 400


class ManifestUnwritable(RuntimeError):
    """The manifest about to be stored does not survive a serialise/validate round trip."""

    def __init__(self, project_id: str, revision: int | None, detail: str) -> None:
        super().__init__(
            f"refusing to persist an unreadable manifest for {project_id} "
            f"(revision {revision}): {detail}"
        )
        self.project_id = project_id
        self.revision = revision


def _describe(value: object) -> dict[str, str]:
    kind = type(value)
    return {
        "type": f"{kind.__module__}.{kind.__qualname__}",
        "repr": repr(value)[:_MAX_REPR],
    }


def serialize_checked(
    manifest: AnimationProjectManifest, *, project_id: str, revision: int | None
) -> str:
    """Return the JSON payload for ``manifest`` after proving it validates back."""
    payload = manifest.model_dump_json()
    try:
        AnimationProjectManifest.model_validate(json.loads(payload))
    except ValidationError as exc:
        detail = str(exc)[:600]
        logger.error(
            "refusing to persist an unreadable manifest",
            extra={
                "project_id": project_id,
                "revision": revision,
                "validation_error": detail,
                "motions": [_describe(motion) for motion in manifest.motions],
                "motion_candidates": [
                    [
                        _describe(candidate)
                        for candidate in getattr(motion, "candidates", ())
                    ]
                    for motion in manifest.motions
                ],
                "subject_current": _describe(manifest.subject.current_revision),
                "payload_head": payload[:2000],
            },
        )
        raise ManifestUnwritable(project_id, revision, detail) from exc
    return payload
