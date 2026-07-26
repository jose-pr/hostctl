"""Windows Remote Management host implementation."""

from __future__ import annotations

import base64
import dataclasses
import io
import json
import logging
import os
import stat as _stat
import subprocess
import typing
import uuid
import warnings
from pathlib import PurePath as _StdPurePath
from urllib.parse import quote, unquote, urlencode

from pathlib_next import Path, WindowsPathname
from pathlib_next.utils.stat import FileStat

from ..executor import (
    NativeWinRMSession,
    PsrpExecutor,
    WinRMExecutor,
    WinRMSession,
    pypsrp_available,
    require_pypsrp,
)
from ..process import RunspaceSession
from ..provider import (
    ExecutorProvider,
    OperationNotStarted,
    PathProvider,
    ProviderProbe,
    ProviderSelector,
)
from ._common import (
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
    strict_uri_credentials,
    strict_uri_query,
    uri_host,
)
from ..shell import POWERSHELL, ShellFlavour

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
WinRMProviderName = typing.Literal["auto", "pywinrm", "psrp"]


@dataclasses.dataclass
class WinRMConfig(HostConfig, schemes=("winrm", "winrms")):
    """Connection and transport settings for the Windows provider."""

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

    def __post_init__(self) -> None:
        HostConfig.__init__(self)
        if not self.host:
            raise ValueError("host must not be empty")
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
            raise ValueError("provider must be 'auto', 'pywinrm', or 'psrp'")

    @property
    def endpoint(self) -> str:
        scheme = "https" if self.ssl else "http"
        port = self.port or (5986 if self.ssl else 5985)
        return f"{scheme}://{uri_host(self.host)}:{port}/wsman"

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
        port = self.port or (5986 if self.ssl else 5985)
        return (
            f"{self.scheme}://{quote(self.username, safe='')}@"
            f"{uri_host(self.host)}:{port}?{query}"
        )

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials: object) -> WinRMConfig:
        strict_uri_credentials(credentials, ("password",))
        query = strict_uri_query(
            parsed,
            {
                "transport",
                "server_cert_validation",
                "message_encryption",
                "operation_timeout_sec",
                "read_timeout_sec",
                "provider",
            },
        )
        if not parsed.hostname or parsed.path not in ("", "/"):
            raise ValueError("WinRM URI requires a host and no path")
        if not parsed.username:
            raise ValueError("WinRM URI requires a username")
        return cls(
            host=parsed.hostname,
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
        )

    def _create_host(self):
        from .system import WindowsHost

        transport = _WinRMTransport(self)
        return WindowsHost(
            self,
            executor_providers=(WinRMExecutorProvider(transport),),
            path_providers=(WinRMPathProvider(transport),),
        )


