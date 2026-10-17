"""The container host: exec, sessions and archive paths over the Docker API."""

from __future__ import annotations


import os
import subprocess
import typing
from pathlib import PureWindowsPath


from ._executor import (
    ContainerExecutor,
    ContainerLike,
    _is_not_found,
    normalize_container_error,
)
from ...executor import (
    _OMITTED,
    normalize_environment,
    command_text,
    raise_normalized,
    CaptureOutput,
    CommandEnvironment,
    StreamTarget,
    CommandInput,
    StrPath,
)
from ...exceptions import MissingExtraError, OperationNotStartedError
from ...provider._selector import ProviderSelector
from ._providers import ContainerArchivePathProvider, ContainerExecutorProvider
from ._process import ContainerProcess
from ...process import Process, TerminalRequest, terminal_options
from ...shell import POWERSHELL, POSIX_SHELL, shell_flavour
from ...shell._flavour import ShellFlavour
from ...shell._grammar import Command, starts_direct_command
from ...host._host import Host
from ...host._info import HostInfo, parse_host_info
from ...host._timeout import DEFAULT

from pathapi import Path


from ._path import ContainerPathBackend, PosixContainerPath, WindowsContainerPath
from ...host._selection import PathnameType
from ._config import ContainerConfig
from ...host._tls import _trust_adapter
from ...host._verify import _ca_bundle


def _pinned_client(
    verify: typing.Union[bool, str],
    client_cert: typing.Optional[typing.Tuple[str, str]] = None,
) -> typing.Callable[..., object]:
    """A `docker.DockerClient` factory whose every request checks with `verify`
    and presents `client_cert`.

    docker-py sends through `requests.Session.request`, which replaces the
    session's `verify` -- a CA bundle, or `False` -- with `REQUESTS_CA_BUNDLE`
    whenever that is set. The first request is sent from the constructor, so
    the override has to be in the class. `True` trusts the OS store, or an
    environment bundle, installed on each `https://` adapter as it is mounted.
    """
    import docker

    class _API(docker.APIClient):
        def mount(self, prefix, adapter):
            if verify is True and prefix.casefold() == "https://":
                _trust_adapter(adapter)
            super().mount(prefix, adapter)

        def merge_environment_settings(self, url, proxies, stream, verify, cert):
            settings = super().merge_environment_settings(
                url, proxies, stream, verify, cert
            )
            settings["verify"] = self.verify
            return settings

    class _Client(docker.DockerClient):
        def __init__(self, **options: object) -> None:
            # `DockerClient.__init__` builds `self.api` and nothing else.
            self.api = _API(
                tls=docker.tls.TLSConfig(client_cert=client_cert, verify=verify),
                **options,
            )

    return _Client


def _client_cert_files(pair: typing.Tuple[str, str]) -> typing.Tuple[str, str]:
    """`pair` when both files exist; raises `FileNotFoundError` naming the first
    that does not, before any request rather than as a TLS error."""
    for role, path in zip(("certificate", "key"), pair):
        if not os.path.isfile(path):
            raise FileNotFoundError(f"client {role} not found: {path}")
    return pair


