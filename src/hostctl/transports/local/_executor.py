"""Local subprocess executor."""

from __future__ import annotations

import os
import signal
import subprocess
import typing

from ..._windows import no_window_flags
from ...executor import (
    expired,
    CaptureOutput,
    CommandLine,
    command_text,
    CommandArgument,
    CommandEnvironment,
    Executor,
    ExecutorCapability,
    CommandLike,
    StreamTarget,
    CommandInput,
    normalize_environment,
    normalize_input,
    capture_streams,
    reject_stdin_conflict,
    wants_text,
)


def _merged_environment(env):
    """`env` merged over the inherited environment, or None when unset.

    A truly empty environment is deliberately not expressible here: it is a
    separate contract (see the shipped header), and it is dangerous -- a
    replacing environment that omits PATH/SystemRoot stops powershell.exe
    from starting at all on Windows.
    """
    values = normalize_environment(env)
    if values is None:
        return None
    merged = dict(os.environ)
    merged.update(values)
    return merged


#: How long the drain after a tree kill may take before the output is dropped.
#: The tree is already dead by then; this only bounds a pipe still held by a
#: process the kill could not reach.
_DRAIN_BUDGET = 5.0


def _kill_tree(process: "subprocess.Popen") -> None:
    """Kill the child *and its descendants*, best effort.

    `subprocess.run(timeout=)` kills only the direct child. Every non-`Exec`
    hostctl command runs through a shell, so the real work is a grandchild
    holding the stdout/stderr pipes: on Windows the follow-up `communicate()`
    then blocked until that grandchild finished on its own -- the call ran to
    completion and reported a timeout carrying the completed output -- and on
    POSIX the grandchild was orphaned and kept running, so a retry ran a
    second concurrent copy. `docs/guide/contracts.md` requires a best-effort
    attempt to terminate the child.
    """
    if os.name == "nt":
        # No job object: taskkill walks the parent/child links the OS already
        # keeps, needs no handle inheritance, and is present on every
        # supported Windows. Its own failure is not interesting -- the tree
        # may simply have exited -- but it must not hang, hence the bound.
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_DRAIN_BUDGET,
                check=False,
                creationflags=no_window_flags(),
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        # A child started in its own session leads its process group, so the
        # whole tree is one signal away. One that stayed in the caller's
        # session (a terminal exists) does not, and only it is killed.
        try:
            group = os.getpgid(process.pid)
            if group == process.pid:
                os.killpg(group, signal.SIGKILL)
        except (OSError, AttributeError):
            pass
    try:
        process.kill()
    except OSError:
        pass


def _has_controlling_tty() -> bool:
    try:
        descriptor = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    os.close(descriptor)
    return True


def _run_bounded(
    argv,
    *,
    bufsize,
    stdin,
    stdout,
    stderr,
    cwd,
    env,
    check,
    encoding,
    errors,
    input,
    timeout,
    text,
    creationflags,
):
    """`subprocess.run` semantics with a timeout that bounds the whole tree."""
    with subprocess.Popen(
        argv,
        bufsize=bufsize,
        stdin=subprocess.PIPE if input is not None else stdin,
        stdout=stdout,
        stderr=stderr,
        cwd=cwd,
        env=env,
        encoding=encoding,
        errors=errors,
        text=text,
        # POSIX only: a new session makes the child its own process group
        # leader, which is what lets one killpg reach every descendant -- but
        # it also drops the controlling terminal, so it is skipped when this
        # process has one and the child keeps `/dev/tty`.
        start_new_session=os.name != "nt" and not _has_controlling_tty(),
        creationflags=creationflags,
    ) as process:
        try:
            out, err = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            try:
                out, err = process.communicate(timeout=_DRAIN_BUDGET)
            except subprocess.TimeoutExpired:
                out = err = None
            # `orphaned=False`: the tree was killed above, so nothing
            # survives this timeout. One payload shape for every transport.
            raise expired(
                argv,
                timeout,
                output=out,
                stderr=err,
                orphaned=False,
                text=bool(text or encoding or errors),
            ) from None
        except BaseException:
            _kill_tree(process)
            raise
        returncode = process.poll()
    if check and returncode:
        raise subprocess.CalledProcessError(returncode, argv, out, err)
    return subprocess.CompletedProcess(argv, returncode, out, err)


#: Targets Windows dispatches through cmd.exe rather than running directly.
_BATCH_SUFFIXES = (".bat", ".cmd")