class _WinRMTransport:
    """A Windows host reached through PowerShell over WinRM."""

    def __init__(self, config: WinRMConfig) -> None:
        self.config = config
        self._session: typing.Optional[WinRMSession] = None
        self._runspace: typing.Optional[RunspaceSession] = None
        self._provider = (
            "psrp"
            if self.config.provider == "psrp"
            or (self.config.provider == "auto" and pypsrp_available())
            else "pywinrm"
        )
        self._executor = WinRMExecutor(
            lambda: self.session,
            lambda: float(self.config.read_timeout_sec),
        )
        # WinRS/pywinrm carries the generated script on a command line and
        # therefore uses a conservative budget.  Native PowerShell remoting
        # and PSRP feed scripts through stdin/messages and can safely batch
        # larger payloads.
        native_context = self.config.password is None and os.name == "nt"
        script_budget = 256_000 if self._provider == "psrp" or native_context else 6_000
        self._path_backend = WinRMPathBackend(
            self.run,
            max_script_bytes=script_budget,
        )

    @property
    def capabilities(self) -> typing.FrozenSet[str]:
        capabilities = {"run", "path"}
        if self._provider == "psrp":
            capabilities.add("runspace")
        return frozenset(capabilities)

    @property
    def shell_flavour(self) -> ShellFlavour:
        return POWERSHELL

    @property
    def executor(self) -> typing.Union[WinRMExecutor, PsrpExecutor]:
        if self._provider == "psrp":
            return PsrpExecutor(self.runspace)
        return self._executor

    def info(self) -> HostInfo:
        return parse_host_info(
            self.run(
                POWERSHELL.info_script,
                check=False,
                encoding="utf-8",
            ).stdout
        )

    @property
    def session(self) -> WinRMSession:
        if self._provider == "psrp":
            raise RuntimeError(
                "PSRP provider uses runspace(); pywinrm session unavailable"
            )
        if self._session is None:
            if self.config.password is None:
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
                self._session = NativeWinRMSession(
                    self.config.host,
                    ssl=self.config.ssl,
                    port=self.config.port,
                    timeout=None,
                    transport=self.config.transport,
                    server_cert_validation=self.config.server_cert_validation,
                    message_encryption=self.config.message_encryption,
                )
                return self._session
            try:
                import winrm
            except ImportError as exc:
                raise ImportError(
                    "WinRM support requires the 'winrm' extra: "
                    "pip install hostctl[winrm]"
                ) from exc
            self._session = winrm.Session(
                self.config.endpoint,
                auth=(self.config.username, self.config.password),
                transport=self.config.transport,
                server_cert_validation=self.config.server_cert_validation,
                message_encryption=self.config.message_encryption,
                operation_timeout_sec=self.config.operation_timeout_sec,
                read_timeout_sec=self.config.read_timeout_sec,
            )
        return self._session

    def runspace(self) -> RunspaceSession:
        """Return a persistent typed PSRP runspace session."""
        if self._provider != "psrp":
            if self.config.provider == "psrp":
                require_pypsrp()
            raise NotImplementedError(
                "PSRP runspaces are unavailable; install hostctl[psrp] on Python 3.10+"
            )
        if self._runspace is None:
            self._runspace = RunspaceSession(self.config)
        self._runspace.connect()
        return self._runspace

    def connect(self) -> None:
        log.debug(
            "opening WinRM %s session to %s",
            self._provider,
            ProviderSelector.redact(self.config.connection_uri),
        )
        if self._provider == "psrp":
            self.runspace()
        else:
            _ = self.session
        log.debug(
            "WinRM %s session to %s established",
            self._provider,
            ProviderSelector.redact(self.config.connection_uri),
        )

    def close(self) -> None:
        if self._session is not None or self._runspace is not None:
            log.debug(
                "closing WinRM %s session to %s",
                self._provider,
                ProviderSelector.redact(self.config.connection_uri),
            )
        if self._session is not None:
            session, self._session = self._session, None
            close = getattr(session, "close", None)
            if close is not None:
                close()
        if self._runspace is not None:
            self._runspace.close()
            self._runspace = None

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
        script = POWERSHELL.script(cmds, cwd=cwd, env=env)
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


