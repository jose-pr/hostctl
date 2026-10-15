"""Windows Remote Management host implementation."""

from __future__ import annotations

import base64
import dataclasses
import errno
import io
import json
import ntpath
import logging
import os
import stat as _stat
import subprocess
import threading
import typing
import uuid
from urllib.parse import quote, unquote, urlencode

from pathapi import FileStat

from ..executor import (
    ExecutorCapability,
    NativeWinRMSession,
    PsrpExecutor,
    WinRMExecutor,
    WinRMSession,
    pypsrp_available,
)
from ..executor.winrm import _b64, _PywinrmStdinSession
from ..process import RunspaceSession
from ..provider import (
    ExecutorProvider,
    OperationNotStarted,
    PathProvider,
    ProviderProbe,
    ProviderSelector,
)
from ._common import (
    _query_bool,
    _refuse_uri,
    _query_int,
    CaptureOutput,
    Command,
    Environment,
    FileHandle,
    HostConfig,
    HostInfo,
    Input,
    PathLike,
    parse_host_info,
    starts_direct_command,
    strict_uri_query,
    uri_host,
    uri_hostname,
)
from ..shell import POWERSHELL, ShellFlavour
from ._backend_path import WindowsBackendPath

log = logging.getLogger("hostctl.host.winrm")

WinRMTransport = typing.Literal[
    "basic",
    "credssp",
    "kerberos",
    "ntlm",
    "plaintext",
    "ssl",
]
CertificateValidation = typing.Literal["validate", "ignore"]
MessageEncryption = typing.Literal["auto", "always", "never"]
WinRMProviderName = typing.Literal["auto", "native", "pywinrm", "psrp"]


#: Script bytes one remote call may carry. Every leg delivers the script on
#: stdin or as a message, so no command line bounds it.
_SCRIPT_BUDGET = 256_000


