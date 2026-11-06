import argparse
import importlib.util
import json
import os
import sys
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from enum import IntEnum
from pathlib import Path
from typing import TextIO

import httpx
from pydantic import TypeAdapter, ValidationError

from liteflow import ChatMessage, GatewayClient, Workflow, bundle_digest, compile_workflow
from liteflow.cli.sanitize import sanitize


class ExitCode(IntEnum):
    success = 0
    error = 1


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)

    def _print_message(self, message: str, file: TextIO | None = None) -> None:
        if message:
            self.output.write(message)


def parser(stdout: TextIO) -> Parser:
    root = Parser(prog="liteflow", allow_abbrev=False)
    root.output = stdout
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("show", "register", "validate", "run", "inspect"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.output = stdout
        command.add_argument("--json", action="store_true")
        command.add_argument("target")
        if name == "register":
            command.add_argument("--workflow-id")
        if name == "inspect":
            command.add_argument("--outputs", action="store_true")
        if name == "run":
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
        403: "execution and trace reads need the owning team's virtual key or a team admin",
        404: "unknown or inaccessible workflow/run",
        409: "the draft changed, re-run register",
        412: "the draft changed, re-run register",
    }.get(response.status_code)
    summary = " ".join(sanitize(str(message if message is not None else response.text)).split())
    result = f"HTTP {response.status_code}: {summary}"
    return result + (f"\n{hint}" if hint else "")


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
        if not args.json:
            try:
                from liteflow.cli import render
            except ImportError:
                raise ValueError("install liteflow[cli]") from None
            target = render.console(stdout, env)
        if args.command == "show":
            workflow = load_workflow(args.target)
            bundle = compile_workflow(workflow)
            if args.json:
                stdout.write(
                    json.dumps({"workflow": workflow.name, "digest": bundle_digest(bundle), "bundle": bundle}) + "\n"
                )
            else:
                render.show(target, workflow, bundle)
            return ExitCode.success
        for name in ("LITELLM_PROXY_URL", "LITELLM_PROXY_API_KEY"):
            if not env.get(name):
                raise ValueError(f"missing {name}")
        with GatewayClient(
            base_url=env["LITELLM_PROXY_URL"],
            api_key=env["LITELLM_PROXY_API_KEY"],
            team_id=env.get("LITELLM_TEAM_ID"),
            transport=transport,
        ) as client:
            if args.command == "register":
                workflow = load_workflow(args.target)
                draft = client.get(args.workflow_id) if args.workflow_id else None
                result = client.register(workflow, draft=draft).model_dump(mode="json", exclude={"bundle"})
            elif args.command == "validate":
                result = client.validate(client.get(args.target))
            elif args.command == "run":
                messages = TypeAdapter(list[ChatMessage]).validate_python(
                    [{"role": "user", "content": args.message}]
                    if args.message is not None
                    else json.loads(Path(args.messages).read_text())
                )
                progress = nullcontext()
                if not args.json and stderr.isatty():
                    progress = render.console(stderr, env).status(render.text(f"invoking workflow/{args.target}"))
                with progress as status:
                    if status is not None:
                        status.update(spinner="dots")
                    run = client.run(args.target, messages=messages, idempotency_key=args.idempotency_key)
                result = run.model_dump(mode="json")
            else:
                trace = client.inspect(args.target)
                result = trace.model_dump(mode="json")
            if args.json:
                stdout.write(json.dumps(result) + "\n")
            elif args.command == "inspect":
                render.inspect(target, trace, args.outputs)
            elif args.command == "run":
                target.print(render.text(run.output))
                target.print(render.text(f"run_id: {run.run_id}"))
            else:
                target.print(render.text(json.dumps(result, indent=2)))
        return ExitCode.success
    except SystemExit as error:
        return ExitCode.success if error.code == 0 else ExitCode.error
    except (httpx.HTTPError, ValueError, OSError, ImportError, SyntaxError) as error:
        if isinstance(error, httpx.HTTPStatusError):
            message = gateway_error(error)
        elif isinstance(error, httpx.RequestError):
            message = f"cannot connect to {error.request.url.host}"
        elif isinstance(error, ValidationError):
            message = "; ".join(f"{item['loc']}: {item['msg']}" for item in error.errors(include_input=False))
        else:
            message = str(error)
        key = env.get("LITELLM_PROXY_API_KEY")
        safe = sanitize(message.replace(key, "[credential removed]") if key else message)
        lines = safe.splitlines()
        stderr.write("error: " + "\n".join(line[:500] for line in lines[:2]) + "\n")
        return ExitCode.error
