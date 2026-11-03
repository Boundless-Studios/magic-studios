import asyncio
from typing import Any, Protocol

from google.api_core.exceptions import AlreadyExists
from google.cloud import tasks_v2


class TaskDispatcher(Protocol):
    async def enqueue(self, job_id: str) -> None: ...


class NullTaskDispatcher:
    async def enqueue(self, job_id: str) -> None:
        return None


class CloudTasksDispatcher:
    def __init__(
        self,
        *,
        project: str,
        location: str,
        queue: str,
        service_url: str,
        service_account_email: str,
        client: Any | None = None,
    ) -> None:
        self._parent = tasks_v2.CloudTasksClient.queue_path(project, location, queue)
        self._project = project
        self._location = location
        self._queue = queue
        self._service_url = service_url.rstrip("/")
        self._service_account_email = service_account_email
        self._client = client

    async def enqueue(self, job_id: str) -> None:
        task = {
            "name": tasks_v2.CloudTasksClient.task_path(
                self._project,
                self._location,
                self._queue,
                f"job-{job_id}",
            ),
            "dispatch_deadline": {"seconds": 900},
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{self._service_url}/tasks/jobs/{job_id}",
                "headers": {"Content-Type": "application/json"},
                "oidc_token": {
                    "service_account_email": self._service_account_email,
                    "audience": self._service_url,
                },
            }
        }
        client = self._client or tasks_v2.CloudTasksClient()
        try:
            await asyncio.to_thread(
                client.create_task,
                request={"parent": self._parent, "task": task},
            )
        except AlreadyExists:
            return
