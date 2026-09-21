"""Small stdlib command-line adapter over the public hostctl API."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import getpass
import json
import os
import re
import subprocess
import sys
import threading
import time
import typing

from pathlib_next import Path as NextPath

from .host import Exec, Host, HostConfig, HostPath

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


def _usable_credentials(uri: str, credentials: dict[str, object]) -> dict[str, object]:
    """Keep only what the target scheme can accept.

    `HOSTCTL_PASSWORD` is ambient: the guide tells the user to export it, so
    it is set for a shell session rather than for one command. Splatting it
    into every `Host(...)` made `hostctl run local: ...` -- the guide own
    example -- fail with `unknown credential argument: password` and exit 125
    the moment that variable existed, because `LocalConfig` declares no
    credentials at all. A credential named explicitly by a config still fails
    closed; only the ambient one is filtered.
    """
    try:
        accepted = HostConfig.supported_credentials(uri)
    except ValueError:
        return credentials
    if accepted is None:
        return credentials
    return {key: value for key, value in credentials.items() if key in accepted}


def _open_host(stack: contextlib.ExitStack, uri: str, credentials: dict[str, object]):
    return stack.enter_context(Host(uri, **_usable_credentials(uri, credentials)))


def _port_colon(value: str, scheme_end: int, authority_end: int) -> int:
    """Index of the authority's port colon, or ``-1`` when it has none.

    The port colon is the first colon of the host part: userinfo colons sit
    before the ``@``, and an IPv6 literal keeps its colons inside brackets.
    """
    userinfo_end = value.rfind("@", scheme_end, authority_end)
    host_start = scheme_end if userinfo_end < 0 else userinfo_end + 1
    if value[host_start : host_start + 1] == "[":
        bracket_end = value.find("]", host_start, authority_end)
        if bracket_end < 0:
            return -1
        host_start = bracket_end + 1
    return value.find(":", host_start, authority_end)


def _path_operand(
    stack: contextlib.ExitStack,
    value: str,
    credentials: dict[str, object],
):
    """Resolve ``URI:PATH`` or an ordinary local filesystem path."""
    if not _URI_OPERAND.match(value) or re.match(r"^[A-Za-z]:[\\/]", value):
        return HostPath(value)
    if "://" in value:
        scheme_end = value.find("://") + 3
        authority_end = value.find("/", scheme_end)
        search_end = len(value) if authority_end < 0 else authority_end
        separator = value.rfind(":", 0, search_end)
        if separator < scheme_end:
            raise ValueError(
                f"remote path operand must be URI:PATH: {value!r} has no "
                "path separator"
            )
        if (
            separator == _port_colon(value, scheme_end, search_end)
            and value[separator + 1 : search_end].isdigit()
        ):
            # `ssh://host:2222/x` has no path colon, so the last colon before
            # the authority's slash is the port's. Splitting there yields
            # `ssh://host` plus the relative path `2222/x` -- the wrong host
            # and the wrong file, with nothing said. The grammar wants
            # `ssh://host:2222:/x`.
            raise ValueError(
                f"remote path operand must be URI:PATH: {value!r} ends at a "
                "port, not a path (write it as ssh://host:2222:/x)"
            )
        uri, path = value[:separator], value[separator + 1 :]
    else:
        # An opaque URI has no authority, so the scheme's own colon ends it
        # and everything after is the path: `local:/tmp/x` is the spelling
        # every other subcommand documents, and looking for a SECOND colon
        # rejected it outright. The scheme keeps its colon -- `local` is not
        # a URI, `local:` is -- and a path containing colons survives.
        scheme_end = value.find(":")
        uri, path = value[: scheme_end + 1], value[scheme_end + 1 :]
    if not path:
        raise ValueError(
            f"remote path operand requires a path: {value!r} names a host "
            "and no file (write it as URI:PATH, e.g. local:/tmp/x or "
            "ssh://host:22:/srv/app)"
        )
    return _open_host(stack, uri, credentials).path(path)


def _command_run(args: argparse.Namespace, stdout, stderr) -> int:
    command = list(args.command)
    if command[:1] == ["--"]:
        command.pop(0)
    if not command:
        raise ValueError("run requires a command after --")
    with Host(args.uri, **_usable_credentials(args.uri, _credentials(args))) as host:
        # `Exec` marks direct execution and takes the program verbatim, so the
        # operand needs no path flavour: a bare name resolves through the
        # target's PATH and an absolute path is used as given.
        result = host.run(
            Exec(command[0], *command[1:]),
            check=False,
            capture_output=True,
        )
    _write(stdout, result.stdout)
    _write(stderr, result.stderr)
    return int(result.returncode)


def _command_ls(args: argparse.Namespace, stdout, stderr) -> int:
    with Host(args.uri, **_usable_credentials(args.uri, _credentials(args))) as host:
        for child in host.path(args.path).iterdir():
            _write(stdout, f"{child.name}\n")
    return 0


def _command_cat(args: argparse.Namespace, stdout, stderr) -> int:
    with Host(args.uri, **_usable_credentials(args.uri, _credentials(args))) as host:
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
    with Host(args.uri, **_usable_credentials(args.uri, _credentials(args))) as host:
        info = host.info()
    value = dataclasses.asdict(info) if dataclasses.is_dataclass(info) else vars(info)
    _write(stdout, json.dumps(value, sort_keys=True) + "\n")
    return 0


def _command_shell(args: argparse.Namespace, stdout, stderr) -> int:
    with Host(args.uri, **_usable_credentials(args.uri, _credentials(args))) as host:
        try:
            session = host.shell.session(terminal=True)
        except NotImplementedError:
            # A raw serial console cannot allocate a PTY, and a URI-built
            # serial host always gets the raw profile -- so `hostctl shell`
            # against a `serial:` URI could never open a session at all. A
            # console without a terminal is still a console.
            session = host.shell.session()
        stopped = threading.Event()

        def pump() -> None:
            try:
                while not stopped.is_set():
                    data = session.read(65536)
                    if data:
                        _write(stdout, data)
                        continue
                    # An empty read is EOF on a pipe-like transport. On a
                    # serial line it is an idle moment: the read timeout
                    # expired and the device said nothing, which used to end
                    # the pump and leave a live console silent. The process
                    # itself is the authority on whether anything is left.
                    if getattr(session, "returncode", 0) is not None:
                        return
                    time.sleep(0.01)
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


_EPILOG = """\
connection URIs:
  local:                      this machine
  ssh://user@host:22          SSH (asyncssh)
  winrm://user@host:5985      WinRM
  psrp://user@host:5985       PowerShell Remoting
  docker://container          a running container
  qemu+libvirt:///domain      a guest, through libvirt
  serial:///dev/ttyUSB0       a raw serial console

