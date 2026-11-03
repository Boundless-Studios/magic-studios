import hashlib
import json
from dataclasses import asdict

import httpx
import pytest

from magic_video_editor.contracts import SubjectGenerationRequest
from magic_video_editor.plugins.subject_http import (
    HttpSubjectGenerator,
    TerminalSubjectProviderError,
)


@pytest.mark.asyncio
async def test_subject_provider_requires_create_once_capability_attestation() -> None:
    request = SubjectGenerationRequest(
        reference_keys=("sources/studio/p/subject-references/a.jpg",),
        output_key="outputs/studio/p/subjects/op.png",
        direction=None,
        idempotency_key="p:subject:op",
    )
    digest = hashlib.sha256(json.dumps(asdict(request), separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    response = httpx.Response(200, json={"output_key": request.output_key, "idempotency_key": request.idempotency_key, "request_digest": digest, "contract_version": "subject-immutable-v1", "publication_mode": "overwrite"})

    class Client:
        async def post(self, url: str, **kwargs: object) -> httpx.Response:
            return response

    provider = HttpSubjectGenerator(endpoint="https://provider.invalid/generate", audience="aud", token_provider=lambda _: "token", client=Client())
    with pytest.raises(TerminalSubjectProviderError, match="create-once"):
        await provider.generate_subject(request)
