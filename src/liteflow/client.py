from collections.abc import Mapping, Sequence
from typing import Final

import httpx
from openai.types.chat import ChatCompletion as ChatCompletionResponse
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from liteflow.authoring import ChatMessage, Workflow
from liteflow.compiler import compile_workflow


class Draft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    workflow_id: str
    team_id: str
    etag: str
    digest: str
    bundle: Mapping[str, JsonValue]
    published_snapshot_id: str | None = None


_JSON_OBJECT: Final = TypeAdapter[Mapping[str, JsonValue]](Mapping[str, JsonValue])
_COMPLETION: Final = TypeAdapter(ChatCompletionResponse)


class RunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    output: str
    run_id: str


class GatewayClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        team_id: str | None = None,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client: Final = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )
        self._team_id: Final = team_id

    def run(
        self,
        name: str,
        *,
        messages: Sequence[ChatMessage],
        idempotency_key: str | None = None,
    ) -> RunResult:
        response = self._client.post(
            "/v1/chat/completions",
            json={"model": f"workflow/{name}", "messages": messages, "stream": False},
            headers={"Idempotency-Key": idempotency_key} if idempotency_key is not None else {},
        )
        response.raise_for_status()
        completion = _COMPLETION.validate_json(response.content, strict=True)
        if len(completion.choices) != 1:
            raise ValueError("Workflow response must contain one final assistant message")
        content = completion.choices[0].message.content
        if not isinstance(content, str):
            raise ValueError("Workflow response must contain final assistant content")
        run_id = response.headers.get("x-litellm-workflow-run-id")
        if run_id is None or not run_id.strip():
            raise ValueError("Workflow response is missing its run ID")
        return RunResult(output=content, run_id=run_id)

    def register(self, workflow: Workflow, *, draft: Draft | None = None) -> Draft:
        path = "/v1/workflows" if draft is None else f"/v1/workflows/{draft.workflow_id}"
        response = self._client.request(
            "POST" if draft is None else "PUT",
            path,
            json={"bundle": compile_workflow(workflow)},
            params={"team_id": self._team_id} if self._team_id is not None else {},
            headers={"If-Match": draft.etag} if draft is not None else {},
        )
        response.raise_for_status()
        return Draft.model_validate_json(response.content)

    def get(self, workflow_id: str) -> Draft:
        response = self._client.get(
            f"/v1/workflows/{workflow_id}", params={"team_id": self._team_id} if self._team_id is not None else {}
        )
        response.raise_for_status()
        return Draft.model_validate_json(response.content)

    def validate(self, draft: Draft) -> Mapping[str, JsonValue]:
        response = self._client.post(
            f"/v1/workflows/{draft.workflow_id}/validate",
            params={"team_id": draft.team_id},
            headers={"If-Match": draft.etag},
        )
        response.raise_for_status()
        return _JSON_OBJECT.validate_json(response.content)

    def publish(self, draft: Draft) -> Mapping[str, JsonValue]:
        response = self._client.post(
            f"/v1/workflows/{draft.workflow_id}/publish",
            params={"team_id": draft.team_id},
            headers={"If-Match": draft.etag},
            json={"digest": draft.digest},
        )
        response.raise_for_status()
        return _JSON_OBJECT.validate_json(response.content)

    def inspect(self, run_id: str) -> Mapping[str, JsonValue]:
        response = self._client.get(f"/v1/workflows/runs/{run_id}/steps")
        response.raise_for_status()
        return _JSON_OBJECT.validate_json(response.content)

    def test(
        self,
        draft: Draft,
        *,
        messages: Sequence[ChatMessage],
        authorize_billing: bool,
        idempotency_key: str | None = None,
    ) -> Mapping[str, JsonValue]:
        response = self._client.post(
            f"/v1/workflows/{draft.workflow_id}/test",
            json={"messages": messages, "authorize_billing": authorize_billing},
            headers={
                "If-Match": draft.etag,
                **({"Idempotency-Key": idempotency_key} if idempotency_key is not None else {}),
            },
        )
        response.raise_for_status()
        return _JSON_OBJECT.validate_json(response.content)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GatewayClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
