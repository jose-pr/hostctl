"""Local subprocess executor."""

from __future__ import annotations

import os
import signal
import subprocess
import typing

from ._common import (
    expired,
    CaptureOutput,
    CommandLine,
    command_text,
    CommandArgument,
    Environment,
    Executor,
    ExecutorCapability,
    ExecutorCommand,
    FileHandle,
    Input,
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
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        # The child was started in its own session, so the process group id
        # is the child's pid and the whole tree is one signal away.
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, AttributeError):
            pass
    try:
        process.kill()
    except OSError:
        pass


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
        # leader, which is what lets one killpg reach every descendant.
        start_new_session=os.name != "nt",
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
    finally accounted for. A `"` inside a value still cannot round-trip
    through a batch `%1` -- that is cmd's own limitation -- but it can no
    longer execute anything.
    """
    if os.name != "nt" or not argv:
        return argv
    program = argv[0]
    if not str(program).casefold().endswith(_BATCH_SUFFIXES):
        return argv
    from ..shell.cmd import _argument

    return " ".join(_argument(value) for value in argv)


class LocalExecutor(Executor[subprocess.CompletedProcess]):
    """Execute direct argv or finalized shell invocations locally."""

    executor_capabilities = frozenset(
        (ExecutorCapability.ARGS, ExecutorCapability.CWD, ExecutorCapability.ENV)
    )

    def __call__(
        self,
        command: ExecutorCommand,
        *args: CommandArgument,
        bufsize: int = -1,
        stdin: typing.Optional[FileHandle] = None,
        stdout: typing.Optional[FileHandle] = None,
        stderr: typing.Optional[FileHandle] = None,
        cwd: typing.Optional[typing.Union[str, os.PathLike[str]]] = None,
        env: typing.Optional[Environment] = None,
        capture_output: CaptureOutput = True,
        check: bool = True,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
        input: Input = None,
        timeout: typing.Optional[float] = None,
        text: typing.Optional[bool] = None,
        **options: object,
    ) -> subprocess.CompletedProcess:
        if options:
            raise TypeError(f"unsupported local executor option: {sorted(options)[0]}")
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
        )