remote path operands (cp):
  URI:PATH -- the colon after the URI separates it from the path.
  local:/tmp/x   ssh://host:/srv/app   ssh://host:2222:/srv/app
  A bare filesystem path (./x, C:\\x) is this machine.

credentials:
  A password comes from the HOSTCTL_PASSWORD environment variable, or from
  --ask-password, and never from the command line, where it would reach the
  process table and the shell history. It is offered only to schemes that
  accept one.

exit status:
  0    the command succeeded (for `run`, the remote command's own status)
  125  connection, timeout, usage, or an unsupported operation
  126  permission denied
  127  not found
  130  interrupted
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hostctl",
        description="Run commands and move files on a host named by a URI.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subcommands = parser.add_subparsers(dest="subcommand", required=True)

    def host_command(name: str, handler, help_text: str):
        command = subcommands.add_parser(name, help=help_text, description=help_text)
        command.add_argument(
            "--ask-password",
            action="store_true",
            help="prompt for a password instead of reading HOSTCTL_PASSWORD",
        )
        command.add_argument("uri", help="connection URI (see `hostctl --help`)")
        command.set_defaults(handler=handler)
        return command

    run = host_command("run", _command_run, "run one command directly, without a shell")
    run.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="the program and its arguments, after `--`",
    )

    ls = host_command("ls", _command_ls, "list a directory on the host")
    ls.add_argument("path", help="a path on the host, not a URI:PATH operand")

    cat = host_command("cat", _command_cat, "write a file's bytes to stdout")
    cat.add_argument("path", help="a path on the host, not a URI:PATH operand")

    host_command("info", _command_info, "print the host's identity as JSON")

    host_command("shell", _command_shell, "open an interactive session on the host")

    cp = subcommands.add_parser(
        "cp",
        help="copy between hosts",
        description="Copy a file or tree; either operand may be URI:PATH.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cp.add_argument(
        "--ask-password",
        action="store_true",
        help="prompt for a password instead of reading HOSTCTL_PASSWORD",
    )
    cp.add_argument(
        "--overwrite", action="store_true", help="replace an existing target"
    )
    cp.add_argument("--recursive", action="store_true", help="copy a whole tree")
    cp.add_argument("source", help="URI:PATH, or a local filesystem path")
    cp.add_argument("target", help="URI:PATH, or a local filesystem path")
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
