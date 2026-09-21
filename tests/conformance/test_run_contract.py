"""Shared subprocess semantics for every registered provider."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hostctl import Exec

from .providers import fake_providers, live_providers, provider_context


def test_live_provider_registry_is_env_gated():
    providers = live_providers()
    assert providers and providers[0].name == "local"


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_direct_argv_and_capture(provider):
    if "run" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no run capability")
    with provider_context(provider) as host:
        result = host.run(
            Exec(
                sys.executable,
                "-c",
                "import sys; print(sys.argv[1]); print('err', file=sys.stderr)",
                "a & b",
            )
        )
    assert result.returncode == 0
    assert result.stdout == b"a & b\r\n" if os.name == "nt" else b"a & b\n"
    assert result.stderr.startswith(b"err")


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_text_env_and_nonzero_check(provider):
    if "run" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no run capability")
    if "env" not in provider.capabilities:
        pytest.skip(f"{provider.name} does not support environment overrides")
    code = "import os; print(os.environ['HOSTCTL_CONFORMANCE'])"
    environment = {"HOSTCTL_CONFORMANCE": 42}
    if os.name == "nt":
        # subprocess ``env`` is a replacement mapping. CPython 3.9 needs
        # SystemRoot to initialize its Windows entropy provider.
        environment["SystemRoot"] = os.environ["SystemRoot"]
    inherited = "HOSTCTL_INHERITED"
    os.environ[inherited] = "kept"
    try:
        with provider_context(provider) as host:
            result = host.run(
                Exec(sys.executable, "-c", code),
                env=environment,
                text=True,
            )
            assert result.stdout.splitlines()[0] == "42"
            # ADDITIVE, on every transport: asserting only that the injected
            # variable is visible is true under replace semantics too, which
            # is how the local executor came to replace the environment while
            # every other transport added to it.
            survived = host.run(
                Exec(
                    sys.executable,
                    "-c",
                    "import os; print(os.environ.get('HOSTCTL_INHERITED', '<gone>'))",
                ),
                env={"HOSTCTL_CONFORMANCE": "1"},
                text=True,
            )
            assert (
                survived.stdout.splitlines()[0] == "kept"
            ), f"{provider.name} replaced the environment instead of adding to it"
    finally:
        os.environ.pop(inherited, None)
    with provider_context(provider) as host:
        failed = host.run(
            Exec(sys.executable, "-c", "raise SystemExit(3)"), check=False
        )
        assert failed.returncode == 3
        with pytest.raises(subprocess.CalledProcessError):
            host.run(Exec(sys.executable, "-c", "raise SystemExit(4)"))


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_errors_alone_selects_text_mode(provider):
    """`subprocess.run`'s rule: any of text/encoding/errors means `str`.

    Four executors inferred this as `bool(encoding or errors or text)` while
    SSH, PSRP, and the serial host ignored `errors`, so the identical call
    returned `str` or `bytes` depending on which provider a `SystemHost`
    happened to select.
    """
    if "run" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no run capability")
    with provider_context(provider) as host:
        result = host.run(
            Exec(sys.executable, "-c", "print('value')"), errors="replace"
        )
        binary = host.run(Exec(sys.executable, "-c", "print('value')"))
    assert isinstance(result.stdout, str), f"{provider.name} returned bytes"
    assert result.stdout.strip() == "value"
    # Nothing set still means bytes -- the rule adds a case, it does not
    # flip the default.
    assert isinstance(binary.stdout, bytes)


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_uncaptured_output_reaches_the_substituted_stdout(provider, monkeypatch):
    """A buffered transport writes an uncaptured stream to `sys.stdout`.

    The serial host instead discarded the transcript for
    `capture_output=False`, which is invisible without checking where the
    output went.

    `local` is excluded rather than exempted: it hands the child the real
    file descriptor, which is `subprocess.run`'s own behaviour for
    `stdout=None` and cannot be observed by substituting `sys.stdout`. Every
    other transport receives the bytes itself and must then route them.
    """
    if "run" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no run capability")
    if provider.name == "local":
        pytest.skip("local inherits the real stdout descriptor; nothing to route")
    sink = io.StringIO()
    monkeypatch.setattr(sys, "stdout", sink)
    with provider_context(provider) as host:
        result = host.run(
            Exec(sys.executable, "-c", "print('routed')"),
            capture_output=False,
            text=True,
        )
    assert result.stdout is None
    assert "routed" in sink.getvalue()


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_cwd_is_applied_only_when_supported(provider, tmp_path):
    if "run" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no run capability")
    if "cwd" not in provider.capabilities:
        pytest.skip(f"{provider.name} does not support cwd")
    with provider_context(provider) as host:
        result = host.run(
            Exec(sys.executable, "-c", "import pathlib; print(pathlib.Path.cwd())"),
            cwd=tmp_path,
            text=True,
        )
    assert Path(result.stdout.strip()) == tmp_path


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_silent_capture_is_empty_bytes(provider):
    if "run" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no run capability")
    with provider_context(provider) as host:
        result = host.run(Exec(sys.executable, "-c", "pass"))
    assert result.stdout == b""
    assert result.stderr == b""


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_shell_command_shapes_and_operators_remain_explicit(provider):
    if "run" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no run capability")
    with provider_context(provider) as host:
        invocation = host.shell_flavour.invocation("echo first")
        if shutil.which(invocation[0]) is None:
            pytest.skip(f"shell executable {invocation[0]!r} is unavailable")
        # Sequence syntax is portable to POSIX sh and Windows PowerShell 5;
        # raw strings remain shell source rather than argv data.
        raw = host.run("echo first; echo second")
        argv = host.run(("echo", "a & b"))
        joined = host.run(("echo", "one"), ("echo", "two"))
    assert b"first" in raw.stdout and b"second" in raw.stdout
    assert b"a & b" in argv.stdout
    assert b"one" in joined.stdout and b"two" in joined.stdout


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_timeout_and_input_are_subprocess_compatible(provider):
    if "run" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no run capability")
    required = {"input", "timeout"}
    missing = required - provider.capabilities
    if missing:
        pytest.skip(f"{provider.name} does not support {', '.join(sorted(missing))}")
    with provider_context(provider) as host:
        result = host.run(
            Exec(sys.executable, "-c", "import sys; print(sys.stdin.read())"),
            input="payload",
            text=True,
        )
        assert result.stdout.strip() == "payload"
        with pytest.raises(subprocess.TimeoutExpired):
            host.run(
                Exec(sys.executable, "-c", "import time; time.sleep(2)"), timeout=0.01
            )


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_every_timeout_carries_the_same_payload(provider):
    """`subprocess.TimeoutExpired` is the shared type, and the attributes
    hung off it were per-transport: `.orphaned` and `.pid` existed only on
    the SSH and QGA paths, so a supervisor writing
    `if exc.orphaned: alert(exc.pid)` -- what the API header documented --
    crashed with `AttributeError` when the same host timed out over local,
    serial or WinRM. The output payload diverged too: `b''` here, `None`
    there.
    """
    if "run" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no run capability")
    if "timeout" not in provider.capabilities:
        pytest.skip(f"{provider.name} does not support timeout")
    with provider_context(provider) as host:
        with pytest.raises(subprocess.TimeoutExpired) as raised:
            host.run(
                Exec(sys.executable, "-c", "import time; time.sleep(30)"),
                timeout=0.5,
            )

    error = raised.value
    assert isinstance(error.orphaned, bool)
    assert error.pid is None or isinstance(error.pid, int)
    assert error.output is not None
    assert error.stderr is not None


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_the_registry_capabilities_match_the_host_that_was_built(provider):
    """The registry's capability strings are hand-written in parallel with the
    hosts, and every case in this battery skips itself when a capability is
    absent -- so a host whose real capabilities drift from the registry
    silently stops being tested instead of failing.

    Only the four that name a whole surface are compared. The rest (`args`,
    `cwd`, `env`, `input`, `timeout`, `symlink`) mean "this contract is
    exercisable here", which is deliberately broader than the provider's
    NATIVE capability set -- WinRM applies cwd and env by rendering them into
    its script, and must still honour them.
    """
    with provider_context(provider) as host:
        actual = set(host.capabilities)

    claimed = set(provider.capabilities)
    # A console reports `session`, which IS its spawn: `SerialHost.spawn()`
    # opens the exclusive stream. The two names describe the same surface
    # from the two ends, so the guard compares the surface.
    if "session" in claimed or "session" in actual:
        claimed = (claimed - {"spawn"}) | {"session"}
        actual = (actual - {"spawn"}) | {"session"}
    for capability in ("run", "path", "spawn", "session"):
        registry = capability in claimed
        real = capability in actual
        assert registry == real, (
            f"{provider.name}: the registry says {capability}={registry} "
            f"while the host reports {real}"
        )