def _batch_safe(argv):
    """Return a `.bat`/`.cmd` invocation cmd.exe cannot re-interpret.

    `Exec` promises one program plus argv, never interpreted by a shell, and
    Windows breaks that promise for us: `CreateProcess` dispatches a batch
    target to cmd.exe, which re-parses the C-runtime-quoted line with *cmd*
    rules (the CVE-2024-24576 class -- CPython ships no mitigation for it).
    An argument could close the quoting and start a new cmd command, and
    `%VAR%` was substituted before the batch file saw it.

    The inserted shell is the platform's, so the escaping has to be too: the
    line is built with cmd's own quoting and submitted as a command line.
    That is not an added shell layer; it is the one Windows already added,
    finally accounted for. Each argument is the batch token (see
    `ShellTarget.BATCH`): a Microsoft C runtime reads its `""` as one quote,
    while a consumer using `CommandLineToArgvW` may split a value with
    whitespace after a quote; either way it executes nothing.

    The program is quoted plainly: `CreateProcess` reads that token before
    cmd does, so no escape can serve both, and a `"` or `%` in the path is
    refused.
    """
    if os.name != "nt" or not argv:
        return argv
    program = str(argv[0])
    if not program.casefold().endswith(_BATCH_SUFFIXES):
        return argv
    if '"' in program or "%" in program:
        raise ValueError(
            f"a batch file path must not contain a quote or a percent sign: {program!r}"
        )
    from ...shell import CMD
    from ...shell._vocabulary import ShellTarget

    return " ".join(
        [f'"{program}"']
        + [CMD.argument(value, target=ShellTarget.BATCH) for value in argv[1:]]
    )


class LocalExecutor(Executor[subprocess.CompletedProcess]):
    """Execute direct argv or finalized shell invocations locally.

    Not a ``BufferedExecutor``: ``subprocess`` is the contract that base
    reproduces, and it hands the child real descriptors rather than buffers.
    """

    executor_capabilities = frozenset(
        (ExecutorCapability.ARGS, ExecutorCapability.CWD, ExecutorCapability.ENV)
    )

    def __call__(
        self,
        command: CommandLike,
        *args: CommandArgument,
        bufsize: int = -1,
        stdin: typing.Optional[StreamTarget] = None,
        stdout: typing.Optional[StreamTarget] = None,
        stderr: typing.Optional[StreamTarget] = None,
        cwd: typing.Optional[typing.Union[str, os.PathLike[str]]] = None,
        env: typing.Optional[CommandEnvironment] = None,
        capture_output: CaptureOutput = True,
        check: bool = True,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
        input: CommandInput = None,
        timeout: typing.Optional[float] = None,
        text: typing.Optional[bool] = None,
        **options: object,
    ) -> subprocess.CompletedProcess:
        # Windows only: the caller's own flags (a visible console, a process
        # group); without them a child gets no window unless this process has
        # a console to share.
        creationflags = options.pop("creationflags", None)
        if options:
            raise TypeError(f"unsupported local executor option: {sorted(options)[0]}")
        if creationflags is None:
            creationflags = no_window_flags()
        reject_stdin_conflict(input, stdin)
        stdout, stderr = capture_streams(capture_output, stdout, stderr)
        if isinstance(command, CommandLine):
            # Already quoted for the target's own parser. Windows submits a
            # command LINE to CreateProcess, so this is the one spelling that
            # can carry a cmd script; re-quoting it as argv is what corrupted
            # every value containing a quote or a metacharacter.
            if args:
                raise ValueError("a CommandLine carries no separate arguments")
            if os.name != "nt":
                raise NotImplementedError(
                    "a pre-rendered command line is a Windows spelling; "
                    "POSIX shells render as argv"
                )
            argv = str(command)
        else:
            argv = [command_text(command), *(command_text(value) for value in args)]
            argv = _batch_safe(argv)
        payload = normalize_input(
            input,
            text_mode=wants_text(text, encoding, errors),
            encoding=encoding,
            errors=errors,
        )
        if timeout is not None:
            return _run_bounded(
                argv,
                bufsize=bufsize,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                cwd=os.fspath(cwd) if cwd is not None else None,
                env=_merged_environment(env),
                check=check,
                encoding=encoding,
                errors=errors,
                input=payload,
                timeout=timeout,
                text=text,
                creationflags=creationflags,
            )
        return subprocess.run(
            argv,
            bufsize=bufsize,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            cwd=os.fspath(cwd) if cwd is not None else None,
            # Additive, like every other transport. `subprocess.run(env=...)`
            # replaces the environment, so the one documented cross-provider
            # rule -- "env is additive to the provider's environment" -- held
            # for SSH, WinRM, container and QGA and was inverted for the
            # local executor. A SystemHost that fell back to local therefore
            # handed the child no PATH where the same call over SSH kept it,
            # which on Windows is enough to stop powershell.exe starting.
            env=_merged_environment(env),
            check=check,
            encoding=encoding,
            errors=errors,
            # `encoding`/`errors`/`text` put subprocess's stdin in text mode,
            # where bytes would kill the writer thread and hang the call.
            input=payload,
            timeout=timeout,
            text=text,
            creationflags=creationflags,
        )
