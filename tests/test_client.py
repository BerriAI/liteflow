import json

import httpx
import pytest

from liteflow import ChatCompletion, Draft, GatewayClient, Input, Workflow, bundle_digest


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
