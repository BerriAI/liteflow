import json
import subprocess
import sys
from io import StringIO
from pathlib import Path

import httpx
import pytest

from liteflow.cli import main
from liteflow.cli.sanitize import sanitize
from test_client import trace_payload

ENV = {"LITELLM_PROXY_URL": "https://gateway.invalid", "LITELLM_PROXY_API_KEY": "test-credential-do-not-print"}
WORKFLOW = str(Path(__file__).with_name("cli_workflow.py")) + ":workflow"


class Terminal(StringIO):
    def isatty(self):
        return True


def invoke(args, handler=None, env=ENV, stdout=None, stderr=None):
    out = stdout if stdout is not None else StringIO()
    err = stderr if stderr is not None else StringIO()
    transport = httpx.MockTransport(handler) if handler else None
    code = main(args, env=env, stdout=out, stderr=err, transport=transport)
    return code, out.getvalue(), err.getvalue()


def completion(output):
    return httpx.Response(
        200,
        headers={"x-litellm-workflow-run-id": "run-id"},
        json={
            "id": "completion-id",
            "object": "chat.completion",
            "created": 1,
            "model": "workflow/reply",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": output}, "finish_reason": "stop"}],
        },
    )


def test_show_text_without_credentials():
    code, out, err = invoke(["show", WORKFLOW], env={})
    assert code == 0 and err == ""
    for value in (
        "reply",
        "node_1 ChatCompletion model=support-model",
        "node_2 Python fn=finish",
        "$input.messages",
        "node_1.result",
        "timeout=60.0s",
        "retry=1",
        "output: node_2.result",
    ):
        assert value in out


def test_show_json_without_credentials():
    code, out, err = invoke(["show", WORKFLOW, "--json"], env={})
    result = json.loads(out)
    assert code == 0 and err == ""
    assert result["workflow"] == "reply"
    assert len(result["digest"]) == 64
    assert result["bundle"]["nodes"][1]["inputs"] == {"text": {"from": "node_1", "output": "result"}}
    assert result["bundle"]["output"] == {"from": "node_2", "output": "result"}


@pytest.mark.parametrize(
    "nodes,error",
    [
        ("[chat, Sequence(nodes=[Python(fn=finish)])]", "unsupported node kind Sequence"),
        ("[chat, Parallel(branches={'left': [Python(fn=finish)]})]", "unsupported node kind Parallel"),
        (
            "[ForEach(items=['first'], nodes=[Python(fn=finish)])]",
            "unsupported node kind ForEach",
        ),
        (
            "[chat, If(condition=predicate, then=[Python(fn=finish)], otherwise=[Python(fn=finish)])]",
            "unsupported node kind If",
        ),
        (
            "[ChatCompletion(model='model', messages=Input.messages, response_model=Record), "
            "Python(fn=finish, inputs={'text': Input.previous.field('text')})]",
            "unsupported binding kind FieldRef",
        ),
    ],
)
def test_show_unsupported_graph_features_are_explicit_and_json_still_works(tmp_path, nodes, error):
    path = tmp_path / "graph.py"
    path.write_text(
        "from liteflow import ChatCompletion, ForEach, If, Input, Parallel, Python, Sequence, Workflow\n"
        "from pydantic import BaseModel\n"
        "from cli_workflow import finish\n"
        "class Record(BaseModel):\n"
        "    text: str\n"
        "def predicate(text: str) -> bool:\n"
        "    return bool(text)\n"
        "chat = ChatCompletion(model='model', messages=Input.messages)\n"
        f"workflow = Workflow(name='graph', nodes={nodes})\n"
    )
    target = f"{path}:workflow"
    code, out, err = invoke(["show", target], env={})
    assert code == 1 and out == ""
    assert err == f"error: {error}\n"
    code, out, err = invoke(["show", target, "--json"], env={})
    assert code == 0 and err == ""
    assert json.loads(out)["bundle"]["contract_version"] == "2"


def test_run_json():
    def gateway(request):
        assert json.loads(request.content) == {
            "model": "workflow/reply",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        }
        assert request.headers["Idempotency-Key"] == "retry"
        return completion("\x1b[31m[bold]result")

    code, out, err = invoke(["run", "reply", "--message", "hello", "--idempotency-key", "retry", "--json"], gateway)
    assert code == 0 and err == ""
    assert "\x1b" not in out
    assert json.loads(out) == {"run_id": "run-id", "output": "\x1b[31m[bold]result"}


def test_run_text_sanitizes_terminal_sequences_without_interpreting_markup():
    output = "\x1b]8;;http://x\x07click\x1b]8;;\x07\x1b[31m[bold red]x[/]"
    code, out, err = invoke(["run", "reply", "--message", "hello"], lambda request: completion(output))
    assert code == 0 and err == "run_id: run-id\n"
    assert out == "click[bold red]x[/]\n"


