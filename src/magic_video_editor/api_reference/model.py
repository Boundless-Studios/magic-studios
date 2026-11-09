"""What the public API reference says about one route.

Each public route has a `RouteDoc` in one of the sibling modules, keyed by its
method and path as FastAPI formats it (`("GET", "/studio/projects/{project_id}")`).
A test fails when a public route has none, so a new route cannot ship undocumented.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# The reference's sections, in the order it shows them.
TAGS: dict[str, str] = {
    "Account": "Who you are, your limits, signing in and out, and deleting your account.",
    "API keys": "Bearer keys for scripts and agents. Managing keys needs a browser session.",
    "Machines and providers": (
        "Machines you attach, the generation settings they offer, and your saved "
        "Higgsfield key."
    ),
    "Projects": "Creating, listing, reading and deleting animation projects, and their history.",
    "Setup": "Declaring a project's directions, views and animations in one reviewed change.",
    "Subject": "The character image every animation starts from.",
    "Motions": "Animations: adding them, generating takes, and choosing one.",
    "Turnarounds": "One video that turns a direction's reference in place, the source of the other directions.",
    "Frames": "Inspecting a take's frames, finding a loop and saving edited frames.",
    "Releases and exports": "Freezing an approved set and downloading it as files.",
    "Movies": "Short films: cast, shots, takes, the cut and its release.",
    "Scene flights": "Camera moves between location images.",
    "Audio": "Sound sets: free sounds and uploads per cue, review, and licensed release packs.",
    "3D": "3D assets: source pictures, meshes you bring, review, selection and release packs.",
    "Downloads": "The MCP server and the agent skill, with their SHA-256.",
    "Reference data": "Presets and installed export targets.",
}

# Default wording for each error status. A route overrides it when it has
# something more specific to say.
ERROR_TEXT: dict[int, str] = {
    400: "The request cannot be processed as sent.",
    401: "No valid API key or session. A key may be malformed, unknown or revoked.",
    403: "You may not do this. `detail.code` says why when there is a reason to give.",
    404: "Not found, or not yours. The two are deliberately the same answer.",
    409: (
        "The request conflicts with the current state. A stale `expected_revision` "
        "answers with `detail.current_revision`; re-read and retry."
    ),
    410: "The item is being deleted for inactivity (`item_expiring`).",
    413: "The upload is too large.",
    416: "The requested byte range lies outside the file.",
    422: "The request body or a parameter is invalid.",
    429: "Too much at once. Wait for running work to finish, then retry.",
    500: "The change was refused to protect the project; nothing was saved.",
    502: "An upstream provider failed.",
    503: "A backend this route needs is unavailable or not configured. Retry later.",
    507: "Your storage allowance is used up. Delete something first.",
}


@dataclass(frozen=True)
class RouteDoc:
    """`summary` is a short imperative line ("Create a project"). `description` is
    one or more short paragraphs of plain sentences, Markdown allowed.

    `errors` lists every status the route can answer with besides success, 401 and
    FastAPI's own 422, each mapped to route-specific wording or `None` for the
    default in `ERROR_TEXT`. 401 is added for every route that needs a caller.

    A route whose handler declares no response model names its success body in
    `response` (a Pydantic model or a type such as `list[Model]`); the reference
    documents it without changing what the route returns. A route that answers
    with a redirect gives its status and wording in `redirect`, and one that
    streams a file says what the file is in `file`."""

    tag: str
    summary: str
    description: str
    errors: Mapping[int, str | None] = field(default_factory=dict)
    response: Any = None
    redirect: tuple[int, str] | None = None
    file: str | None = None