def _encoded(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


class WinRMPathBackend:
    """Structured PowerShell filesystem operations for :class:`WinRMPath`."""

    # WinRS command lines are much smaller than a local PowerShell stdin
    # stream.  Keep each generated script below the conservative 6 KiB
    # budget used by pywinrm, while native adapters may opt into larger
    # batches through ``max_script_bytes``.
    chunk_size = 48 * 1024

    def __init__(
        self,
        run: typing.Callable[..., subprocess.CompletedProcess],
        *,
        max_script_bytes: int = 6_000,
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
            f"[Convert]::FromBase64String('{_encoded(path)}'))",
        ]
        if target is not None:
            values.append(
                "$t=[Text.Encoding]::UTF8.GetString("
                f"[Convert]::FromBase64String('{_encoded(target)}'))"
            )
        values.append(
            "try {" + body + "} catch {"
            "$e=$_.Exception;"
            "$c=[string]$_.CategoryInfo.Category;"
            "$k=if($c -eq 'ObjectNotFound'){'missing'}"
            "elseif($c -eq 'PermissionDenied' -or "
            "$e -is [UnauthorizedAccessException]){'permission'}"
            "elseif($c -eq 'ResourceExists'){'exists'}"
            "elseif($e -is [IO.FileNotFoundException] -or "
            "$e -is [IO.DirectoryNotFoundException] -or "
            "$_ -is [System.Management.Automation.ItemNotFoundException]){'missing'}"
            "elseif($e -is [IO.IOException] -and "
            "$e.HResult -eq -2147024816){'exists'}"
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
            error_type = {
                "missing": FileNotFoundError,
                "permission": PermissionError,
                "exists": FileExistsError,
                "isdir": IsADirectoryError,
                "notdir": NotADirectoryError,
            }.get(kind, OSError)
            raise error_type(detail or path)
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

    def stat(self, path: str, *, follow_symlinks: bool = True) -> FileStat:
        body = (
            self._metadata_script("Get-Item -LiteralPath $p -Force")
            + "|ConvertTo-Json -Compress"
        )
        value = json.loads(self._execute(path, body))
        result = self._stat_value(value)
        if follow_symlinks and _stat.S_ISLNK(result.st_mode):
            target = typing.cast(str, value.get("target") or "")
            if not target:
                raise OSError(f"cannot resolve reparse point: {path}")
            try:
                return self.stat(target, follow_symlinks=True)
            except OSError as exc:
                raise OSError(f"cannot resolve reparse point: {path}") from exc
        return result

    def scandir(self, path: str) -> typing.List[typing.Tuple[str, FileStat]]:
        current = self.stat(path, follow_symlinks=False)
        if not _stat.S_ISDIR(current.st_mode):
            raise NotADirectoryError(path)
        metadata = self._metadata_script("$_")
        body = (
            f"@((Get-ChildItem -LiteralPath $p -Force)|ForEach-Object{{{metadata}}})"
            "|ConvertTo-Json -Compress"
        )
        values = json.loads(self._execute(path, body) or "[]")
        if isinstance(values, dict):
            values = [values]
        return [(value["name"], self._stat_value(value)) for value in values]

    def read_bytes(self, path: str) -> bytes:
        body = (
            "$s=[IO.File]::OpenRead($p);try{$b=New-Object byte[] "
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
        return io.BufferedReader(_WinRMReadStream(self, path))

    def write_bytes(self, path: str, value: bytes, *, exclusive: bool = False) -> None:
        temporary = path + ".hostctl-" + uuid.uuid4().hex
        try:
            self._execute(
                temporary,
                "$s=[IO.File]::Open($p,[IO.FileMode]::CreateNew,"
                "[IO.FileAccess]::Write,[IO.FileShare]::None);$s.Dispose()",
            )
            # Build one script per transport-sized batch.  This avoids a
            # round-trip for every tiny chunk while never exceeding the
            # WinRS command-line budget.
            batches: typing.List[typing.List[str]] = []
            batch: typing.List[str] = []
            batch_size = 0
            payload_size = min(self.chunk_size, 1536)
            script_budget = max(1024, self.max_script_bytes - 1_200)
            for offset in range(0, len(value), payload_size):
                encoded = base64.b64encode(
                    value[offset : offset + payload_size]
                ).decode("ascii")
                operation = (
                    "$b=[Convert]::FromBase64String('"
                    + encoded
                    + "');$s=[IO.File]::Open($p,[IO.FileMode]::Append,"
                    "[IO.FileAccess]::Write,[IO.FileShare]::None);"
                    "try{$s.Write($b,0,$b.Length)}finally{$s.Dispose()}"
                )
                if batch and batch_size + len(operation) > script_budget:
                    batches.append(batch)
                    batch = []
                    batch_size = 0
                batch.append(operation)
                batch_size += len(operation)
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
                    else "if([IO.File]::Exists($t)){"
                    "[IO.File]::Replace($p,$t,$null)}else{[IO.File]::Move($p,$t)}"
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
            "Remove-Item -LiteralPath $p -Force"
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
            raise OSError(f"cannot resolve reparse point: {path}")
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


class _WriteBackBytesIO(io.BytesIO):
    def __init__(
        self,
        value: bytes,
        commit: typing.Optional[typing.Callable[[bytes], None]],
    ) -> None:
        super().__init__(value)
        self._commit = commit

    def close(self) -> None:
        if not self.closed and self._commit is not None:
            value = self.getvalue()
            commit, self._commit = self._commit, None
            try:
                commit(value)
            finally:
                super().close()
        else:
            super().close()

    def __del__(self):
        if getattr(self, "_commit", None) is not None and not self.closed:
            warnings.warn(
                "unclosed WinRM write stream discarded without committing",
                ResourceWarning,
                stacklevel=2,
            )
            self._commit = None
        try:
            super().close()
        except Exception:
            pass


class _WinRMReadStream(io.RawIOBase):
    """Lazy bounded reader backed by independent WinRM range requests."""

    def __init__(self, backend: WinRMPathBackend, path: str) -> None:
        self._backend = backend
        self._path = path
        self._offset = 0

    def readable(self) -> bool:
        return True

    def readinto(self, target: bytearray) -> int:
        if not target:
            return 0
        data = self._backend.read_range(self._path, self._offset, len(target))
        if not data:
            return 0
        target[: len(data)] = data
        self._offset += len(data)
        return len(data)


class WinRMPath(WindowsPathname, Path):
    """A Windows path whose I/O executes through a WinRM host."""

    __slots__ = ("_backend",)

    def copy(self, target, **kwargs):
        return Path.copy(self, target, **kwargs)

    def move(self, target, **kwargs):
        return Path.move(self, target, **kwargs)

    def _copy_from(self, source, **kwargs):
        if self.exists() and not kwargs.get("overwrite", False):
            raise FileExistsError(str(self))
        with source.open("rb") as src, self.open("wb") as dst:
            while chunk := src.read(1024 * 1024):
                dst.write(chunk)
        return self

    def __init__(self, *segments, backend=None):
        # Python 3.14's pathlib.PurePath.__init__ no longer accepts kwargs.
        # Path state is initialized by __new__; backend is attached there.
        if not hasattr(self, "_raw_paths") and not hasattr(self, "_parts"):
            _StdPurePath.__init__(self, *segments)

    def __new__(
        cls,
        *segments: typing.Union[str, WindowsPathname],
        backend: typing.Optional[WinRMPathBackend] = None,
    ):
        inherited = next(
            (segment.backend for segment in segments if isinstance(segment, WinRMPath)),
            None,
        )
        self = super().__new__(cls, *segments)
        self._backend = backend or inherited
        if self._backend is None:
            raise TypeError("WinRMPath requires a backend")
        return self

    @property
    def backend(self) -> WinRMPathBackend:
        return self._backend

    def with_segments(self, *segments: str):
        return type(self)(*segments, backend=self.backend)

    def __truediv__(self, key):
        return type(self)(self, key, backend=self.backend)

    def joinpath(self, *args):
        return type(self)(self, *args, backend=self.backend)

    @property
    def parent(self):
        return type(self)(str(super().parent), backend=self.backend)

    def stat(self, *, follow_symlinks: bool = True) -> FileStat:
        return self.backend.stat(str(self), follow_symlinks=follow_symlinks)

    def _scandir(self):
        yield from self.backend.scandir(str(self))

    def iterdir(self):
        for name, _ in self._scandir():
            yield self / name

    def _open(self, mode="r", buffering=-1):
        if (
            not mode
            or sum(mode.count(value) for value in "rwax") != 1
            or mode.count("+") > 1
            or len(mode.replace("t", "")) != 1 + mode.count("+")
            or mode.count("t") > 1
        ):
            raise ValueError(f"invalid mode: {mode!r}")
        readable = "r" in mode or "+" in mode
        writable = any(value in mode for value in "wax+")
        if "r" in mode and not writable and hasattr(self.backend, "open_read"):
            return self.backend.open_read(str(self))
        if "r" in mode or "a" in mode:
            try:
                value = self.backend.read_bytes(str(self))
            except FileNotFoundError:
                if "a" in mode:
                    value = b""
                else:
                    raise
        else:
            value = b""
        stream = _WriteBackBytesIO(
            value,
            (
                (
                    lambda data: self.backend.write_bytes(
                        str(self), data, exclusive="x" in mode
                    )
                )
                if writable
                else None
            ),
        )
        if "a" in mode:
            stream.seek(0, io.SEEK_END)
        elif not readable:
            stream.seek(0)
        return stream

    def symlink_to(self, target, target_is_directory: bool = False):
        """Create this path as a symbolic link to ``target``.

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

    def chmod(self, mode: int, *, follow_symlinks: bool = True):
        if not follow_symlinks:
            raise NotImplementedError("WinRMPath chmod does not support links")
        self.backend.chmod(str(self), mode)

    def unlink(self, missing_ok=False):
        self.backend.unlink(str(self), missing_ok=missing_ok)

    def rmdir(self):
        self.backend.rmdir(str(self))

    def rename(self, target):
        if not isinstance(target, WinRMPath):
            target = type(self)(target, backend=self.backend)
        if target.backend is not self.backend:
            raise ValueError("cannot rename across WinRM path backends")
        self.backend.rename(str(self), str(target))
        return target


class WinRMExecutorProvider(ExecutorProvider):
    """Lifecycle-owning WinRM/PSRP executor provider."""

    def __init__(self, transport: _WinRMTransport):
        self.transport = transport
        capabilities = set(transport.executor.executor_capabilities)
        if "runspace" in transport.capabilities:
            capabilities.add("runspace")
        super().__init__("winrm", transport.executor, capabilities=capabilities)

    def probe(self):
        return ProviderProbe("available", capabilities=self.capabilities)

    def connect(self):
        try:
            self.transport.connect()
        except (ConnectionError, TimeoutError) as exc:
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