def test_no_color_on_tty():
    code, out, err = invoke(
        ["run", "reply", "--message", "hello"],
        lambda request: completion("hello"),
        env={**ENV, "NO_COLOR": ""},
        stdout=Terminal(),
        stderr=Terminal(),
    )
    assert code == 0
    assert "hello" in out
    assert "\x1b" not in out + err


@pytest.mark.parametrize("json_mode", [False, True])
def test_inspect_failed_and_unfinished_steps(json_mode):
    payload = trace_payload(state="failed")
    payload["steps"][0]["error"] = json.dumps({"message": "\x1b[31m[bold]bad output[/]"})
    payload["steps"].append(
        {
            **payload["steps"][0],
            "node_id": "node_2",
            "state": "running",
            "finished_at": None,
            "error": "raw\x1b]8;;http://x\x1b\\failure",
            "input": "[bold]input",
            "output": None,
        }
    )
    code, out, err = invoke(
        ["inspect", "run-id", "--outputs"] + (["--json"] if json_mode else []),
        lambda request: httpx.Response(200, json=payload),
    )
    assert code == 0 and err == ""
    assert "\x1b" not in out
    if json_mode:
        assert json.loads(out) == payload
    else:
        for value in ("snapshot-id", "failed", "False", "node_1", "node_2", "2s", "10/5", "bad", "null", "input"):
            assert value in out
        assert "redacted" not in out


@pytest.mark.parametrize(
    "status,hint",
    [
        (401, "LITELLM_PROXY_API_KEY"),
        (403, "owning-team key with the workflow/<name> grant"),
        (404, "unknown or inaccessible"),
        (409, "re-run register"),
        (412, "re-run register"),
    ],
)
def test_gateway_errors_are_actionable_and_hide_credentials(status, hint):
    code, out, err = invoke(
        ["inspect", "run-id"],
        lambda request: httpx.Response(status, json={"error": {"message": "denied " + ENV["LITELLM_PROXY_API_KEY"]}}),
    )
    assert code == 1 and out == ""
    assert str(status) in err and hint in err
    assert ENV["LITELLM_PROXY_API_KEY"] not in out + err


@pytest.mark.parametrize("envelope", ["detail", "error"])
@pytest.mark.parametrize("error_code", ["not_found", None, ""])
def test_workflow_gateway_error_envelopes(envelope, error_code):
    failure = {"type": "workflow_error", "message": "Workflow not found", "param": None}
    if error_code is not None:
        failure["code"] = error_code
    code, out, err = invoke(
        ["inspect", "run-id"],
        lambda request: httpx.Response(404, json={envelope: failure}),
    )
    expected_code = " not_found" if error_code else ""
    assert code == 1 and out == ""
    assert err == f"error: HTTP 404{expected_code}: Workflow not found\nunknown or inaccessible workflow/run\n"


@pytest.mark.parametrize("missing", ["LITELLM_PROXY_URL", "LITELLM_PROXY_API_KEY"])
def test_missing_environment(missing):
    code, out, err = invoke(["inspect", "run-id", "--json"], env={k: v for k, v in ENV.items() if k != missing})
    assert code == 1 and out == "" and f"set {missing}" in err
    assert ("without /v1" if missing == "LITELLM_PROXY_URL" else "workflow/<name> grant") in err


def test_no_api_key_flag():
    code, out, err = invoke(["inspect", "run-id", "--api-key", "forbidden"])
    assert code == 2 and out == ""
    assert "unrecognized arguments" in err


@pytest.mark.parametrize("replace", [False, True])
def test_register_and_validate_use_draft_etag(replace):
    requests = []
    draft = {
        "name": "reply",
        "workflow_id": "draft-id",
        "team_id": "team-id",
        "etag": "etag",
        "digest": "digest",
        "bundle": {},
        "published_snapshot_id": None,
    }

    def gateway(request):
        requests.append(request)
        if request.url.path.endswith("/validate"):
            assert request.headers["If-Match"] == "etag"
            return httpx.Response(200, json={"valid": True})
        if request.method == "PUT":
            assert request.headers["If-Match"] == "etag"
        return httpx.Response(200, json=draft)

    code, out, err = invoke(
        ["register", WORKFLOW, "--json"] + (["--workflow-id", "draft-id"] if replace else []), gateway
    )
    assert code == 0 and err == ""
    assert json.loads(out) == {k: v for k, v in draft.items() if k != "bundle"}
    assert [request.method for request in requests] == (["GET", "PUT"] if replace else ["POST"])
    code, out, err = invoke(["validate", "draft-id", "--json"], gateway)
    assert code == 0 and err == "" and json.loads(out) == {"valid": True}


def test_messages_file_validates_at_boundary(tmp_path):
    path = tmp_path / "messages.json"
    path.write_text('[{"role":"user","content":"file message"}]')

    def gateway(request):
        assert json.loads(request.content)["messages"][0]["content"] == "file message"
        return completion("")

    code, out, err = invoke(["run", "reply", "--messages", str(path), "--json"], gateway)
    assert code == 0 and err == "" and json.loads(out)["output"] == ""
    path.write_text('{"not":"a list"}')
    code, out, err = invoke(["run", "reply", "--messages", str(path), "--json"], gateway)
    assert code == 1 and out == "" and "error:" in err


