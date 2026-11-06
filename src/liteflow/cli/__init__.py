import argparse
import importlib.util
import json
import os
import sys
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from enum import Enum, IntEnum
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

import httpx
from pydantic import TypeAdapter, ValidationError

from liteflow import ChatMessage, GatewayClient, Workflow, bundle_digest, compile_workflow
from liteflow.cli.sanitize import sanitize

if TYPE_CHECKING:
    from liteflow.cli import render


class ExitCode(IntEnum):
    success = 0
    error = 1
    usage = 2


class Command(str, Enum):
    show = "show"
    register = "register"
    validate = "validate"
    run = "run"
    inspect = "inspect"


class UsageError(ValueError):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    base_url: str
    api_key: str
    team_id: str | None


def settings(env: Mapping[str, str]) -> Settings:
    if not env.get("LITELLM_PROXY_URL"):
        raise ValueError("set LITELLM_PROXY_URL to the gateway root URL (without /v1)")
    if not env.get("LITELLM_PROXY_API_KEY"):
        raise ValueError(
            "set LITELLM_PROXY_API_KEY to a valid owning-team virtual key, "
            "run also needs the workflow/<name> grant, inspect needs the execution principal or an authorized team admin"
        )
    return Settings(
        base_url=env["LITELLM_PROXY_URL"], api_key=env["LITELLM_PROXY_API_KEY"], team_id=env.get("LITELLM_TEAM_ID")
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Context:
    env: Mapping[str, str]
    stdout: TextIO
    stderr: TextIO
    transport: httpx.BaseTransport | None

    def client(self) -> GatewayClient:
        config = settings(self.env)
        return GatewayClient(
            base_url=config.base_url, api_key=config.api_key, team_id=config.team_id, transport=self.transport
        )

    def renderer(self) -> "render.Console":
        from liteflow.cli import render

        return render.console(self.stdout, self.env)

    def result(self, args: argparse.Namespace, values: Mapping[str, object]) -> ExitCode:
        if args.json:
            self.stdout.write(json.dumps(values) + "\n")
        else:
            from liteflow.cli import render

            render.fields(self.renderer(), values)
        return ExitCode.success


class Parser(argparse.ArgumentParser):
    def __init__(self, *args: object, stdout: TextIO, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._stdout = stdout

    def error(self, message: str) -> None:
        raise UsageError(message)

    def print_help(self, file: TextIO | None = None) -> None:
        super().print_help(file=file if file is not None else self._stdout)


def parser(stdout: TextIO) -> Parser:
    root = Parser(prog="liteflow", allow_abbrev=False, stdout=stdout)
    commands = root.add_subparsers(dest="command", required=True, parser_class=partial(Parser, stdout=stdout))
    for name in Command:
        command = commands.add_parser(name.value, allow_abbrev=False)
        command.add_argument("--json", action="store_true")
        command.add_argument("target")
        if name is Command.register:
            command.add_argument("--workflow-id")
        if name is Command.inspect:
            command.add_argument("--outputs", action="store_true")
        if name is Command.run:
            messages = command.add_mutually_exclusive_group(required=True)
            messages.add_argument("--message")
            messages.add_argument("--messages")
            command.add_argument("--idempotency-key")
    return root


def load_workflow(target: str) -> Workflow:
    path, separator, attribute = target.rpartition(":")
    if not separator or not attribute:
        raise ValueError("expected path.py:attr")
    spec = importlib.util.spec_from_file_location("_liteflow_workflow", Path(path).resolve())
    if spec is None or spec.loader is None:
        raise ValueError("cannot load workflow file")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        workflow = getattr(module, attribute, None)
        if not isinstance(workflow, Workflow):
            raise ValueError("attribute must be a Workflow")
        return workflow
    finally:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous


def gateway_error(error: httpx.HTTPStatusError) -> str:
    response = error.response
    try:
        body = response.json()
    except ValueError:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    nested = body.get("error") if isinstance(body, dict) else None
    message = nested.get("message", detail) if isinstance(nested, dict) else detail
    hint = {
        401: "check LITELLM_PROXY_API_KEY",
        403: "run needs an owning-team key with the workflow/<name> grant, "
        "inspect needs the key that ran it or an owning-team admin using a valid team key",
        404: "unknown or inaccessible workflow/run",
        409: "the draft changed, re-run register",
        412: "the draft changed, re-run register",
    }.get(response.status_code)
    summary = " ".join(sanitize(str(message if message is not None else response.text)).split())
    result = f"HTTP {response.status_code}: {summary}"
    return result + (f"\n{hint}" if hint else "")


def show(args: argparse.Namespace, context: Context) -> ExitCode:
    workflow = load_workflow(args.target)
    bundle = compile_workflow(workflow)
    if args.json:
        return context.result(args, {"workflow": workflow.name, "digest": bundle_digest(bundle), "bundle": bundle})
    from liteflow.cli import render

    render.show(context.renderer(), workflow, bundle)
    return ExitCode.success


def register(args: argparse.Namespace, context: Context) -> ExitCode:
    workflow = load_workflow(args.target)
    with context.client() as client:
        draft = client.get(args.workflow_id) if args.workflow_id else None
        result = client.register(workflow, draft=draft)
    return context.result(args, result.model_dump(mode="json", exclude={"bundle"}))


def validate(args: argparse.Namespace, context: Context) -> ExitCode:
    with context.client() as client:
        result = client.validate(client.get(args.target))
    return context.result(args, result)


def run(args: argparse.Namespace, context: Context) -> ExitCode:
    messages = TypeAdapter(list[ChatMessage]).validate_python(
        [{"role": "user", "content": args.message}]
        if args.message is not None
        else json.loads(Path(args.messages).read_text())
    )
    progress = nullcontext()
    if not args.json and context.stderr.isatty():
        from liteflow.cli import render

        progress = render.console(context.stderr, context.env).status(render.text(f"invoking workflow/{args.target}"))
    with context.client() as client, progress:
        result = client.run(args.target, messages=messages, idempotency_key=args.idempotency_key)
    if args.json:
        return context.result(args, result.model_dump(mode="json"))
    from liteflow.cli import render

    context.renderer().print(render.text(result.output))
    render.console(context.stderr, context.env).print(render.text(f"run_id: {result.run_id}"))
    return ExitCode.success


def inspect(args: argparse.Namespace, context: Context) -> ExitCode:
    with context.client() as client:
        result = client.inspect(args.target)
    if args.json:
        return context.result(args, result.model_dump(mode="json"))
    from liteflow.cli import render

    render.inspect(context.renderer(), result, args.outputs)
    return ExitCode.success


_HANDLERS = {
    Command.show: show,
    Command.register: register,
    Command.validate: validate,
    Command.run: run,
    Command.inspect: inspect,
}


def error_message(error: Exception, env: Mapping[str, str]) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        message = gateway_error(error)
    elif isinstance(error, httpx.RequestError):
        message = f"cannot connect to {error.request.url.host}"
    elif isinstance(error, ValidationError):
        message = ", ".join(f"{item['loc']}: {item['msg']}" for item in error.errors(include_input=False))
    elif isinstance(error, ImportError) and getattr(error, "name", "") in {"rich", "rich.console"}:
        message = "install liteflow[cli]"
    else:
        message = str(error)
    key = env.get("LITELLM_PROXY_API_KEY")
    safe = sanitize(message.replace(key, "[credential removed]") if key else message)
    return "error: " + "\n".join(line[:500] for line in safe.splitlines()[:2]) + "\n"


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] = os.environ,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    transport: httpx.BaseTransport | None = None,
) -> int:
    try:
        args = parser(stdout).parse_args(argv)
        context = Context(env=env, stdout=stdout, stderr=stderr, transport=transport)
        if not args.json:
            context.renderer()
        return _HANDLERS[Command(args.command)](args, context)
    except SystemExit as error:
        return ExitCode.success if error.code == 0 else ExitCode.usage
    except (httpx.HTTPError, ValueError, OSError, ImportError, SyntaxError) as error:
        stderr.write(error_message(error, env))
        return ExitCode.usage if isinstance(error, UsageError) else ExitCode.error
