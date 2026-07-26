"""Small stdlib command-line adapter over the public hostctl API."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import getpass
import json
import os
from pathlib import PurePath
import re
import subprocess
import sys
import threading
import typing

from pathlib_next import Path as NextPath

from .host import Host, HostPath

_URI_OPERAND = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


_OutputStream = typing.Union[typing.TextIO, typing.BinaryIO]
_OutputValue = typing.Union[str, bytes]


def _write(stream: _OutputStream, value: _OutputValue) -> None:
    if value is None:
        return
    if isinstance(value, bytes):
        binary = getattr(stream, "buffer", stream)
        binary.write(value)
    else:
        stream.write(str(value))
    flush = getattr(stream, "flush", None)
    if flush:
        flush()


def _credentials(args: argparse.Namespace) -> dict[str, object]:
    password = os.environ.get("HOSTCTL_PASSWORD")
    if args.ask_password:
        password = getpass.getpass("Password: ")
    return {"password": password} if password is not None else {}


def _open_host(stack: contextlib.ExitStack, uri: str, credentials: dict[str, object]):
    return stack.enter_context(Host(uri, **credentials))


def _path_operand(
    stack: contextlib.ExitStack,
    value: str,
    credentials: dict[str, object],
):
    """Resolve ``URI:PATH`` or an ordinary local filesystem path."""
    if not _URI_OPERAND.match(value) or re.match(r"^[A-Za-z]:[\\/]", value):
        return HostPath(value)
    if "://" in value:
        authority_end = value.find("/", value.find("://") + 3)
        search_end = len(value) if authority_end < 0 else authority_end
        separator = value.rfind(":", 0, search_end)
        if separator <= value.find("://") + 2:
            raise ValueError("remote path operand must be URI:PATH")
    else:
        separator = value.find(":", value.find(":") + 1)
        if separator < 0:
            raise ValueError("remote path operand must be URI:PATH")
    uri, path = value[:separator], value[separator + 1 :]
    if not path:
        raise ValueError("remote path operand requires a path")
    return _open_host(stack, uri, credentials).path(path)


def _command_run(args: argparse.Namespace, stdout, stderr) -> int:
    command = list(args.command)
    if command[:1] == ["--"]:
        command.pop(0)
    if not command:
        raise ValueError("run requires a command after --")
    with Host(args.uri, **_credentials(args)) as host:
        try:
            command_path = host.shell_flavour.command_path(command[0])
        except NotImplementedError:
            command_path = PurePath(command[0])
        result = host.run(
            command_path,
            *command[1:],
            check=False,
            capture_output=True,
        )
    _write(stdout, result.stdout)
    _write(stderr, result.stderr)
    return int(result.returncode)


def _command_ls(args: argparse.Namespace, stdout, stderr) -> int:
    with Host(args.uri, **_credentials(args)) as host:
        for child in host.path(args.path).iterdir():
            _write(stdout, f"{child.name}\n")
    return 0


def _command_cat(args: argparse.Namespace, stdout, stderr) -> int:
    with Host(args.uri, **_credentials(args)) as host:
        _write(stdout, host.path(args.path).read_bytes())
    return 0


def _command_cp(args: argparse.Namespace, stdout, stderr) -> int:
    with contextlib.ExitStack() as stack:
        credentials = _credentials(args)
        source = _path_operand(stack, args.source, credentials)
        target = _path_operand(stack, args.target, credentials)
        # Python 3.14 added stdlib pathlib.Path.copy(), which currently wins
        # LocalPath's mixed MRO. Route explicitly through pathlib_next so local
        # and remote operands retain one copy contract on every supported floor.
        NextPath.copy(
            source,
            target,
            overwrite=args.overwrite,
            recursive=args.recursive,
        )
    return 0


def _command_info(args: argparse.Namespace, stdout, stderr) -> int:
    with Host(args.uri, **_credentials(args)) as host:
        info = host.info()
    value = dataclasses.asdict(info) if dataclasses.is_dataclass(info) else vars(info)
    _write(stdout, json.dumps(value, sort_keys=True) + "\n")
    return 0


def _command_shell(args: argparse.Namespace, stdout, stderr) -> int:
    with Host(args.uri, **_credentials(args)) as host:
        session = host.shell.session(terminal=True)
        stopped = threading.Event()

        def pump() -> None:
            try:
                while not stopped.is_set():
                    data = session.read(65536)
                    if not data:
                        return
                    _write(stdout, data)
            except (OSError, ValueError):
                if not stopped.is_set():
                    raise

        reader = threading.Thread(target=pump, name="hostctl-shell-output", daemon=True)
        reader.start()
        try:
            for line in sys.stdin:
                session.send(line.rstrip("\r\n"))
        except KeyboardInterrupt:
            return 130
        finally:
            try:
                session.send_eof()
            except NotImplementedError:
                pass
            stopped.set()
            session.close()
            reader.join(timeout=1)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hostctl")
    subcommands = parser.add_subparsers(dest="subcommand", required=True)

    def host_command(name: str, handler):
        command = subcommands.add_parser(name)
        command.add_argument("--ask-password", action="store_true")
        command.add_argument("uri")
        command.set_defaults(handler=handler)
        return command

    run = host_command("run", _command_run)
    run.add_argument("command", nargs=argparse.REMAINDER)

    ls = host_command("ls", _command_ls)
    ls.add_argument("path")

    cat = host_command("cat", _command_cat)
    cat.add_argument("path")

    info = host_command("info", _command_info)

    shell = host_command("shell", _command_shell)

    cp = subcommands.add_parser("cp")
    cp.add_argument("--ask-password", action="store_true")
    cp.add_argument("--overwrite", action="store_true")
    cp.add_argument("--recursive", action="store_true")
    cp.add_argument("source")
    cp.add_argument("target")
    cp.set_defaults(handler=_command_cp)
    return parser


def main(
    argv: typing.Optional[typing.Sequence[str]] = None,
    *,
    stdout: typing.Optional[_OutputStream] = None,
    stderr: typing.Optional[_OutputStream] = None,
) -> int:
    """Run the CLI and return its process exit status."""
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args, stdout, stderr))
    except subprocess.TimeoutExpired as exc:
        _write(stderr, f"hostctl: timed out: {exc}\n")
        return 125
    except PermissionError as exc:
        _write(stderr, f"hostctl: permission denied: {exc}\n")
        return 126
    except FileNotFoundError as exc:
        _write(stderr, f"hostctl: not found: {exc}\n")
        return 127
    except (ConnectionError, OSError, ValueError, NotImplementedError) as exc:
        _write(stderr, f"hostctl: {exc}\n")
        return 125


__all__ = ["main"]
