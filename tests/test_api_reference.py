"""The published API reference covers every public route, and only those."""

from __future__ import annotations

import base64
import hashlib
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from outbound_gpu_worker_pool import (
    MemoryAssetStore,
    MemoryAuditLog,
    MemoryJobStore,
    MemoryWorkerAuthenticator,
    MemoryWorkerRegistry,
)
from outbound_gpu_worker_pool.plugins import (
    DeterministicEchoPlugin,
    capability_schemas_from_plugins,
)
from outbound_gpu_worker_pool.service import WorkerPoolService

from magic_video_editor.api_reference import ASSETS, DOCS, HIDDEN, STATIC, public_routes, route_caller
from magic_video_editor.api_reference.model import ERROR_TEXT, TAGS
from magic_video_editor.local_studio import (
    create_local_studio_app,
    create_local_studio_runtime,
)


def full_app() -> FastAPI:
    """The local studio with every optional core router on, so every public route
    exists. Extensions' routes and docs are checked with each extension."""
    runtime = create_local_studio_runtime()
    runtime.worker_pool = WorkerPoolService(
        MemoryJobStore(),
        MemoryAssetStore(),
        MemoryWorkerRegistry(),
        MemoryAuditLog(),
        MemoryWorkerAuthenticator({}),
        capability_schemas_from_plugins([DeterministicEchoPlugin()]),
    )
    return create_local_studio_app(runtime=runtime)


@pytest.fixture(scope="module")
def application() -> FastAPI:
    return full_app()


@pytest.fixture(scope="module")
def schema(application: FastAPI) -> dict:
    return TestClient(application).get("/studio/openapi.json").json()


def _operations(schema: dict):
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            yield method.upper(), path, operation


def test_every_public_route_has_a_reference_entry_and_no_entry_is_stale(application: FastAPI) -> None:
    routes = {(method, route.path_format) for route in public_routes(application) for method in route.methods}
    routes -= HIDDEN
    assert sorted(routes - DOCS.keys()) == [], "document these routes in magic_video_editor/api_reference"
    assert sorted(DOCS.keys() - routes) == [], "these entries name no public route"


def test_only_studio_routes_for_callers_are_published(schema: dict) -> None:
    paths = set(schema["paths"])
    assert all(path.startswith("/studio/") for path in paths)
    assert not {path for _, path in HIDDEN} & paths
    assert "/studio/openapi.json" not in paths


def test_every_operation_has_a_summary_description_tag_and_unique_id(schema: dict) -> None:
    ids = []
    for method, path, operation in _operations(schema):
        where = f"{method} {path}"
        assert operation["tags"] and operation["tags"][0] in TAGS, where
        assert operation["summary"].strip() and len(operation["summary"]) <= 60, where
        assert not operation["summary"].endswith("."), where
        assert len(operation["description"].strip()) >= 20, where
        ids.append(operation["operationId"])
    assert len(ids) == len(set(ids)), sorted(i for i in ids if ids.count(i) > 1)


def test_authentication_matches_the_identity_each_route_resolves(application: FastAPI, schema: dict) -> None:
    assert set(schema["components"]["securitySchemes"]) == {"apiKey", "session"}
    for route in public_routes(application):
        for method in route.methods:
            if (method, route.path_format) in HIDDEN:
                continue
            operation = schema["paths"][route.path_format][method.lower()]
            caller = route_caller(route)
            schemes = {name for requirement in operation["security"] for name in requirement}
            expected = {"key": {"apiKey", "session"}, "session": {"session"}, "none": set()}[caller]
            assert schemes == expected, f"{method} {route.path_format}"
            assert ("401" in operation["responses"]) == (caller != "none"), f"{method} {route.path_format}"


def test_errors_are_documented_with_a_shared_shape(schema: dict) -> None:
    for method, path, operation in _operations(schema):
        for status, response in operation["responses"].items():
            if status.startswith(("2", "3")):
                continue
            assert int(status) in ERROR_TEXT, f"{method} {path} {status}"
            assert response["description"].strip(), f"{method} {path} {status}"


def test_writes_that_take_a_revision_document_the_conflict(application: FastAPI, schema: dict) -> None:
    for route in public_routes(application):
        body = route.body_field.type_ if route.body_field else None
        if "expected_revision" not in (getattr(body, "model_fields", None) or {}):
            continue
        for method in route.methods:
            assert "409" in schema["paths"][route.path_format][method.lower()]["responses"], f"{method} {route.path_format}"


def test_routes_on_an_owned_item_document_not_found(schema: dict) -> None:
    owned = re.compile(r"^/studio/(projects|movies|scene-flights|releases|exports|api-keys|workers)/\{")
    for method, path, operation in _operations(schema):
        if owned.match(path):
            assert "404" in operation["responses"], f"{method} {path}"


