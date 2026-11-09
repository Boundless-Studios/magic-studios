"""The public API reference: an OpenAPI schema of the `/studio` routes people and
agents call, and the pages that render it.

FastAPI's own schema lists every route the process serves, internal and machine
routes included, with no authentication and no errors beyond validation. This
builds the published one from the same routes instead: only public routes, each
with its `RouteDoc` summary, description and errors, and the two ways to
authenticate. `/studio/reference` renders it with Redoc and `/studio/docs` with
Swagger UI, so both are reachable on every host the load balancer sends `/studio/`
to.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from pydantic import TypeAdapter

from magic_video_editor.api_reference.account import ROUTES as ACCOUNT_ROUTES
from magic_video_editor.api_reference.assets3d import ROUTES as ASSETS3D_ROUTES
from magic_video_editor.api_reference.audio import ROUTES as AUDIO_ROUTES
from magic_video_editor.api_reference.media import ROUTES as MEDIA_ROUTES
from magic_video_editor.api_reference.model import ERROR_TEXT, TAGS, RouteDoc
from magic_video_editor.api_reference.motions import ROUTES as MOTION_ROUTES
from magic_video_editor.api_reference.projects import ROUTES as PROJECT_ROUTES
from magic_video_editor.studio.routes import (
    CSRF_SAFE_METHODS,
    studio_actor_id,
    studio_browser_actor_id,
)

SCHEMA_PATH = "/studio/openapi.json"
REFERENCE_PATH = "/studio/reference"
SWAGGER_PATH = "/studio/docs"

# The pages run on the signed-in origin, so their code is served from here, never a CDN:
# script there would act with the visitor's session. These are the npm releases
# (redoc@2.5.4, swagger-ui-dist@5.33.0), unmodified, checked against the registry's
# published integrity, and each tag carries its own SRI hash.
ASSETS_PATH = "/studio/reference/assets"
ASSETS: dict[str, tuple[str, str]] = {
    "redoc-2.5.4.standalone.js": (
        "text/javascript; charset=utf-8",
        "sha384-w447zOpYfw/1Tv/5AK9NfHTlQIqE3RVR6KY62jCyy9zNDgO64cMwGGP1Fj0zJVf5",
    ),
    "swagger-ui-5.33.0-bundle.js": (
        "text/javascript; charset=utf-8",
        "sha384-YDALVcy8kj8yltLBVi1vBiBAUqdxvus673gM8XKwiy6aDUJFXivF/KCufekjYbVf",
    ),
    "swagger-ui-5.33.0.css": (
        "text/css; charset=utf-8",
        "sha384-Ov4/wv3j2bmct8cDc5X4ngJZohVPzEmc6uDPH8WeljUxO5vtoykvMEfbu9Vh6RaW",
    ),
}
STATIC = Path(__file__).parent / "static"

DOCS: dict[tuple[str, str], RouteDoc] = {
    **ACCOUNT_ROUTES, **PROJECT_ROUTES, **MOTION_ROUTES, **MEDIA_ROUTES, **AUDIO_ROUTES,
    **ASSETS3D_ROUTES,
}

# Under /studio but not for callers: what an attaching machine downloads and
# calls and the narrative document the reference links to.
HIDDEN: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/studio/attach.sh"),
    ("GET", "/studio/attach.ps1"),
    ("GET", "/studio/worker.py"),
    ("GET", "/studio/cpu-worker.py"),
    ("GET", "/studio/worker-update.py"),
    ("GET", "/studio/worker-runtime.py"),
    ("GET", "/studio/worker-install.py"),
    ("POST", "/studio/workers/redeem"),
    ("POST", "/studio/workers/rotate"),
    ("POST", "/studio/workers/package"),
    ("POST", "/studio/workers/package/manifest"),
    ("POST", "/studio/workers/package/update"),
    ("GET", "/studio/api.md"),
})

Caller = Literal["key", "session", "none"]

CSRF_TEXT = (
    "With a browser session, the request must come from this site: a missing or "
    "mismatched `Origin` or `Referer` is refused (`csrf_origin_mismatch`). API keys are exempt."
)

# Headers the identity dependencies read. `Authorization` is the API key scheme
# below; the rest only a trusted gateway or local development may set. None of
# them is a parameter a caller fills in.
IDENTITY_HEADERS = frozenset({
    "authorization",
    "x-studio-actor-id",
    "x-goog-authenticated-user-email",
    "x-goog-iap-jwt-assertion",
})

DESCRIPTION = """\
Magic Studios runs on this API. The web app uses the same routes, so scripts and AI
agents can do what you do with your projects in the browser.