@dataclasses.dataclass
class WinRMConfig(HostConfig, schemes=("winrm", "winrms")):
    """Connection and transport settings for the Windows provider."""

    #: Declared rather than enforced inside `_from_parsed_uri`, so the
    #: whitelist can be read before a config is built: an ambient
    #: credential (the CLI's `HOSTCTL_PASSWORD`) is offered only where it
    #: is accepted. The base dispatcher enforces it.
    uri_credentials = ("password",)

    host: str
    username: str
    password: typing.Optional[str] = dataclasses.field(default=None, repr=False)
    transport: WinRMTransport = "ntlm"
    port: typing.Optional[int] = None
    ssl: bool = False
    server_cert_validation: CertificateValidation = "validate"
    message_encryption: MessageEncryption = "auto"
    operation_timeout_sec: int = 20
    read_timeout_sec: int = 30
    provider: WinRMProviderName = "auto"
    #: Opt in to Basic credentials over plain HTTP; see `__post_init__`.
    allow_cleartext: bool = False

    def __post_init__(self) -> None:
        HostConfig.__init__(self)
        if not self.host:
            raise ValueError("host must not be empty")
        _refuse_uri("WinRMConfig", self.host)
        if not self.username:
            raise ValueError("username must not be empty")
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.transport not in typing.get_args(WinRMTransport):
            raise ValueError(f"unsupported WinRM transport: {self.transport}")
        if self.server_cert_validation not in typing.get_args(CertificateValidation):
            raise ValueError("server_cert_validation must be 'validate' or 'ignore'")
        if self.message_encryption not in typing.get_args(MessageEncryption):
            raise ValueError("message_encryption must be 'auto', 'always', or 'never'")
        if self.operation_timeout_sec <= 0:
            raise ValueError("operation_timeout_sec must be positive")
        if self.read_timeout_sec <= self.operation_timeout_sec:
            raise ValueError(
                "read_timeout_sec must be greater than operation_timeout_sec"
            )
        if self.provider not in typing.get_args(WinRMProviderName):
            raise ValueError("provider must be 'auto', 'native', 'pywinrm', or 'psrp'")
        if self.provider == "native" and self.password is not None:
            raise ValueError(
                "native WinRM uses the current Windows logon; drop password= "
                "or choose 'pywinrm'/'psrp'"
            )
        if (
            self.provider in ("pywinrm", "psrp")
            and self.password is None
            and self.transport != "kerberos"
        ):
            raise ValueError(
                f"provider={self.provider!r} needs password= (or "
                "transport='kerberos'); use provider='native' for the "
                "current Windows logon"
            )
        if (
            self.transport in ("basic", "plaintext", "ssl")
            and not self.ssl
            and not self.allow_cleartext
        ):
            raise ValueError(
                f"transport={self.transport!r} over http sends the password in "
                "cleartext; use ssl=True (winrms://) or allow_cleartext=True"
            )

    @property
    def endpoint(self) -> str:
        scheme = "https" if self.ssl else "http"
        from netimps import get_default_port, join_host

        port = self.port or get_default_port(self.scheme)
        return f"{scheme}://{join_host(self.host, port)}/wsman"

    @property
    def scheme(self) -> str:
        return "winrms" if self.ssl else "winrm"

    @property
    def connection_uri(self) -> str:
        query = urlencode(
            {
                "transport": self.transport,
                "server_cert_validation": self.server_cert_validation,
                "message_encryption": self.message_encryption,
                "operation_timeout_sec": self.operation_timeout_sec,
                "read_timeout_sec": self.read_timeout_sec,
            }
        )
        if self.provider != "auto":
            query += "&" + urlencode({"provider": self.provider})
        if self.allow_cleartext:
            query += "&allow_cleartext=true"
        authority = uri_host(self.host)
        if self.port is not None:
            from netimps import join_host

            authority = join_host(self.host, self.port)
        return f"{self.scheme}://{quote(self.username, safe='')}@{authority}?{query}"

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials: object) -> WinRMConfig:
        query = strict_uri_query(
            parsed,
            {
                "transport",
                "server_cert_validation",
                "message_encryption",
                "operation_timeout_sec",
                "read_timeout_sec",
                "provider",
                "allow_cleartext",
            },
        )
        if not parsed.hostname or parsed.path not in ("", "/"):
            raise ValueError("WinRM URI requires a host and no path")
        if not parsed.username:
            raise ValueError("WinRM URI requires a username")
        return cls(
            host=uri_hostname(parsed),
            username=unquote(parsed.username),
            password=typing.cast(typing.Optional[str], credentials.get("password")),
            port=parsed.port,
            ssl=parsed.scheme.casefold() == "winrms",
            transport=typing.cast(WinRMTransport, query.get("transport", "ntlm")),
            server_cert_validation=typing.cast(
                CertificateValidation,
                query.get("server_cert_validation", "validate"),
            ),
            message_encryption=typing.cast(
                MessageEncryption,
                query.get("message_encryption", "auto"),
            ),
            operation_timeout_sec=_query_int(query, "operation_timeout_sec", 20),
            read_timeout_sec=_query_int(query, "read_timeout_sec", 30),
            provider=typing.cast(WinRMProviderName, query.get("provider", "auto")),
            allow_cleartext=_query_bool(query, "allow_cleartext", False),
        )

    def _create_host(self):
        from .system import WindowsHost

        transport = _WinRMTransport(self)
        return WindowsHost(
            self,
            executor_providers=(WinRMExecutorProvider(transport),),
            path_providers=(WinRMPathProvider(transport),),
        )


class _Leg:
    """What one way of reaching the host owns: its lazy session and executor.

    The executor is built once, with the status and deadline behaviour the
    leg's session has, so the transport has no second source of truth for them.
    """

    name: str
    executor: typing.Union[WinRMExecutor, PsrpExecutor]

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
            lambda: float(self.config.read_timeout_sec),
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
        if self.config.transport not in {"ntlm", "kerberos"}:
            raise NotImplementedError(
                "native current-context WinRM supports ntlm/kerberos only"
            )
        return NativeWinRMSession(
            self.config.host,
            ssl=self.config.ssl,
            port=self.config.port,
            # The config's read timeout IS the deadline here: the local
            # powershell.exe relays the call, so its own wall clock is the
            # only bound available.
            timeout=float(self.config.read_timeout_sec),
            transport=self.config.transport,
            server_cert_validation=self.config.server_cert_validation,
            message_encryption=self.config.message_encryption,
        )


