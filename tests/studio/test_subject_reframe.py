"""`POST /subject/revisions/{revision_id}/reframe`: the Subject page's
"Fit to frame" / "Clean up" finding actions.
"""

from studio.test_routes import _studio_client, _studio_runtime
from studio.test_subject_revision_rejection import _upload_and_settle


def _project_with_two_takes() -> tuple:
    """Same shape as the rejection suite's fixture, but keeps the runtime so a
    second actor can be pointed at the very same project."""
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id = client.post("/studio/projects", json={"name": "Ember Knight"}).json()[
        "project_id"
    ]
    first = _upload_and_settle(client, runtime, project_id, 1, (200, 200, 824, 824))
    second = _upload_and_settle(client, runtime, project_id, 2, (100, 100, 700, 700))
    return runtime, client, project_id, first, second


def _reframe(client, project_id: str, revision_id: str, revision: int, **body: object):
    return client.post(
        f"/studio/projects/{project_id}/subject/revisions/{revision_id}/reframe",
        json={"expected_revision": revision, **body},
    )


def test_reframe_appends_a_selected_edited_revision_with_parent_lineage() -> None:
    _runtime, client, project_id, _first, second = _project_with_two_takes()
    source_id = second["manifest"]["subject"]["current_revision"]["revision_id"]

    response = _reframe(client, project_id, source_id, second["revision"])

    assert response.status_code == 200
    subject = response.json()["manifest"]["subject"]
    current = subject["current_revision"]
    assert current["revision_id"] != source_id
    assert current["source"] == "edited"
    assert current["parent_revision_ids"] == [source_id]
    # The framing pass normalises onto a 1024 square canvas, and nothing is deleted.
    assert (current["width"], current["height"]) == (1024, 1024)
    assert len(subject["revisions"]) == 3


def test_cleanup_edit_also_appends_a_new_edited_revision() -> None:
    _runtime, client, project_id, _first, second = _project_with_two_takes()
    source_id = second["manifest"]["subject"]["current_revision"]["revision_id"]

    response = _reframe(
        client, project_id, source_id, second["revision"], edit="cleanup"
    )

    assert response.status_code == 200
    current = response.json()["manifest"]["subject"]["current_revision"]
    assert current["source"] == "edited"
    assert current["parent_revision_ids"] == [source_id]


def test_reframe_is_owner_only() -> None:
    runtime, _client, project_id, _first, second = _project_with_two_takes()
    source_id = second["manifest"]["subject"]["current_revision"]["revision_id"]
    intruder = _studio_client(runtime, actor_id="intruder-1")

    response = _reframe(intruder, project_id, source_id, second["revision"])

    assert response.status_code == 404


def test_reframe_returns_404_for_an_unknown_revision() -> None:
    _runtime, client, project_id, _first, second = _project_with_two_takes()

    response = _reframe(client, project_id, "does-not-exist", second["revision"])

    assert response.status_code == 404


def test_reframe_refuses_a_rejected_revision() -> None:
    _runtime, client, project_id, first, second = _project_with_two_takes()
    first_id = first["manifest"]["subject"]["current_revision"]["revision_id"]
    rejected = client.post(
        f"/studio/projects/{project_id}/subject/revisions/{first_id}/reject",
        json={"expected_revision": second["revision"]},
    ).json()

    response = _reframe(client, project_id, first_id, rejected["revision"])

    assert response.status_code == 422
