"""The WinRM transport service and its native, pywinrm and PSRP legs."""

from __future__ import annotations


import logging
import os
import subprocess
import threading
import typing


from ._native import NativeWinRMSession
from ._pywinrm import WinRMExecutor, WinRMSession, _PywinrmStdinSession
from ..._extras import has_psrp
from ._psrp import PSRPExecutor, RunspaceSession
from ...provider._selector import ProviderSelector
from ...executor import (
    CaptureOutput,
    CommandEnvironment,
    StreamTarget,
    CommandInput,
    StrPath,
)
from ...shell._grammar import Command, starts_direct_command
from ...host._info import HostInfo, parse_host_info
from ...shell import POWERSHELL
from ...shell._flavour import ShellFlavour
from ...shell._vocabulary import wants_epilogue
from ._config import _OPERATION_TIMEOUT, _READ_TIMEOUT, WinRMConfig
from ...host._tls import _trust_session
from ...host._verify import _ca_bundle
from ...executor import _OMITTED, _Omitted
from ._path import WinRMPath, WinRMPathBackend

log = logging.getLogger(__name__)


#: Script bytes one remote call may carry. Every leg delivers the script on
#: stdin or as a message, so no command line bounds it.
_SCRIPT_BUDGET = 256_000


class _Leg:
    """What one way of reaching the host owns: its lazy session and executor.

    The executor is built once, with the status and deadline behaviour the
    leg's session has, so the transport has no second source of truth for them.
    """

    name: str
    executor: typing.Union[WinRMExecutor, PSRPExecutor]

    def __init__(self, config: WinRMConfig) -> None:
        self.config = config
        #: Guards lazy construction: two threads on a fresh leg each built a
        #: session, and the loser's was opened and never closed.
        self._lock = threading.RLock()

    @property
    def opened(self) -> bool:
        raise NotImplementedError

    def connect(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class _SessionLeg(_Leg):
    """A leg that runs scripts through a `run_ps(script)` session."""

    def __init__(self, config: WinRMConfig) -> None:
        super().__init__(config)
        self._session: typing.Optional[WinRMSession] = None

    def _build(self) -> WinRMSession:
        raise NotImplementedError

    @property
    def session(self) -> WinRMSession:
        with self._lock:
            if self._session is None:
                self._session = self._build()
            return self._session

    @property
    def opened(self) -> bool:
        return self._session is not None

    def connect(self) -> None:
        _ = self.session

    def close(self) -> None:
        with self._lock:
            session, self._session = self._session, None
        close = getattr(session, "close", None)
        if close is not None:
            close()


class _NativeLeg(_SessionLeg):
    """The local PowerShell relays `Invoke-Command` with the current logon."""

    name = "native"

    def __init__(self, config: WinRMConfig) -> None:
        super().__init__(config)
        # The local powershell.exe relays the call, so it has a wall clock to
        # bound and reports the remote status on its own marker line.
        self.executor = WinRMExecutor(
            lambda: self.session,
            lambda: self.config.timeouts.total,
            manages_status=True,
            accepts_timeout=True,
        )

    def _build(self) -> WinRMSession:
        if os.name != "nt":
            raise ValueError(
                "current-context WinRM requires a Windows client; pass password="
            )
        domain = os.environ.get("USERDOMAIN", "").strip()
        username = os.environ.get("USERNAME", "").strip()
        if not username:
            raise ValueError(
                "current-context WinRM requires USERNAME/USERDOMAIN; pass password="
            )
        configured = self.config.username.casefold()
        candidates = {username.casefold()}
        if domain:
            candidates.add(f"{domain}\\{username}".casefold())
            candidates.add(f"{username}@{domain}".casefold())
        if configured not in candidates:
            raise ValueError(
                "password-free native WinRM requires the current Windows user"
            )
        if self.config.auth not in {"ntlm", "kerberos"}:
            raise NotImplementedError(
                "native current-context WinRM supports ntlm/kerberos only"
            )
        return NativeWinRMSession(
            self.config.host,
            ssl=self.config.tls,
            port=self.config.port,
            timeout=self.config.timeouts.total,
            # Against a host that accepts TCP and never answers (Windows
            # PowerShell 5.1), -OpenTimeout 5 s / 60 s / 180 s (the default)
            # ended the call after 18.5 / 70.5 / 190.4 s.
            open_timeout=self.config.timeouts.connect,
            transport=self.config.auth,
            verify=self.config.verify,
            message_encryption=self.config.message_encryption,
        )


class _PywinrmLeg(_SessionLeg):
    """pywinrm's protocol, with the script on stdin."""

    name = "pywinrm"

    def __init__(self, config: WinRMConfig) -> None:
        super().__init__(config)
        # `transport_timeout` is the read limit: a timeout pywinrm raises
        # itself is one HTTP request outliving it.
        self.executor = WinRMExecutor(
            lambda: self.session,
            lambda: float(_READ_TIMEOUT),
            accepts_timeout=True,
        )

    def _build(self) -> WinRMSession:
        try:
            import winrm
        except ImportError as exc:
            raise ImportError(
                "WinRM support requires the 'winrm' extra: "
                "pip install hostctl[winrm]"
            ) from exc
        verify = self.config.verify
        trust: typing.Dict[str, object] = {}
        if isinstance(verify, str):
            # A given `ca_trust_path` replaces both `REQUESTS_CA_BUNDLE` and
            # certifi; pywinrm hands it to `requests` as `verify=`.
            trust["ca_trust_path"] = _ca_bundle(verify)
        elif verify is True and self.config.tls:
            # `None` stops pywinrm reading the CA variables itself: the
            # installed trust reads them.
            trust["ca_trust_path"] = None
        # `winrm.Session` still owns endpoint and auth handling; only its
        # protocol is used, because `run_ps` goes through cmd.exe.
        protocol = winrm.Session(
            self.config.endpoint,
            auth=(self.config.username, self.config.password),
            transport=self.config.auth,
            # pywinrm's own keywords.
            server_cert_validation="ignore" if verify is False else "validate",
            message_encryption=self.config.message_encryption,
            operation_timeout_sec=_OPERATION_TIMEOUT,
            read_timeout_sec=_READ_TIMEOUT,
            **trust,
        ).protocol
        if verify is True and self.config.tls:
            _trust_pywinrm(protocol.transport)
        connect = self.config.timeouts.connect
        if connect is not None:
            # pywinrm hands this attribute to `requests` as the `timeout=` of
            # every request, so a `(connect, read)` pair splits the two.
            protocol.transport.read_timeout_sec = (connect, _READ_TIMEOUT)
        return _PywinrmStdinSession(protocol, timeout=self.config.timeouts.total)


def _trust_pywinrm(transport: typing.Any) -> None:
    """Make every session a pywinrm `Transport` builds trust the OS store.

    pywinrm builds a fresh session after `close_session()`, and with message
    encryption sends its first request from inside `build_session()`, through
    `setup_encryption()` -- so both are wrapped, before any request is sent.
    """
    build_session = transport.build_session
    setup_encryption = transport.setup_encryption

    def _setup_encryption(session: typing.Any) -> None:
        _trust_session(session)
        setup_encryption(session)

    def _build_session() -> typing.Any:
        session = build_session()
        _trust_session(session)
        return session

    transport.setup_encryption = _setup_encryption
    transport.build_session = _build_session


class _PSRPLeg(_Leg):
    """A persistent PSRP runspace; the executor opens a pipeline in it."""

    name = "psrp"

    def __init__(self, config: WinRMConfig) -> None:
        super().__init__(config)
        self._runspace: typing.Optional[RunspaceSession] = None
        self.executor = PSRPExecutor(
            lambda: self.runspace(), lambda: self.config.timeouts.total
        )

    def runspace(self) -> RunspaceSession:
        with self._lock:
            if self._runspace is None:
                self._runspace = RunspaceSession(self.config)
            runspace = self._runspace
        runspace.connect()
        return runspace

    @property
    def opened(self) -> bool:
        return self._runspace is not None

    def connect(self) -> None:
        self.runspace()

    def close(self) -> None:
        with self._lock:
            runspace, self._runspace = self._runspace, None
        if runspace is not None:
            runspace.close()


_LEGS: typing.Dict[str, typing.Callable[[WinRMConfig], _Leg]] = {
    "native": _NativeLeg,
    "pywinrm": _PywinrmLeg,
    "psrp": _PSRPLeg,
}


def _select_leg(config: WinRMConfig) -> str:
    """Pick the leg once: an explicit `provider`, else by credentials.

    `auto` is native for a password-free config on a Windows client (the
    current logon), else PSRP when `hostctl[psrp]` imports, else pywinrm.
    Installing the psrp extra never takes the native path away.
    """
    if config.provider != "auto":
        return config.provider
    if config.password is None:
        if os.name == "nt":
            return "native"
        if config.auth != "kerberos":
            raise ValueError(
                "current-context WinRM requires a Windows client; pass password="
            )
    return "psrp" if has_psrp() else "pywinrm"


class _WinRMTransport:
    """A Windows host reached through PowerShell over WinRM."""

    def __init__(self, config: WinRMConfig) -> None:
        self.config = config
        self._leg = _LEGS[_select_leg(config)](config)
        self._path_backend = WinRMPathBackend(
            self.run,
            max_script_bytes=_SCRIPT_BUDGET,
        )

    @property
    def capabilities(self) -> typing.FrozenSet[str]:
        capabilities = {"run", "path"}
        capabilities.update(
            getattr(item, "value", item)
            for item in self._leg.executor.executor_capabilities
        )
        if self._leg.name == "psrp":
            capabilities.add("runspace")
        return frozenset(capabilities)

    @property
    def shell_flavour(self) -> ShellFlavour:
        return POWERSHELL

    @property
    def executor(self) -> typing.Union[WinRMExecutor, PSRPExecutor]:
        return self._leg.executor

    def info(self) -> HostInfo:
        return parse_host_info(
            self.run(
                POWERSHELL.info_script,
                check=False,
                encoding="utf-8",
            ).stdout
        )

    def runspace(self) -> RunspaceSession:
        """Return a persistent typed PSRP runspace session."""
        if not isinstance(self._leg, _PSRPLeg):
            raise NotImplementedError(
                "PSRP runspaces are unavailable; install hostctl[psrp] on Python 3.10+"
            )
        return self._leg.runspace()

    def connect(self) -> None:
        target = ProviderSelector.redact(self.config.connection_uri)
        log.debug("opening WinRM %s session to %s", self._leg.name, target)
        self._leg.connect()
        log.debug("WinRM %s session to %s established", self._leg.name, target)

    def close(self) -> None:
        if self._leg.opened:
            log.debug(
                "closing WinRM %s session to %s",
                self._leg.name,
                ProviderSelector.redact(self.config.connection_uri),
            )
        self._leg.close()

    def path(
        self, *segments: StrPath, backend: typing.Optional[str] = None
    ) -> WinRMPath:
        if backend not in (None, "winrm"):
            raise ValueError("WinRM path backend must be 'winrm'")
        if (
            len(segments) >= 1
            and isinstance(segments[0], str)
            and len(segments[0]) == 2
            and segments[0].endswith(":")
        ):
            segments = (segments[0] + "\\", *segments[1:])
        return WinRMPath(*segments, backend=self._path_backend)

    def run(
        self,
        *cmds: Command,
        bufsize: int = -1,
        executable: typing.Optional[str] = None,
        stdin: typing.Optional[StreamTarget] = None,
        stdout: typing.Optional[StreamTarget] = None,
        stderr: typing.Optional[StreamTarget] = None,
        cwd: typing.Optional[StrPath] = None,
        env: typing.Optional[CommandEnvironment] = None,
        capture_output: CaptureOutput = True,
        check: bool = True,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
        input: CommandInput = None,
        timeout: typing.Union[float, None, _Omitted] = _OMITTED,
        text: typing.Optional[bool] = None,
    ) -> subprocess.CompletedProcess:
        """Run on the selected leg; an omitted `timeout` is the config's total."""
        if executable is not None:
            raise NotImplementedError(
                "WinRM provider run supports PowerShell only and does not accept executable"
            )
        direct = starts_direct_command(cmds)
        if direct is not None:
            command, args = direct
            cmds = ((command, *args),)
        # No epilogue when the session reports its own status: the native
        # wrapper carries the remote code back on its marker line, and the
        # flavour's epilogue ends the remote pipeline BEFORE that marker can
        # be emitted. `wants_epilogue` is the one place the rule lives.
        script = POWERSHELL.script(
            cmds,
            cwd=cwd,
            env=env,
            epilogue=wants_epilogue(self.capabilities),
        )
        return self.executor(
            script,
            bufsize=bufsize,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            capture_output=capture_output,
            check=check,
            encoding=encoding,
            errors=errors,
            input=input,
            timeout=timeout,
            text=text,
        )
