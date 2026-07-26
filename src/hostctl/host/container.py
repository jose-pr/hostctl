"""Docker Engine container host implementation."""

from __future__ import annotations

import dataclasses
import subprocess
import typing
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from urllib.parse import quote, unquote, urlencode

from pathlib_next import Pathname, PosixPathname, WindowsPathname

from ..executor.container import (
    ContainerExecutor,
    ContainerLike,
    normalize_container_error,
)
from ..executor import normalize_environment
from ..process import (
    ContainerProcess,
    Process,
    TerminalRequest,
    terminal_options,
)
from ..shell import (
    POWERSHELL,
    POSIX_SHELL,
    ShellFlavour,
    ShellFlavourSelection,
    shell_flavour,
)
from ._common import (
    CaptureOutput,
    Command,
    Environment,
    FileHandle,
    Host,
    HostConfig,
    HostInfo,
    HostPath,
    Input,
    PathLike,
    is_direct_command,
    parse_host_info,
    strict_uri_credentials,
    strict_uri_query,
)
from .container_path import (
    ContainerPathBackend,
    PosixContainerPath,
    WindowsContainerPath,
)

PathnameConstructor = typing.Type[typing.Union[PurePath, Pathname]]
ContainerShellSelection = typing.Union[ShellFlavourSelection, typing.Literal["auto"]]
ContainerPathSelection = typing.Union[PathnameConstructor, typing.Literal["auto"]]


def _path_selection(value: str) -> ContainerPathSelection:
    try:
        return {
            "auto": "auto",
            "posix": PosixPathname,
            "windows": WindowsPathname,
        }[value]
    except KeyError as exc:
        raise ValueError("path_flavor must be 'auto', 'posix', or 'windows'") from exc


@dataclasses.dataclass
class ContainerConfig(HostConfig, schemes=("docker",)):
    """Docker Engine target and in-container execution defaults."""

    container: str
    engine_url: typing.Optional[str] = None
    user: typing.Optional[str] = None
    workdir: typing.Optional[str] = None
    executable: typing.Optional[str] = None
    dialect: ContainerShellSelection = "auto"
    path_flavor: ContainerPathSelection = "auto"
    client_factory: typing.Optional[typing.Callable[..., object]] = dataclasses.field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        HostConfig.__init__(self)
        if not self.container:
            raise ValueError("container must not be empty")
        if self.dialect != "auto":
            self.dialect = shell_flavour(self.dialect)
        elif self.executable is not None:
            raise ValueError("executable cannot be combined with dialect='auto'")
        if self.path_flavor != "auto":
            value = self.path_flavor
            if not isinstance(value, type) or not issubclass(
                value, (Pathname, PurePath)
            ):
                raise TypeError("path_flavor must be a pure-path class or 'auto'")
            if value is PurePath or not issubclass(
                value, (PurePosixPath, PureWindowsPath)
            ):
                raise TypeError("path_flavor must use POSIX or Windows semantics")

    @property
    def connection_uri(self) -> str:
        query: typing.Dict[str, object] = {
            "dialect": self.dialect,
            "path_flavor": (
                self.path_flavor
                if self.path_flavor == "auto"
                else (
                    "windows"
                    if issubclass(
                        typing.cast(PathnameConstructor, self.path_flavor),
                        PureWindowsPath,
                    )
                    else "posix"
                )
            ),
        }
        for name in ("engine_url", "user", "workdir", "executable"):
            value = getattr(self, name)
            if value is not None:
                query[name] = value
        return f"docker://{quote(self.container, safe='')}" + (
            f"?{urlencode(query)}" if query else ""
        )

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials: object) -> ContainerConfig:
        strict_uri_credentials(credentials, ("client_factory",))
        query = strict_uri_query(
            parsed,
            {
                "engine_url",
                "user",
                "workdir",
                "executable",
                "dialect",
                "path_flavor",
            },
        )
        if not parsed.netloc or parsed.path not in ("", "/"):
            raise ValueError("Docker URI requires a container and no path")
        if parsed.username or parsed.password or parsed.port:
            raise ValueError("Docker URI authority must contain only a container name")
        return cls(
            container=unquote(parsed.netloc),
            engine_url=query.get("engine_url") or None,
            user=query.get("user") or None,
            workdir=query.get("workdir") or None,
            executable=query.get("executable") or None,
            dialect=(
                "auto"
                if query.get("dialect", "auto") == "auto"
                else shell_flavour(query["dialect"])
            ),
            path_flavor=_path_selection(query.get("path_flavor", "auto")),
            client_factory=typing.cast(
                typing.Optional[typing.Callable[..., object]],
                credentials.get("client_factory"),
            ),
        )

    def _create_host(self) -> ContainerHost:
        return ContainerHost(self)