def test_identity_headers_are_not_parameters(schema: dict) -> None:
    for method, path, operation in _operations(schema):
        names = {parameter["name"].lower() for parameter in operation.get("parameters", [])}
        identity = {"authorization", "x-studio-actor-id", "x-goog-authenticated-user-email", "x-goog-iap-jwt-assertion"}
        assert not names & identity, f"{method} {path}"


def test_the_reference_pages_are_served_and_the_old_urls_lead_to_them(application: FastAPI) -> None:
    client = TestClient(application)
    reference = client.get("/studio/reference")
    assert reference.status_code == 200
    assert '<redoc spec-url="/studio/openapi.json">' in reference.text
    assert "/studio/reference/assets/redoc-2.5.4.standalone.js" in reference.text
    swagger = client.get("/studio/docs")
    assert swagger.status_code == 200 and "/studio/reference/assets/swagger-ui-5.33.0-bundle.js" in swagger.text
    for old, new in (("/openapi.json", "/studio/openapi.json"), ("/docs", "/studio/docs"), ("/redoc", "/studio/reference")):
        moved = client.get(old, follow_redirects=False)
        assert moved.status_code == 308 and moved.headers["location"] == new


@pytest.mark.parametrize("page", ["/studio/reference", "/studio/docs"])
def test_the_reference_pages_load_nothing_from_another_origin(application: FastAPI, page: str) -> None:
    """They run on the signed-in origin, so third-party script there would act with
    the visitor's session."""
    html = TestClient(application).get(page).text
    assert not re.findall(r"""(?:src|href|url)\s*[=(]\s*["']?(?:https?:)?//""", html, re.IGNORECASE)
    tags = re.findall(r"<(?:script|link)\b[^>]*>", html)
    for tag in tags:
        if "src=" in tag or "href=" in tag:
            assert 'integrity="sha384-' in tag, tag
    assert '"validatorUrl": null' in html or page == "/studio/reference"


def test_the_vendored_assets_are_served_and_match_their_pinned_integrity(application: FastAPI) -> None:
    client = TestClient(application)
    for name, (media_type, integrity) in ASSETS.items():
        digest = "sha384-" + base64.b64encode(hashlib.sha384((STATIC / name).read_bytes()).digest()).decode()
        assert digest == integrity, f"{name} changed; update its pinned hash only for a reviewed release"
        served = client.get(f"/studio/reference/assets/{name}")
        assert served.status_code == 200 and served.content == (STATIC / name).read_bytes()
        assert served.headers["content-type"] == media_type
    assert client.get("/studio/reference/assets/redoc-LICENSE.txt").status_code == 404
    assert client.get("/studio/reference/assets/..%2F__init__.py").status_code == 404


def test_session_writes_document_the_origin_check(schema: dict) -> None:
    """A session's non-GET request without a matching Origin is refused, so every
    operation a session may call that way says so."""
    for method, path, operation in _operations(schema):
        if method in {"GET", "HEAD", "OPTIONS"}:
            continue
        if any("session" in requirement for requirement in operation["security"]):
            assert "csrf_origin_mismatch" in operation["responses"].get("403", {}).get("description", ""), f"{method} {path}"


def test_every_operation_says_what_success_returns(schema: dict) -> None:
    """A JSON body with a real shape, a file, a redirect, or nothing at all. A closed
    route is marked deprecated and only says that it is gone (410)."""
    for method, path, operation in _operations(schema):
        success = {status: response for status, response in operation["responses"].items() if status[0] in "23"}
        if operation.get("deprecated"):
            assert not success and "410" in operation["responses"], f"{method} {path}"
            continue
        assert success, f"{method} {path}"
        for status, response in success.items():
            content = response.get("content")
            if status.startswith("3") or status == "204" or not content:
                continue
            body = next(iter(content.values())).get("schema", {})
            bare = body.get("type") == "object" and not body.get("properties") and body.get("additionalProperties") in (None, True, {})
            assert body and not bare, f"{method} {path} {status}: say what it returns with RouteDoc.response, file or redirect"


def test_generation_machines_publish_last_frame_eligibility(schema: dict) -> None:
    machine = schema["components"]["schemas"]["GenerationMachine"]
    assert "features" in machine["required"]
    features = schema["components"]["schemas"]["MachineFeatures"]
    assert features["properties"]["last_frame"]["type"] == "boolean"


def test_turnaround_reads_say_how_work_is_settled(schema: dict) -> None:
    paths = schema["paths"]
    base = "/studio/projects/{project_id}/turnarounds"
    for path in (base, base + "/{turnaround_id}"):
        assert "/activity/reconcile" in paths[path]["get"]["description"]
    assert "features.last_frame" in paths[base]["post"]["description"]