def _close_hijacked(stream: object) -> None:
    """Close a hijacked docker connection, socket included.

    docker-py hands back a `socket.SocketIO` whose `close()` leaves the
    underlying socket open, so closing only the wrapper leaked the daemon
    connection until the garbage collector broke its reference cycle.
    """
    for attribute in ("_sock", "_socket", "sock"):
        raw = getattr(stream, attribute, None)
        close = getattr(raw, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass
    close = getattr(stream, "close", None)
    if close is not None:
        try:
            close()
        except Exception:
            pass


class ContainerHost(Host):
    """A running container reached through the Docker Engine API.

    Commands and paths are assembled from ordered providers
    (:class:`ContainerExecutorProvider` and
    :class:`ContainerArchivePathProvider`) without changing the public API.
    The archive provider declares only the operations the Docker archive API
    can perform, so an unsupported mutation is rejected outright instead of
    falling through to another provider.
    """

    def __init__(
        self,
        config: ContainerConfig,
        *,
        executor_providers: typing.Iterable[object] = (),
        path_providers: typing.Iterable[object] = (),
    ) -> None:
        self.config = config
        self._client: typing.Optional[object] = None
        self._container: typing.Optional[ContainerLike] = None
        #: The one archive backend handed to every path, so `close()` can
        #: refuse the paths already out.
        self._path_backend: typing.Optional[ContainerPathBackend] = None
        self._attrs: typing.Optional[typing.Mapping[str, object]] = None
        self._executor = ContainerExecutor(
            lambda: self.container,
            user=config.username,
            workdir=config.workdir,
        )
        self._executor_provider_selector = ProviderSelector(
            tuple(executor_providers)
            or (
                ContainerExecutorProvider(
                    self._executor,
                    connect=lambda: self.container,
                    close=self._close_client,
                ),
            )
        )
        self._path_provider_selector = ProviderSelector(
            tuple(path_providers) or (ContainerArchivePathProvider(self._archive_path),)
        )

    @property
    def executor_providers(self) -> typing.Tuple[object, ...]:
        """The ordered command providers backing :meth:`run`."""
        return self._executor_provider_selector.providers

    @property
    def path_providers(self) -> typing.Tuple[object, ...]:
        """The ordered filesystem providers backing :meth:`path`."""
        return self._path_provider_selector.providers

    def _run_selector(self) -> ProviderSelector:
        return self._executor_provider_selector

    @property
    def capabilities(self) -> typing.FrozenSet[str]:
        values = set()
        if any(
            self._executor_provider_selector.probe(provider).usable
            for provider in self._executor_provider_selector.providers
        ):
            values.update(("run", "spawn", "tty"))
        if any(
            self._path_provider_selector.probe(provider).usable
            for provider in self._path_provider_selector.providers
        ):
            values.add("path")
        return frozenset(values)

    @property
    def client(self) -> object:
        if self._client is None:
            factory = self.config.client_factory
            if factory is None:
                try:
                    import docker
                except ImportError as exc:
                    raise MissingExtraError("container", "a Docker container") from exc
                factory = (
                    docker.DockerClient if self.config.engine_url else docker.from_env
                )
            options: typing.Dict[str, object] = {}
            if self.config.engine_url is not None:
                options["base_url"] = self.config.engine_url
            connect = self.config.timeout.connect
            if connect is not DEFAULT:
                # Left out at DEFAULT, so the SDK (or a client_factory) keeps
                # its own; `None` is the SDK's "no limit".
                options["timeout"] = connect
            verify = self.config.verify
            engine = (self.config.engine_url or "").casefold()
            if self.config.client_factory is None and engine.startswith("https://"):
                # The config refuses a verify other than True, and a client
                # certificate, anywhere else.
                factory = _pinned_client(
                    (
                        verify
                        if isinstance(verify, bool)
                        else _ca_bundle(typing.cast(str, verify))
                    ),
                    (
                        None
                        if self.config.client_cert is None
                        else _client_cert_files(self.config.client_cert)
                    ),
                )
            self._client = factory(**options)
        return self._client

    @property
    def container(self) -> ContainerLike:
        if self._container is None:
            try:
                container = self.client.containers.get(self.config.container)
                container.reload()
            except Exception as exc:
                if _is_not_found(exc):
                    raise ConnectionError(
                        f"container {self.config.container!r} not found"
                    ) from exc
                raise_normalized(exc, normalize_container_error)
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
        if self.config.shell != "auto":
            return typing.cast(ShellFlavour, self.config.shell)
        return POWERSHELL if self.inspected_os == "windows" else POSIX_SHELL

    @property
    def executor(self) -> ContainerExecutor:
        return self._executor_provider_selector.select().provider.executor

    def connect(self) -> None:
        _ = self.container

    def _close_client(self) -> None:
        backend, self._path_backend = self._path_backend, None
        if backend is not None:
            # docker-py reopens a closed client on the next request, so a path
            # obtained earlier would otherwise quietly leak a new connection.
            backend.invalidate()
        self._container = None
        self._attrs = None
        if self._client is not None:
            client, self._client = self._client, None
            close = getattr(client, "close", None)
            if close is not None:
                close()

    def close(self) -> None:
        first_error: typing.Optional[BaseException] = None
        closed: typing.List[int] = []
        for provider in (
            *self._executor_provider_selector.providers,
            *self._path_provider_selector.providers,
        ):
            if id(provider) in closed:
                continue
            closed.append(id(provider))
            close = getattr(provider, "close", None)
            if close is None:
                continue
            try:
                close()
            except BaseException as exc:  # pragma: no cover - defensive
                if first_error is None:
                    first_error = exc
        self._close_client()
        self._executor_provider_selector.invalidate()
        self._path_provider_selector.invalidate()
        if first_error is not None:  # pragma: no cover - defensive
            raise first_error

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

    def path(self, *segments: StrPath, backend: typing.Optional[str] = None) -> Path:
        names = {provider.name for provider in self._path_provider_selector.providers}
        if backend is not None and backend not in (names | {"docker"}):
            raise ValueError(f"unsupported container path backend: {backend!r}")
        if backend in (None, "docker"):
            provider = self._path_provider_selector.select().provider
        else:
            provider = next(
                item
                for item in self._path_provider_selector.providers
                if item.name == backend
            )
            if not provider.probe().usable:
                raise OperationNotStartedError(
                    f"path provider {backend!r} is unavailable"
                )
        return provider.path(*segments)

    def _archive_path(self, *segments: StrPath) -> Path:
        """Build a Docker-archive-backed path with the configured flavour."""
        selection = self.config.pathname
        windows = (
            self.inspected_os == "windows"
            if selection == "auto"
            else issubclass(typing.cast(PathnameType, selection), PureWindowsPath)
        )
        container = self.container
        flavour = "windows" if windows else "posix"
        path_backend = self._path_backend
        if (
            path_backend is None
            or path_backend.container is not container
            or path_backend.pathname != flavour
        ):
            path_backend = self._path_backend = ContainerPathBackend(
                container, pathname=flavour
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
        timeout: typing.Optional[float] = _OMITTED,  # type: ignore[assignment]
        text: typing.Optional[bool] = None,
    ) -> subprocess.CompletedProcess:
        # The config supports no total, so an omitted timeout is no limit.
        if timeout is _OMITTED or timeout is DEFAULT:
            timeout = None
        excluded: typing.List[str] = []
        while True:
            provider = self._executor_provider_selector.select(
                exclude=excluded
            ).provider
            try:
                return self._run_with_provider(
                    provider,
                    cmds,
                    bufsize=bufsize,
                    executable=executable,
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
            except OperationNotStartedError as exc:
                # Only a proven pre-dispatch refusal may reach another
                # provider; a dispatched exec is never replayed.
                self._executor_provider_selector.decline(provider.name, str(exc))
                excluded.append(provider.name)

    def _run_with_provider(
        self,
        provider,
        cmds: typing.Sequence[Command],
        *,
        bufsize: int,
        executable: typing.Optional[str],
        stdin,
        stdout,
        stderr,
        cwd,
        env,
        capture_output,
        check,
        encoding,
        errors,
        input,
        timeout,
        text,
    ) -> subprocess.CompletedProcess:
        direct = starts_direct_command(cmds)
        if direct is not None:
            command, args = direct
            if executable is not None:
                raise NotImplementedError(
                    "executable cannot be combined with a direct command"
                )
            return provider.execute(
                command,
                *args,
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
        return provider.execute(
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
        cwd: typing.Optional[StrPath] = None,
        env: typing.Optional[CommandEnvironment] = None,
        terminal: TerminalRequest = None,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
    ) -> Process:
        """Start a persistent Docker exec process with an optional TTY."""
        # Either `errors=` or `encoding=` selects text mode, the rule every
        # transport follows (`executor.wants_text`), so `errors="replace"`
        # alone applies to undecodable output.
        if errors is not None and encoding is None:
            encoding = "utf-8"
        direct = starts_direct_command(cmds)
        if direct is not None:
            command, args = direct
            if executable is not None:
                # `run()` refuses this too, so the same call means one thing
                # at both entry points.
                raise NotImplementedError(
                    "executable cannot be combined with a direct command"
                )
            invocation = [command_text(command), *(command_text(v) for v in args)]
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
                [selected] if selected else [self.shell_flavour.default_executable]
            )
            environment = None

        selected_terminal = terminal_options(terminal)
        tty = selected_terminal is not None
        # Through the provider, as `run()` goes: it inspects the container
        # before dispatch, so a stopped or vanished container is a proven
        # pre-dispatch refusal (`OperationNotStartedError`/`ConnectionError`)
        # rather than the `FileNotFoundError` the raw API answers with --
        # two different errors for one condition, depending on which entry
        # point the caller used.
        provider = self._executor_provider_selector.select().provider
        connect = getattr(provider, "connect", None)
        if callable(connect):
            connect()
        api = typing.cast(typing.Any, self.client).api
        selected_workdir = str(cwd) if cwd is not None else self.config.workdir
        options: typing.Dict[str, object] = {
            "cmd": list(invocation),
            "stdin": True,
            "stdout": True,
            "stderr": True,
            "tty": tty,
        }
        if selected_terminal is not None and selected_terminal.term_type:
            # `term_type` was accepted and never sent. Docker's exec API has
            # no TERM field, so it travels as the environment variable every
            # program actually reads -- without clobbering a caller's own.
            environment = dict(environment or {})
            environment.setdefault("TERM", selected_terminal.term_type)
        if environment is not None:
            options["environment"] = environment
        if selected_workdir is not None:
            options["workdir"] = selected_workdir
        if self.config.username is not None:
            options["user"] = self.config.username
        try:
            created = api.exec_create(
                getattr(self.container, "id", self.config.container),
                **options,
            )
            exec_id = created["Id"] if isinstance(created, dict) else created
            stream = api.exec_start(exec_id, socket=True, tty=tty)
            if selected_terminal is not None:
                try:
                    api.exec_resize(
                        exec_id,
                        height=selected_terminal.rows,
                        width=selected_terminal.columns,
                    )
                except Exception:
                    _close_hijacked(stream)
                    raise
        except Exception as exc:
            raise_normalized(exc, normalize_container_error)
        try:
            return ContainerProcess(
                api,
                str(exec_id),
                stream,
                tty=tty,
                command=list(invocation),
                encoding=encoding,
                errors=errors,
            )
        except Exception:
            # The exec has already started, so the hijacked daemon connection
            # is live, and this construction can raise (a bad `encoding` gives
            # LookupError). `SocketIO.close()` does not close the underlying
            # socket, which would then stay open until a GC pass.
            _close_hijacked(stream)
            raise