class ContainerHost(Host):
    """A running container reached through the Docker Engine API."""

    def __init__(self, config: ContainerConfig) -> None:
        self.config = config
        self._client: typing.Optional[object] = None
        self._container: typing.Optional[ContainerLike] = None
        self._attrs: typing.Optional[typing.Mapping[str, object]] = None
        self._executor = ContainerExecutor(
            lambda: self.container,
            user=config.user,
            workdir=config.workdir,
        )

    @property
    def capabilities(self) -> typing.FrozenSet[str]:
        return frozenset(("path", "run", "spawn", "tty"))

    @property
    def client(self) -> object:
        if self._client is None:
            factory = self.config.client_factory
            if factory is None:
                try:
                    import docker
                except ImportError as exc:
                    raise ImportError(
                        "container support requires the 'container' extra: "
                        "pip install hostctl[container]"
                    ) from exc
                factory = (
                    docker.DockerClient if self.config.engine_url else docker.from_env
                )
            self._client = (
                factory(base_url=self.config.engine_url)
                if self.config.engine_url is not None
                else factory()
            )
        return self._client

    @property
    def container(self) -> ContainerLike:
        if self._container is None:
            try:
                container = self.client.containers.get(self.config.container)
                container.reload()
            except Exception as exc:
                normalized = normalize_container_error(exc)
                if normalized is exc:
                    raise
                raise normalized from exc
            attrs = typing.cast(typing.Mapping[str, object], container.attrs)
            state = typing.cast(typing.Mapping[str, object], attrs.get("State", {}))
            if not state.get("Running", False):
                raise ConnectionError(
                    f"container {self.config.container!r} is not running"
                )
            self._container = typing.cast(ContainerLike, container)
            self._attrs = attrs
        return self._container

    @property
    def inspected_os(self) -> str:
        _ = self.container
        attrs = self._attrs or {}
        value = str(attrs.get("Platform") or attrs.get("Os") or "").casefold()
        if not value:
            image = getattr(self._container, "image", None)
            value = str(getattr(image, "attrs", {}).get("Os", "")).casefold()
        if value not in ("linux", "windows"):
            raise RuntimeError(
                "unsupported or undetected container OS: " f"{value or '<empty>'}"
            )
        return value

    @property
    def shell_flavour(self) -> ShellFlavour:
        if self.config.dialect != "auto":
            return typing.cast(ShellFlavour, self.config.dialect)
        return POWERSHELL if self.inspected_os == "windows" else POSIX_SHELL

    @property
    def executor(self) -> ContainerExecutor:
        return self._executor

    def connect(self) -> None:
        _ = self.container

    def close(self) -> None:
        self._container = None
        self._attrs = None
        if self._client is not None:
            client, self._client = self._client, None
            close = getattr(client, "close", None)
            if close is not None:
                close()

    def info(self) -> HostInfo:
        result = self.run(self.shell_flavour.info_script, check=False, encoding="utf-8")
        info = parse_host_info(result.stdout)
        attrs = self._attrs or {}
        return HostInfo(
            hostname=info.hostname,
            os_family=info.os_family or self.inspected_os,
            os_name=info.os_name,
            os_version=info.os_version,
            architecture=info.architecture
            or typing.cast(typing.Optional[str], attrs.get("Architecture")),
        )

    def path(
        self, *segments: PathLike, backend: typing.Optional[str] = None
    ) -> HostPath:
        if backend not in (None, "archive", "docker"):
            raise ValueError(f"unsupported container path backend: {backend!r}")
        path_backend = ContainerPathBackend(self.container)
        selection = self.config.path_flavor
        windows = (
            self.inspected_os == "windows"
            if selection == "auto"
            else issubclass(
                typing.cast(PathnameConstructor, selection), PureWindowsPath
            )
        )
        if windows:
            values = segments or ("C:\\",)
            return WindowsContainerPath(*values, backend=path_backend)
        values = segments or ("/",)
        return PosixContainerPath(*values, backend=path_backend)

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
        if cmds and is_direct_command((cmds[0],)):
            return self.executor(
                cmds[0],
                *cmds[1:],
                bufsize=bufsize,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                cwd=cwd,
                env=env,
                capture_output=capture_output,
                check=check,
                encoding=encoding,
                errors=errors,
                input=input,
                timeout=timeout,
                text=text,
            )
        script = self.shell_flavour.script(cmds, cwd=None, env=None)
        invocation = self.shell_flavour.invocation(
            script, executable=executable or self.config.executable
        )
        return self.executor(
            invocation[0],
            *invocation[1:],
            bufsize=bufsize,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            env=env,
            capture_output=capture_output,
            check=check,
            encoding=encoding,
            errors=errors,
            input=input,
            timeout=timeout,
            text=text,
        )

    def spawn(
        self,
        *cmds: Command,
        executable: typing.Optional[str] = None,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
        terminal: TerminalRequest = None,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
    ) -> Process:
        """Start a persistent Docker exec process with an optional TTY."""
        if cmds and is_direct_command((cmds[0],)):
            invocation = [
                str(cmds[0]),
                *[
                    value.decode() if isinstance(value, bytes) else str(value)
                    for value in cmds[1:]
                ],
            ]
            environment = normalize_environment(env)
        elif cmds:
            script = self.shell_flavour.script(cmds, cwd=None, env=None)
            invocation = self.shell_flavour.invocation(
                script, executable=executable or self.config.executable
            )
            environment = normalize_environment(env)
        else:
            if cwd is not None or env is not None:
                raise ValueError("cwd and env require a command when spawning")
            selected = executable or self.config.executable
            invocation = (
                [selected]
                if selected
                else list(self.shell_flavour.invocation("", executable=None)[:1])
            )
            environment = None

        selected_terminal = terminal_options(terminal)
        tty = selected_terminal is not None
        api = typing.cast(typing.Any, self.client).api
        selected_workdir = str(cwd) if cwd is not None else self.config.workdir
        options: typing.Dict[str, object] = {
            "cmd": list(invocation),
            "stdin": True,
            "stdout": True,
            "stderr": True,
            "tty": tty,
        }
        if environment is not None:
            options["environment"] = environment
        if selected_workdir is not None:
            options["workdir"] = selected_workdir
        if self.config.user is not None:
            options["user"] = self.config.user
        try:
            created = api.exec_create(
                getattr(self.container, "id", self.config.container),
                **options,
            )
            exec_id = created["Id"] if isinstance(created, dict) else created
            stream = api.exec_start(exec_id, socket=True, tty=tty)
            if selected_terminal is not None:
                api.exec_resize(
                    exec_id,
                    height=selected_terminal.rows,
                    width=selected_terminal.columns,
                )
        except Exception as exc:
            normalized = normalize_container_error(exc)
            if normalized is exc:
                raise
            raise normalized from exc
        return ContainerProcess(
            api,
            str(exec_id),
            stream,
            tty=tty,
            command=list(invocation),
            encoding=encoding,
            errors=errors,
        )