class _PywinrmLeg(_SessionLeg):
    """pywinrm's protocol, with the script on stdin."""

    name = "pywinrm"

    def __init__(self, config: WinRMConfig) -> None:
        super().__init__(config)
        self.executor = WinRMExecutor(
            lambda: self.session, lambda: float(self.config.read_timeout_sec)
        )

    def _build(self) -> WinRMSession:
        try:
            import winrm
        except ImportError as exc:
            raise ImportError(
                "WinRM support requires the 'winrm' extra: "
                "pip install hostctl[winrm]"
            ) from exc
        # `winrm.Session` still owns endpoint and auth handling; only its
        # protocol is used, because `run_ps` goes through cmd.exe.
        return _PywinrmStdinSession(
            winrm.Session(
                self.config.endpoint,
                auth=(self.config.username, self.config.password),
                transport=self.config.transport,
                server_cert_validation=self.config.server_cert_validation,
                message_encryption=self.config.message_encryption,
                operation_timeout_sec=self.config.operation_timeout_sec,
                read_timeout_sec=self.config.read_timeout_sec,
            ).protocol
        )


class _PsrpLeg(_Leg):
    """A persistent PSRP runspace; the executor opens a pipeline in it."""

    name = "psrp"

    def __init__(self, config: WinRMConfig) -> None:
        super().__init__(config)
        self._runspace: typing.Optional[RunspaceSession] = None
        self.executor = PsrpExecutor(lambda: self.runspace())

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
    "psrp": _PsrpLeg,
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
        if config.transport != "kerberos":
            raise ValueError(
                "current-context WinRM requires a Windows client; pass password="
            )
    return "psrp" if pypsrp_available() else "pywinrm"


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
    def executor(self) -> typing.Union[WinRMExecutor, PsrpExecutor]:
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
        if not isinstance(self._leg, _PsrpLeg):
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
        self, *segments: PathLike, backend: typing.Optional[str] = None
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
        stdin: typing.Optional[FileHandle] = None,
        stdout: typing.Optional[FileHandle] = None,
        stderr: typing.Optional[FileHandle] = None,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
        capture_output: CaptureOutput = True,
        check: bool = True,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
        input: Input = None,
        timeout: typing.Optional[float] = None,
        text: typing.Optional[bool] = None,
    ) -> subprocess.CompletedProcess:
        if executable is not None:
            raise NotImplementedError(
                "WinRM provider run supports PowerShell only and does not accept executable"
            )
        direct = starts_direct_command(cmds)
        if direct is not None:
            command, args = direct
            cmds = ((command, *args),)
        # `for_session` when the session reports its own status: the native
        # wrapper carries the remote code back on its marker line, and the
        # flavour's own `exit` epilogue ends the remote pipeline BEFORE that
        # marker can be emitted. `SystemHost` applies this rule from the
        # provider's `manages_status` capability; this path composes its own
        # script and has to apply the same rule rather than a different one.
        script = POWERSHELL.script(
            cmds,
            cwd=cwd,
            env=env,
            for_session=ExecutorCapability.MANAGES_STATUS.value in self.capabilities,
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


def _append_operation(encoded: str) -> str:
    """One chunk appended to the temporary file; also sizes the chunks."""
    return (
        "$b=[Convert]::FromBase64String('"
        + encoded
        + "');$s=[IO.File]::Open($p,[IO.FileMode]::Append,"
        "[IO.FileAccess]::Write,[IO.FileShare]::None);"
        "try{$s.Write($b,0,$b.Length)}finally{$s.Dispose()}"
    )


class WinRMPathBackend:
    """Structured PowerShell filesystem operations for :class:`WinRMPath`."""

    # `max_script_bytes` is the transport's per-call script budget; a write
    # batches as many chunks as fit in one script.
    chunk_size = 48 * 1024

    def __init__(
        self,
        run: typing.Callable[..., subprocess.CompletedProcess],
        *,
        max_script_bytes: int,
    ) -> None:
        self._run = run
        if max_script_bytes < 2_048:
            raise ValueError("max_script_bytes must be at least 2048")
        self.max_script_bytes = max_script_bytes

    def _script(
        self, path: str, body: str, *, target: typing.Optional[str] = None
    ) -> str:
        values = [
            # A headless native Invoke-Command runspace can have no valid
            # console handle. Keep the WinRS UTF-8 fix, but do not let
            # Console.OutputEncoding abort otherwise-valid filesystem work.
            "try{[Console]::OutputEncoding="
            "[Text.UTF8Encoding]::new($false)}catch{};"
            "$OutputEncoding=[Text.UTF8Encoding]::new($false)",
            "$ErrorActionPreference='Stop'",
            "$p=[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{_b64(path)}'))",
        ]
        if target is not None:
            values.append(
                "$t=[Text.Encoding]::UTF8.GetString("
                f"[Convert]::FromBase64String('{_b64(target)}'))"
            )
        values.append(
            "try {" + body + "} catch {"
            "$e=$_.Exception;"
            # PowerShell wraps a .NET static-method failure in a
            # MethodInvocationException whose category is NotSpecified, so
            # every test below used to miss and every error from an
            # `[IO.File]`/`[IO.Directory]` body degraded to bare OSError. The
            # worst of it: read_bytes() on a missing file raised OSError, which
            # defeats staged_open's `except FileNotFoundError`, so open("ab")
            # on a missing file raised instead of creating it.
            "while($e.InnerException -and ($e -is "
            "[System.Management.Automation.MethodInvocationException]))"
            "{$e=$e.InnerException};"
            "$c=[string]$_.CategoryInfo.Category;"
            "$k=if([IO.Directory]::Exists($p) -and "
            "($e -is [UnauthorizedAccessException])){'isdir'}"
            "elseif($c -eq 'ObjectNotFound'){'missing'}"
            "elseif($c -eq 'PermissionDenied' -or "
            "$e -is [UnauthorizedAccessException]){'permission'}"
            "elseif($c -eq 'ResourceExists'){'exists'}"
            "elseif($e -is [IO.FileNotFoundException] -or "
            "$e -is [IO.DirectoryNotFoundException] -or "
            "$_ -is [System.Management.Automation.ItemNotFoundException]){'missing'}"
            "elseif($e -is [IO.IOException] -and "
            "$e.HResult -eq -2147024816){'exists'}"
            # ERROR_ALREADY_EXISTS: a move onto an existing entry.
            "elseif($e -is [IO.IOException] -and "
            "$e.HResult -eq -2147024713){'exists'}"
            # ERROR_DIR_NOT_EMPTY: a delete of a non-empty directory.
            "elseif($e -is [IO.IOException] -and "
            "$e.HResult -eq -2147024751){'notempty'}"
            "else{'oserror'};"
            "$m=[Convert]::ToBase64String("
            "[Text.Encoding]::UTF8.GetBytes($e.Message));"
            "Write-Output ('HOSTCTL_ERROR:'+$k+':'+$m)}"
        )
        return ";".join(values)

    def _execute(
        self, path: str, body: str, *, target: typing.Optional[str] = None
    ) -> str:
        result = self._run(
            self._script(path, body, target=target),
            check=False,
            encoding="utf-8",
        )
        output = result.stdout or ""
        marker = next(
            (line for line in output.splitlines() if line.startswith("HOSTCTL_ERROR:")),
            None,
        )
        if result.returncode or marker is not None:
            if marker is not None:
                _, kind, encoded = marker.split(":", 2)
                detail = base64.b64decode(encoded).decode("utf-8", "replace")
            else:
                message = result.stderr or ""
                if isinstance(message, bytes):
                    message = message.decode("utf-8", "replace")
                detail = message
                kind = "oserror"
            error_type, code = {
                "missing": (FileNotFoundError, errno.ENOENT),
                "permission": (PermissionError, errno.EACCES),
                "exists": (FileExistsError, errno.EEXIST),
                "isdir": (IsADirectoryError, errno.EISDIR),
                "notdir": (NotADirectoryError, errno.ENOTDIR),
                "notempty": (OSError, errno.ENOTEMPTY),
            }.get(kind, (OSError, errno.EIO))
            # The full tuple: an `OSError` with no `errno` and no `filename`
            # cannot be handled generically (`exc.errno == errno.ENOENT`) and
            # cannot name the path in a message -- a caller had only the
            # class and whatever text the remote happened to send.
            raise error_type(code, detail or "remote error", path)
        return output

    @staticmethod
    def _metadata_script(expression: str) -> str:
        return (
            f"$i={expression};"
            "[pscustomobject]@{"
            "name=$i.Name;directory=[bool]$i.PSIsContainer;"
            "size=$(if($i.PSIsContainer){0}else{[long]$i.Length});"
            "mtime=([DateTimeOffset]$i.LastWriteTimeUtc).ToUnixTimeSeconds();"
            "readonly=[bool]($i.Attributes -band [IO.FileAttributes]::ReadOnly);"
            "link=[bool]($i.Attributes -band [IO.FileAttributes]::ReparsePoint);"
            "target=[string]$i.Target"
            "}"
        )

    @staticmethod
    def _stat_value(value: typing.Mapping[str, object]) -> FileStat:
        mode = _stat.S_IFDIR if value["directory"] else _stat.S_IFREG
        if value.get("link"):
            mode = _stat.S_IFLNK
        permissions = 0o444 if value.get("readonly") else 0o666
        if value["directory"]:
            permissions |= 0o111
        return FileStat(
            st_mode=mode | permissions,
            st_size=typing.cast(int, value["size"]),
            st_mtime=typing.cast(int, value["mtime"]),
        )

    def stat(
        self, path: str, *, follow_symlinks: bool = True, _hops: int = 8
    ) -> FileStat:
        body = (
            self._metadata_script("Get-Item -LiteralPath $p -Force")
            + "|ConvertTo-Json -Compress"
        )
        value = json.loads(self._execute(path, body))
        result = self._stat_value(value)
        if follow_symlinks and _stat.S_ISLNK(result.st_mode):
            target = typing.cast(str, value.get("target") or "")
            if not target:
                raise OSError(errno.EINVAL, "cannot resolve reparse point", path)
            # A link's target is stored as written, and Windows resolves a
            # relative one against the LINK's directory. Re-stating it as an
            # absolute path looked it up from the drive root instead, so
            # `C:\app\current -> releases\v3` stat'd `releases\v3`.
            if not ntpath.isabs(target) and not target.startswith("\\\\"):
                target = ntpath.join(ntpath.dirname(path), target)
            if _hops <= 0:
                raise OSError(errno.ELOOP, "too many levels of symbolic links", path)
            try:
                return self.stat(target, follow_symlinks=True, _hops=_hops - 1)
            except OSError as exc:
                if getattr(exc, "errno", None) == errno.ELOOP:
                    raise
                # Keep the cause's errno, so a dangling link is a
                # FileNotFoundError, as local `stat` makes it.
                raise OSError(
                    exc.errno or errno.EIO, "cannot resolve reparse point", path
                ) from exc
        return result

    def scandir(self, path: str) -> typing.List[typing.Tuple[str, FileStat]]:
        # Follow the link, as `os.scandir` and `pathlib.Path.iterdir` do. The
        # guard used to stat without following, and every reparse point is
        # reported as S_IFLNK whatever it points at -- so listing a directory
        # symlink or a junction (a redirected profile folder, a DFS-style
        # mount point, "C:\Users\All Users") raised NotADirectoryError while
        # `is_dir()` on the same path returned True.
        current = self.stat(path, follow_symlinks=True)
        if not _stat.S_ISDIR(current.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "Not a directory", path)
        metadata = self._metadata_script("$_")
        body = (
            f"@((Get-ChildItem -LiteralPath $p -Force)|ForEach-Object{{{metadata}}})"
            "|ConvertTo-Json -Compress"
        )
        values = json.loads(self._execute(path, body) or "[]")
        if isinstance(values, dict):
            values = [values]
        return [(value["name"], self._stat_value(value)) for value in values]

    def read_bytes(self, path: str, offset: int = 0) -> bytes:
        """Read from `offset` to the end of the file in one script."""
        if offset < 0:
            raise ValueError("offset must be non-negative")
        seek = f"$s.Seek({offset},[IO.SeekOrigin]::Begin)|Out-Null;" if offset else ""
        body = (
            "$s=[IO.File]::OpenRead($p);try{" + seek + "$b=New-Object byte[] "
            f"{self.chunk_size};while(($n=$s.Read($b,0,$b.Length))-gt 0){{"
            "[Convert]::ToBase64String($b,0,$n)}}finally{$s.Dispose()}"
        )
        return b"".join(
            base64.b64decode(line)
            for line in self._execute(path, body).splitlines()
            if line
        )

    def read_range(self, path: str, offset: int, count: int) -> bytes:
        """Read one bounded byte range through PowerShell."""
        if offset < 0 or count < 0:
            raise ValueError("offset and count must be non-negative")
        body = (
            "$s=[IO.File]::OpenRead($p);try{$s.Seek(%d,[IO.SeekOrigin]::Begin)|Out-Null;"
            "$b=New-Object byte[] %d;$n=$s.Read($b,0,$b.Length);"
            "[Convert]::ToBase64String($b,0,$n)}finally{$s.Dispose()}" % (offset, count)
        )
        output = self._execute(path, body)
        return b"".join(base64.b64decode(line) for line in output.splitlines() if line)

    def open_read(self, path: str) -> io.BufferedReader:
        return io.BufferedReader(_WinRMReadStream(self, path), buffer_size=_READ_BUFFER)

    def exists(self, path: str, *, follow_symlinks: bool = True) -> bool:
        """Whether the path exists, from metadata alone."""
        try:
            self.stat(path, follow_symlinks=follow_symlinks)
        except FileNotFoundError:
            return False
        return True

    def write_bytes(self, path: str, value: bytes, *, exclusive: bool = False) -> None:
        temporary = path + ".hostctl-" + uuid.uuid4().hex
        try:
            self._execute(
                temporary,
                "$s=[IO.File]::Open($p,[IO.FileMode]::CreateNew,"
                "[IO.FileAccess]::Write,[IO.FileShare]::None);$s.Dispose()",
            )
            # One script per batch of chunks: a round trip per chunk would
            # cost far more than the base64 does.
            batches: typing.List[typing.List[str]] = []
            batch: typing.List[str] = []
            batch_size = 0
            # MEASURED, not guessed: the fixed cost is this path's own
            # prelude, which carries the base64 of the path itself.
            overhead = len(self._script(temporary, "", target=path).encode("utf-8"))
            room = self.max_script_bytes - overhead - len(_append_operation(""))
            payload_size = min(self.chunk_size, (room // 4) * 3)
            if payload_size < 3:
                raise OSError(
                    errno.ENAMETOOLONG,
                    f"the script budget ({self.max_script_bytes} bytes) cannot "
                    f"hold this path's prelude ({overhead} bytes) and one chunk",
                    path,
                )
            script_budget = self.max_script_bytes - overhead
            for offset in range(0, len(value), payload_size):
                encoded = base64.b64encode(
                    value[offset : offset + payload_size]
                ).decode("ascii")
                operation = _append_operation(encoded)
                if batch and batch_size + len(operation) + 1 > script_budget:
                    batches.append(batch)
                    batch = []
                    batch_size = 0
                batch.append(operation)
                batch_size += len(operation) + 1
            if batch:
                batches.append(batch)
            for operations in batches:
                self._execute(temporary, ";".join(operations))
            self._execute(
                temporary,
                (
                    "if([IO.File]::Exists($t)){"
                    "throw [IO.IOException]::new('Path exists',-2147024816)};"
                    "[IO.File]::Move($p,$t)"
                    if exclusive
                    # `[NullString]::Value`, not `$null`: PowerShell's .NET
                    # binder turns `$null` into "" for a `string` parameter,
                    # so `Replace` received an empty backup name and threw on
                    # every PowerShell version -- overwriting an existing file
                    # through a WinRM path could never succeed, and the old
                    # contents stayed put.
                    else "if([IO.File]::Exists($t)){"
                    "[IO.File]::Replace($p,$t,[NullString]::Value)}"
                    "else{[IO.File]::Move($p,$t)}"
                ),
                target=path,
            )
        except Exception:
            self.unlink(temporary, missing_ok=True)
            raise

    def mkdir(self, path: str) -> None:
        self._execute(
            path,
            "if([IO.Directory]::Exists($p) -or [IO.File]::Exists($p)){"
            "throw [IO.IOException]::new('Path exists',-2147024816)};"
            "$parent=[IO.Path]::GetDirectoryName($p);"
            "if($parent -and -not [IO.Directory]::Exists($parent)){"
            "throw [IO.DirectoryNotFoundException]::new($parent)};"
            "[IO.Directory]::CreateDirectory($p)|Out-Null",
        )

    def unlink(self, path: str, *, missing_ok: bool = False) -> None:
        body = (
            "$i=Get-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue;"
            "if($null -eq $i){"
            + ("" if missing_ok else "throw [IO.FileNotFoundException]::new($p)")
            + "}elseif($i.Attributes -band [IO.FileAttributes]::ReparsePoint){"
            # NOT Remove-Item: on Windows PowerShell 5.1 -- the default remote
            # shell on every Windows Server -- a DIRECTORY reparse point whose
            # target has entries is treated as a directory with children, so
            # Remove-Item asks for confirmation. Every WinRM hop runs
            # powershell.exe with -NonInteractive, where the prompt cannot be
            # answered: it raised and left the junction in place, and only
            # when the target was non-empty, so the failure looked
            # intermittent. These .NET calls delete the link itself and
            # never recurse into what it points at.
            "if($i.PSIsContainer){[IO.Directory]::Delete($p,$false)}"
            "else{[IO.File]::Delete($p)}"
            "}elseif($i.PSIsContainer){"
            "$m=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($p));"
            "Write-Output ('HOSTCTL_ERROR:isdir:'+$m)}else{[IO.File]::Delete($p)}"
        )
        self._execute(path, body)

    def rmdir(self, path: str) -> None:
        self._execute(
            path,
            "$i=Get-Item -LiteralPath $p -Force;"
            "if(-not $i.PSIsContainer){"
            "$m=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($p));"
            "Write-Output ('HOSTCTL_ERROR:notdir:'+$m)}else{"
            "[IO.Directory]::Delete($p,$false)}",
        )

    def rename(self, path: str, target: str) -> None:
        self._execute(
            path,
            "if([IO.Directory]::Exists($p)){[IO.Directory]::Move($p,$t)}"
            "else{[IO.File]::Move($p,$t)}",
            target=target,
        )

    def symlink(self, path: str, target: str) -> None:
        """Create ``path`` as a symbolic link to ``target``.

        Creating a symbolic link on Windows requires either an elevated
        session or Developer Mode; without one, ``New-Item`` fails with a
        privilege error which the shared error mapping surfaces as
        :class:`PermissionError`.  ``-ItemType SymbolicLink`` is used for
        both file and directory targets: Windows records the distinction
        from the target itself when it exists, and a dangling link is
        created as a file link (matching ``os.symlink`` defaults).
        """
        self._execute(
            path,
            "if([IO.Directory]::Exists($p) -or [IO.File]::Exists($p)){"
            "throw [IO.IOException]::new('Path exists',-2147024816)};"
            # A privilege failure raised by New-Item is a SecurityException or
            # a Win32 ERROR_PRIVILEGE_NOT_HELD (1314) IOException; normalize
            # both to the 'permission' marker the caller maps to
            # PermissionError, rather than letting them fall through to a
            # bare OSError.
            "try{New-Item -ItemType SymbolicLink -Path $p -Target $t "
            "-Force:$false|Out-Null}catch{"
            "$x=$_.Exception;"
            "if($x -is [Security.SecurityException] -or "
            "$x.HResult -eq -2147024891 -or $x.HResult -eq -2147023582 -or "
            "$x.Message -match 'privilege'){"
            "throw [UnauthorizedAccessException]::new($x.Message)};throw}",
            target=target,
        )

    def readlink(self, path: str) -> str:
        body = (
            "$i=Get-Item -LiteralPath $p -Force;"
            "if(-not ($i.Attributes -band [IO.FileAttributes]::ReparsePoint)){"
            "$m=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("
            "'not a symbolic link: '+$p));"
            "Write-Output ('HOSTCTL_ERROR:oserror:'+$m)}else{"
            "Write-Output ([Convert]::ToBase64String("
            "[Text.Encoding]::UTF8.GetBytes([string]$i.Target)))}"
        )
        output = self._execute(path, body).strip()
        if not output:
            raise OSError(errno.EINVAL, "cannot resolve reparse point", path)
        return base64.b64decode(output).decode("utf-8", "replace")

    def chmod(self, path: str, mode: int) -> None:
        readonly = "$false" if mode & 0o200 else "$true"
        self._execute(
            path,
            "$i=Get-Item -LiteralPath $p -Force;"
            f"$i.Attributes=if({readonly}){{[IO.FileAttributes]($i.Attributes -bor "
            "[IO.FileAttributes]::ReadOnly)}else{[IO.FileAttributes]($i.Attributes "
            "-band (-bnot [IO.FileAttributes]::ReadOnly))};"
            "Set-ItemProperty -LiteralPath $p -Name Attributes -Value $i.Attributes",
        )


#: One range script per MiB for a partial read.
_READ_BUFFER = 1 << 20


class _WinRMReadStream(io.RawIOBase):
    """Lazy bounded reader backed by independent WinRM range requests."""

    def __init__(self, backend: WinRMPathBackend, path: str) -> None:
        self._backend = backend
        self._path = path
        self._offset = 0
        self._at_end = False

    def readable(self) -> bool:
        return True

    def readall(self) -> bytes:
        """The rest of the file in one script, which `read()` calls."""
        if self._at_end:
            return b""
        data = self._backend.read_bytes(self._path, self._offset)
        self._offset += len(data)
        self._at_end = True
        return data

    def readinto(self, target: bytearray) -> int:
        if not target or self._at_end:
            return 0
        data = self._backend.read_range(self._path, self._offset, len(target))
        if not data:
            return 0
        target[: len(data)] = data
        self._offset += len(data)
        return len(data)


class WinRMPath(WindowsBackendPath):
    """A Windows path whose I/O executes through a WinRM host."""

    __slots__ = ()
    _backend_type = WinRMPathBackend

    def _symlink_to(self, target, target_is_directory: bool = False):
        """Create this path as a symbolic link to ``target``.

        The PRIMITIVE, not the public method: `pathapi`'s wrapper adds
        `force=` (remove an existing entry first) and normalises a `str`
        target without re-parsing it as URI syntax.

        Windows only permits this from an elevated session or with
        Developer Mode enabled; otherwise the backend raises
        :class:`PermissionError`.  ``target_is_directory`` is accepted for
        :meth:`pathlib.Path.symlink_to` signature parity and ignored --
        ``New-Item -ItemType SymbolicLink`` infers the kind from the target.
        """
        self.backend.symlink(str(self), str(target))

    def readlink(self):
        return self.with_segments(self.backend.readlink(str(self)))

    def _mkdir(self, mode: int):
        self.backend.mkdir(str(self))

    def _chmod_on_backend(self, mode: int, follow_symlinks: bool) -> None:
        if not follow_symlinks:
            raise NotImplementedError("WinRMPath chmod does not support links")
        self.backend.chmod(str(self), mode)


class WinRMExecutorProvider(ExecutorProvider):
    """Lifecycle-owning WinRM/PSRP executor provider."""

    def __init__(self, transport: _WinRMTransport):
        self.transport = transport
        # The executor already declares `manages_status` when its session
        # reports the status itself, which is what keeps the flavour's `exit`
        # epilogue off the script.
        capabilities = set(transport.executor.executor_capabilities)
        if "runspace" in transport.capabilities:
            capabilities.add("runspace")
        super().__init__("winrm", transport.executor, capabilities=capabilities)

    def probe(self):
        return ProviderProbe("available", capabilities=self.capabilities)

    def connect(self):
        try:
            self.transport.connect()
        # OSError, not just ConnectionError: a DNS failure (socket.gaierror)
        # and a routing failure (EHOSTUNREACH/ENETUNREACH) are OSError but not
        # ConnectionError, and they are unambiguously pre-dispatch -- no
        # channel opened, nothing sent. They used to escape the provider, so
        # ordered fallback stopped dead and a raw socket error crossed the
        # public boundary.
        except (OSError, TimeoutError) as exc:
            log.debug(
                "WinRM provider declining before dispatch: %s: %s",
                type(exc).__name__,
                ProviderSelector.redact(exc),
            )
            raise OperationNotStarted(
                "WinRM connection failed before dispatch", cause=exc
            ) from exc

    def close(self):
        self.transport.close()

    def info(self):
        return self.transport.info()

    # No `spawn`: WinRM has no persistent byte-stream session (see
    # docs/guide/contracts.md). Defining a pass-through here would satisfy
    # SystemHost.spawn's `getattr(provider, "spawn", None)` guard and then
    # fail one frame deeper with AttributeError; omitting it lets that guard
    # raise the documented NotImplementedError instead.

    def runspace(self):
        if "runspace" not in self.capabilities:
            raise NotImplementedError("WinRM provider does not support PSRP runspaces")
        return self.transport.runspace()


class WinRMPathProvider(PathProvider):
    """Windows-semantic WinRM path provider sharing the executor transport."""

    def __init__(self, transport: _WinRMTransport):
        self.transport = transport
        super().__init__(
            "winrm", lambda *segments: transport.path(*segments), capabilities=("path",)
        )


def winrm_providers(
    config: WinRMConfig,
) -> typing.Tuple[WinRMExecutorProvider, WinRMPathProvider]:
    """Build an executor and path provider sharing one WinRM transport.

    The SSH counterpart, `hostctl.host.ssh_providers`, documents why this
    returns a pair rather than exposing the transport: both providers must
    share one, and building them separately silently opens two.
    """
    transport = _WinRMTransport(config)
    return WinRMExecutorProvider(transport), WinRMPathProvider(transport)