def test_connection_error_only_names_host():
    def gateway(request):
        raise httpx.ConnectError("unsafe headers " + ENV["LITELLM_PROXY_API_KEY"], request=request)

    code, out, err = invoke(["inspect", "run-id", "--json"], gateway)
    assert code == 1 and out == ""
    assert err == "error: cannot connect to gateway.invalid\n"


def test_progress_is_only_invocation_status_on_interactive_stderr():
    stderr = Terminal()

    def gateway(request):
        assert "node_1" not in stderr.getvalue()
        return completion("done")

    code, out, err = invoke(["run", "reply", "--message", "hello"], gateway, stderr=stderr)
    assert code == 0 and out == "done\n"
    assert "run_id: run-id" in err


@pytest.mark.parametrize("body", [{"detail": "denied\n" * 200}, None])
def test_error_body_is_bounded_and_preserves_hint(body):
    response = httpx.Response(403, json=body) if body is not None else httpx.Response(403, text="denied " * 200)
    code, out, err = invoke(["inspect", "run-id", "--json"], lambda request: response)
    assert code == 1 and out == ""
    assert len(err.splitlines()) == 2
    assert len(err.splitlines()[0]) <= 507
    assert "owning-team key with the workflow/<name> grant" in err


def test_show_bad_attribute_reports_error():
    code, out, err = invoke(["show", WORKFLOW.replace(":workflow", ":missing"), "--json"], env={})
    assert code == 1 and out == ""
    assert "attribute must be a Workflow" in err


def test_empty_trace():
    payload = {**trace_payload(), "steps": []}
    code, out, err = invoke(["inspect", "run-id"], lambda request: httpx.Response(200, json=payload))
    assert code == 0 and err == ""
    assert "run-id" in out and "node_1" not in out


@pytest.mark.parametrize("value", ["\x1b]title\x07ok", "\x1b]title\x1b\\ok", "\x1b[31mok\x1b[0m", "\x1b(Bok"])
def test_sanitizer_removes_escape_sequences(value):
    assert sanitize(value) == "ok"
    assert sanitize("a\x00\x85\x7fb\n\tc") == "ab\n\tc"


def test_sanitizer_removes_bidi_controls():
    assert sanitize("a\u202a\u202b\u202c\u202d\u202eb\u2066\u2067\u2068\u2069c") == "abc"


@pytest.mark.parametrize("args", [["--help"], ["run", "--help"]])
def test_help_goes_to_injected_stdout(args):
    code, out, err = invoke(args, env={})
    assert code == 0 and err == ""
    assert "usage:" in out


@pytest.mark.parametrize("args", [[], ["unknown"], ["run", "reply"], ["inspect"]])
def test_usage_errors_go_to_stderr(args):
    code, out, err = invoke(args, env={})
    assert code == 2 and out == ""
    assert err.startswith("error:")


@pytest.mark.parametrize("command", ["register", "validate"])
def test_draft_text_is_aligned_sanitized_fields(command):
    draft = {
        "name": "[bold]reply\u202e\x1b[31m",
        "workflow_id": "draft-id",
        "team_id": "team-id",
        "etag": "etag",
        "digest": "digest",
        "bundle": {},
        "published_snapshot_id": None,
    }

    def gateway(request):
        return httpx.Response(
            200,
            json={"valid": True, "message": "[bold]ok\x1b[31m\u2066"}
            if request.url.path.endswith("/validate")
            else draft,
        )

    code, out, err = invoke([command, WORKFLOW if command == "register" else "draft-id"], gateway)
    assert code == 0 and err == ""
    assert "\x1b" not in out and "\u202e" not in out and "\u2066" not in out
    lines = out.splitlines()
    assert len({line.index(":") for line in lines}) == 1
    assert "[bold]" in out
    assert ("published_snapshot_id: None" if command == "register" else "valid  : True") in out


def test_json_mode_does_not_import_rich_and_text_reports_missing_extra():
    script = """
import importlib.abc
import sys
from io import StringIO
class NoRich(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "rich" or fullname.startswith("rich."):
            raise ModuleNotFoundError("blocked", name="rich")
sys.meta_path.insert(0, NoRich())
from liteflow.cli import main
out, err = StringIO(), StringIO()
assert main(["show", sys.argv[1], "--json"], env={}, stdout=out, stderr=err) == 0
assert '"workflow": "reply"' in out.getvalue()
assert main(["show", sys.argv[1]], env={}, stdout=out, stderr=err) == 1
assert "install liteflow[cli]" in err.getvalue()
"""
    result = subprocess.run([sys.executable, "-c", script, WORKFLOW], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