**Base URL.** `https://api.magicstudios.ai`. `https://magicstudios.ai` serves the same
routes.

**Authentication.** Create a key in the web app under account menu → **API keys**
and send it as `Authorization: Bearer mvs_…`. A key acts as you and nothing more.
Routes marked with the session scheme only, such as managing keys or deleting your
account, need a signed-in browser.

**Revisions.** Writes that change an existing project, movie or scene flight carry
`expected_revision`. A stale one answers `409` with `detail.current_revision`.

**Getting started.** The [API guide](/studio/api.md) explains the concepts and walks
through a project from an image to an export. The
[studio guide](https://magicstudios.ai/guide#agents) covers connecting an agent.
"""

ERROR_SCHEMA: dict[str, Any] = {
    "title": "Error",
    "type": "object",
    "description": (
        "Most errors carry `detail`: a message, or an object with `code`, `message` "
        "and sometimes more fields. A few project-integrity errors carry `code` and "
        "`message` at the top level instead."
    ),
    "properties": {
        "detail": {
            "anyOf": [
                {"type": "string"},
                {
                    "type": "object",
                    "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
                    "additionalProperties": True,
                },
            ]
        },
        "code": {"type": "string"},
        "message": {"type": "string"},
    },
}

SECURITY_SCHEMES: dict[str, Any] = {
    "apiKey": {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "mvs_ followed by 32 url-safe characters",
        "description": "An API key from account menu → API keys, sent as `Authorization: Bearer mvs_…`.",
    },
    "session": {
        "type": "apiKey",
        "in": "cookie",
        "name": "mvs_session",
        "description": (
            "The browser session set by signing in. A request other than GET must "
            "also send an `Origin` matching the host, or it is refused with `403`."
        ),
    },
}


def public_routes(application: FastAPI, hidden: frozenset[tuple[str, str]] = HIDDEN) -> list[APIRoute]:
    """The routes the reference documents, one per method."""
    routes = []
    for route in application.routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        if not route.path_format.startswith("/studio/"):
            continue
        for method in sorted(route.methods):
            if (method, route.path_format) not in hidden:
                routes.append(route)
                break
    return routes


def route_caller(route: APIRoute) -> Caller:
    """Who may call the route, from the identity dependencies it actually uses."""
    calls = set()
    pending = [route.dependant]
    while pending:
        dependant = pending.pop()
        calls.add(dependant.call)
        pending.extend(dependant.dependencies)
    if studio_browser_actor_id in calls:
        return "session"
    if studio_actor_id in calls:
        return "key"
    return "none"


def operation_id(summary: str) -> str:
    words = [word for word in re.findall(r"[A-Za-z0-9]+", summary) if word.lower() not in {"a", "an", "the"}]
    return words[0].lower() + "".join(word[:1].upper() + word[1:] for word in words[1:])


def build_schema(
    application: FastAPI,
    docs: Mapping[tuple[str, str], RouteDoc] = DOCS,
    hidden: frozenset[tuple[str, str]] = HIDDEN,
    tags: Mapping[str, str] = TAGS,
) -> dict[str, Any]:
    routes = public_routes(application, hidden)
    schema = get_openapi(
        title="Magic Studios API",
        version="1",
        description=DESCRIPTION,
        routes=routes,
        tags=[{"name": name, "description": text} for name, text in tags.items()],
        servers=[
            {"url": "/", "description": "The host serving this page"},
            {"url": "https://api.magicstudios.ai", "description": "For scripts and agents"},
        ],
    )
    components = schema.setdefault("components", {})
    components.setdefault("schemas", {})["Error"] = ERROR_SCHEMA
    components["securitySchemes"] = SECURITY_SCHEMES
    for route in routes:
        for method in route.methods:
            key = (method, route.path_format)
            if key in hidden:
                continue
            doc = docs.get(key)
            if doc is None:
                raise LookupError(f"{method} {route.path_format} has no RouteDoc")
            operation = schema["paths"][route.path_format][method.lower()]
            _document(operation, doc, route_caller(route), method)
            _document_success(operation, doc, components["schemas"])
    return schema


def _document_success(operation: dict[str, Any], doc: RouteDoc, schemas: dict[str, Any]) -> None:
    """Describe a success body the handler itself does not declare."""
    responses = operation["responses"]
    success = [status for status in responses if status.startswith("2")]
    if doc.redirect is not None:
        status, text = doc.redirect
        for code in success:
            del responses[code]
        responses[str(status)] = {
            "description": text,
            "headers": {"Location": {"description": "Where to go next.", "schema": {"type": "string"}}},
        }
    elif doc.file is not None:
        for code in success:
            del responses[code]
        binary = {"*/*": {"schema": {"type": "string", "format": "binary"}}}
        responses["200"] = {"description": doc.file, "content": binary}
        responses["206"] = {"description": "Part of the file, for a `Range` request.", "content": binary}
    elif doc.response is not None:
        body = TypeAdapter(doc.response).json_schema(
            ref_template="#/components/schemas/{model}", mode="serialization"
        )
        for name, definition in body.pop("$defs", {}).items():
            if _same_shape(schemas.setdefault(name, definition)) != _same_shape(definition):
                raise ValueError(f"two different schemas are both named {name}")
        status = success[0] if success else "200"
        response = responses.setdefault(status, {"description": "Successful Response"})
        response["content"] = {"application/json": {"schema": body}}


def _same_shape(schema: Any) -> Any:
    """A schema without `"default": null`, which FastAPI's request schemas omit and
    a response schema of the same model keeps; otherwise they describe one shape."""
    if isinstance(schema, dict):
        return {key: _same_shape(value) for key, value in schema.items() if not (key == "default" and value is None)}
    if isinstance(schema, list):
        return [_same_shape(value) for value in schema]
    return schema


def _document(operation: dict[str, Any], doc: RouteDoc, caller: Caller, method: str) -> None:
    operation["tags"] = [doc.tag]
    operation["summary"] = doc.summary
    operation["description"] = doc.description
    operation["operationId"] = operation_id(doc.summary)
    parameters = [
        parameter
        for parameter in operation.get("parameters", [])
        if not (parameter["in"] == "header" and parameter["name"].lower() in IDENTITY_HEADERS)
    ]
    if parameters:
        operation["parameters"] = parameters
    else:
        operation.pop("parameters", None)
    operation["security"] = {
        "key": [{"apiKey": []}, {"session": []}],
        "session": [{"session": []}],
        "none": [],
    }[caller]
    errors = dict(doc.errors)
    if caller != "none" and method not in CSRF_SAFE_METHODS:
        # A browser session's writes must come from this site; a key is exempt.
        errors[403] = CSRF_TEXT if 403 not in errors else f"{errors[403] or ERROR_TEXT[403]} {CSRF_TEXT}"
    if caller != "none":
        errors.setdefault(401, None)
    responses = operation["responses"]
    for status in sorted(errors):
        text = errors[status] or ERROR_TEXT[status]
        error = {"$ref": "#/components/schemas/Error"}
        if status == 422 and "422" in responses:
            validation = responses["422"]["content"]["application/json"]["schema"]
            error = {"anyOf": [validation, error]}
        responses[str(status)] = {"description": text, "content": {"application/json": {"schema": error}}}
    if "422" in responses and 422 not in errors:
        responses["422"]["description"] = ERROR_TEXT[422]


def install_api_reference(
    application: FastAPI,
    *,
    docs: Mapping[tuple[str, str], RouteDoc] | None = None,
    hidden: frozenset[tuple[str, str]] = frozenset(),
    tags: Mapping[str, str] | None = None,
) -> None:
    """Serve the schema, both pages and their assets, and point FastAPI's old URLs at
    them. Those redirects work where every path reaches the API (api.magicstudios.ai);
    on magicstudios.ai only `/studio/*` does, so the web app redirects `/docs` and
    `/redoc` itself, and the schema is documented only at `/studio/openapi.json`.
    `docs`, `hidden` and `tags` add extensions' routes to the core ones."""
    cached: dict[str, Any] = {}
    all_docs = {**DOCS, **(docs or {})}
    all_hidden = HIDDEN | hidden
    all_tags = {**TAGS, **(tags or {})}

    def schema() -> dict[str, Any]:
        if not cached:
            cached.update(build_schema(application, all_docs, all_hidden, all_tags))
        return cached

    application.openapi = schema  # type: ignore[method-assign]

    @application.get(SCHEMA_PATH, include_in_schema=False)
    async def openapi_schema() -> JSONResponse:
        return JSONResponse(schema())

    @application.get(REFERENCE_PATH, include_in_schema=False)
    async def reference_page() -> HTMLResponse:
        return HTMLResponse(_page(
            "Magic Studios API reference",
            body=f'<redoc spec-url="{SCHEMA_PATH}"></redoc>',
            scripts=["redoc-2.5.4.standalone.js"],
        ))

    @application.get(SWAGGER_PATH, include_in_schema=False)
    async def swagger_page() -> HTMLResponse:
        # No validator badge: Swagger UI would otherwise fetch it from swagger.io.
        options = json.dumps({"url": SCHEMA_PATH, "dom_id": "#swagger-ui", "deepLinking": True,
                              "validatorUrl": None, "persistAuthorization": False})
        return HTMLResponse(_page(
            "Magic Studios API: try it",
            body='<div id="swagger-ui"></div>',
            styles=["swagger-ui-5.33.0.css"],
            scripts=["swagger-ui-5.33.0-bundle.js"],
            inline=f"SwaggerUIBundle({options});",
        ))

    @application.get(ASSETS_PATH + "/{name}", include_in_schema=False)
    async def reference_asset(name: str) -> FileResponse:
        if name not in ASSETS:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(
            STATIC / name,
            media_type=ASSETS[name][0],
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    for old, new in (("/openapi.json", SCHEMA_PATH), ("/docs", SWAGGER_PATH), ("/redoc", REFERENCE_PATH)):
        application.add_api_route(
            old, _redirect(new), methods=["GET"], include_in_schema=False
        )


def _page(title: str, *, body: str, scripts: Sequence[str], styles: Sequence[str] = (), inline: str = "") -> str:
    tags = [f'<link rel="stylesheet" href="{ASSETS_PATH}/{name}" integrity="{ASSETS[name][1]}">' for name in styles]
    tags += [f'<script src="{ASSETS_PATH}/{name}" integrity="{ASSETS[name][1]}"></script>' for name in scripts]
    script = f"<script>{inline}</script>" if inline else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title>{''.join(tags[:len(styles)])}</head>"
        f"<body>{body}{''.join(tags[len(styles):])}{script}</body></html>"
    )


def _redirect(target: str):
    async def redirect() -> RedirectResponse:
        return RedirectResponse(target, status_code=308)

    return redirect
