import json

import httpx
import pytest

from liteflow import ChatCompletion, Draft, GatewayClient, Input, RunResult, Workflow, bundle_digest


def test_registration_uses_the_bundle_etag_for_replacement_and_publication():
    requests = []

    def gateway(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path.endswith("/publish"):
            return httpx.Response(403, json={"error": "independent publisher required"})
        bundle = json.loads(request.content)["bundle"]
        return httpx.Response(
            201 if request.method == "POST" else 200,
            json={
                "name": bundle["name"],
                "workflow_id": "draft-id",
                "team_id": "test-team",
                "etag": '"reviewed-etag"',
                "digest": bundle_digest(bundle),
                "bundle": bundle,
            },
        )

    flow = Workflow(name="email-reply", nodes=[ChatCompletion(model="support-model", messages=Input.messages)])
    with GatewayClient(
        base_url="https://gateway.invalid",
        api_key="synthetic",
        team_id="test-team",
        transport=httpx.MockTransport(gateway),
    ) as client:
        draft = client.register(flow)
        replaced = client.register(flow, draft=draft)
        with pytest.raises(httpx.HTTPStatusError) as error:
            client.publish(replaced)
    assert error.value.response.status_code == 403
    assert tuple((request.method, request.url.path) for request in requests) == (
        ("POST", "/v1/workflows"),
        ("PUT", "/v1/workflows/draft-id"),
        ("POST", "/v1/workflows/draft-id/publish"),
    )
    assert requests[0].url.params["team_id"] == "test-team"
    assert requests[1].headers["If-Match"] == draft.etag
    assert requests[2].headers["If-Match"] == replaced.etag
    assert json.loads(requests[2].content) == {"digest": replaced.digest}


def test_validation_draft_test_and_inspection_keep_authorization_explicit():
    requests = []

    def gateway(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"valid": True})

    draft = Draft(
        name="email-reply", workflow_id="draft-id", team_id="test-team", etag='"etag"', digest="digest", bundle={}
    )
    with GatewayClient(
        base_url="https://gateway.invalid", api_key="synthetic", transport=httpx.MockTransport(gateway)
    ) as client:
        assert client.validate(draft) == {"valid": True}
        assert client.test(
            draft, messages=[{"role": "user", "content": "Review"}], authorize_billing=True, idempotency_key="retry-key"
        ) == {"valid": True}
        assert client.inspect("run-id") == {"valid": True}
    assert tuple(request.url.path for request in requests) == (
        "/v1/workflows/draft-id/validate",
        "/v1/workflows/draft-id/test",
        "/v1/workflows/runs/run-id/steps",
    )
    assert requests[1].headers["Idempotency-Key"] == "retry-key"
    assert requests[1].headers["If-Match"] == draft.etag
    assert json.loads(requests[1].content) == {
        "messages": [{"role": "user", "content": "Review"}],
        "authorize_billing": True,
    }


def _completion(message):
    return {
        "id": "chatcmpl-run-id",
        "object": "chat.completion",
        "created": 1,
        "model": "workflow/email-reply",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }


@pytest.mark.parametrize("output", ["", "Reviewed reply", '{"approved":true}'])
@pytest.mark.parametrize("idempotency_key", [None, "same-request"])
def test_run_only_invokes_standard_completions_and_returns_inspectable_content(output, idempotency_key):
    requests = []

    def gateway(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"x-litellm-workflow-run-id": "run-id"},
            json=_completion({"role": "assistant", "content": output}),
        )

    messages = [{"role": "user", "content": "Review this draft"}]
    with GatewayClient(
        base_url="https://gateway.invalid", api_key="synthetic", transport=httpx.MockTransport(gateway)
    ) as client:
        result = client.run("email-reply", messages=messages, idempotency_key=idempotency_key)
    assert result == RunResult(output=output, run_id="run-id")
    assert len(requests) == 1
    request = requests[0]
    assert (request.method, request.url.path) == ("POST", "/v1/chat/completions")
    assert json.loads(request.content) == {"model": "workflow/email-reply", "messages": messages, "stream": False}
    assert request.headers.get("Idempotency-Key") == idempotency_key
    assert request.headers["Authorization"] == "Bearer synthetic"
    assert not request.url.params


@pytest.mark.parametrize("status", [403, 404, 409, 429, 500])
def test_run_preserves_http_errors(status):
    def gateway(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "Request refused"}})

    with GatewayClient(
        base_url="https://gateway.invalid", api_key="synthetic", transport=httpx.MockTransport(gateway)
    ) as client:
        with pytest.raises(httpx.HTTPStatusError) as error:
            client.run("email-reply", messages=[{"role": "user", "content": "Review"}])
    assert error.value.response.status_code == status
    assert error.value.response.json() == {"error": {"message": "Request refused"}}


@pytest.mark.parametrize(
    "message",
    [
        {"role": "assistant"},
        {"role": "assistant", "content": None},
        {
            "role": "assistant",
            "tool_calls": [{"id": "call-id", "type": "function", "function": {"name": "prepare", "arguments": "{}"}}],
        },
        {"role": "assistant", "content": 123},
        {"role": "assistant", "content": {"text": "not content"}},
        {"role": "user", "content": "not an assistant"},
    ],
)
def test_run_rejects_missing_or_invalid_final_content(message):
    def gateway(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-litellm-workflow-run-id": "run-id"}, json=_completion(message))

    with GatewayClient(
        base_url="https://gateway.invalid", api_key="synthetic", transport=httpx.MockTransport(gateway)
    ) as client:
        with pytest.raises(ValueError):
            client.run("email-reply", messages=[{"role": "user", "content": "Review"}])


@pytest.mark.parametrize("choices", [[], None])
def test_run_rejects_missing_final_choice(choices):
    def gateway(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"x-litellm-workflow-run-id": "run-id"},
            json={**_completion({"role": "assistant", "content": "Reply"}), "choices": choices},
        )

    with GatewayClient(
        base_url="https://gateway.invalid", api_key="synthetic", transport=httpx.MockTransport(gateway)
    ) as client:
        with pytest.raises(ValueError):
            client.run("email-reply", messages=[{"role": "user", "content": "Review"}])


@pytest.mark.parametrize("output", ["", "Reply"])
@pytest.mark.parametrize("headers", [{}, {"x-litellm-workflow-run-id": ""}, {"x-litellm-workflow-run-id": " "}])
def test_run_rejects_missing_run_id(headers, output):
    def gateway(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, json=_completion({"role": "assistant", "content": output}))

    with GatewayClient(
        base_url="https://gateway.invalid", api_key="synthetic", transport=httpx.MockTransport(gateway)
    ) as client:
        with pytest.raises(ValueError, match="run ID"):
            client.run("email-reply", messages=[{"role": "user", "content": "Review"}])
