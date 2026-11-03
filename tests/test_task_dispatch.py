from magic_video_editor.task_dispatch import CloudTasksDispatcher


class _Client:
    def __init__(self) -> None:
        self.request = None

    def create_task(self, *, request):
        self.request = request


async def test_dispatch_uses_oidc_and_the_private_worker_route() -> None:
    client = _Client()
    dispatcher = CloudTasksDispatcher(
        project="example-project",
        location="us-west1",
        queue="video-jobs",
        service_url="https://editor.run.app",
        service_account_email="tasks@example-project.iam.gserviceaccount.com",
        client=client,
    )

    await dispatcher.enqueue("job-1")

    assert client.request["parent"].endswith("/locations/us-west1/queues/video-jobs")
    assert client.request["task"]["name"].endswith(
        "/locations/us-west1/queues/video-jobs/tasks/job-job-1"
    )
    assert client.request["task"]["dispatch_deadline"] == {"seconds": 900}
    request = client.request["task"]["http_request"]
    assert request["url"] == "https://editor.run.app/tasks/jobs/job-1"
    assert request["oidc_token"]["audience"] == "https://editor.run.app"
    assert request["oidc_token"]["service_account_email"].startswith("tasks@")
