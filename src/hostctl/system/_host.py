"""`SystemHost`: a host composed from ordered executor and path providers."""

from __future__ import annotations

import collections.abc
import dataclasses
import logging
import subprocess
import threading
import typing
from urllib.parse import quote

from pathapi import Path

from ..executor import (
    command_text,
    masked_environment,
    run_displayed,
    validate_environment_names,
    CaptureOutput,
    CommandEnvironment,
    StreamTarget,
    CommandInput,
    StrPath,
)
from ..exceptions import OperationNotStartedError
from ..provider._selector import ProviderSelector
from ..shell import shell_flavour
from ..shell._flavour import ShellFlavour
from ..shell._invocation import render_command
from ..shell._vocabulary import wants_epilogue
from ..host._host import Host, _HostExecutor
from ..host._config import HostConfig
from ..host._info import HostInfo
from ..shell._grammar import Command, starts_direct_command
from ..paths._composite import CompositePosixPath, CompositeWindowsPath
from ._config import SystemConfig
from ._shell_plan import _ShellContext, _shell_invocation

log = logging.getLogger(__name__)


class SystemHost(Host):
    """Host orchestration shared by POSIX, Windows, and IOS systems."""

    system_family = "generic"
    default_shell: typing.Optional[ShellFlavour] = None
    #: Configuration class used when a host is constructed without one.
    #: Bound by each concrete family below, once those classes exist.
    config_type: typing.ClassVar[typing.Type[SystemConfig]]

    def __init__(
        self,
        config: typing.Optional[SystemConfig] = None,
        *,
        executor_providers=(),
        path_providers=(),
        shell=None,
        info: typing.Optional[HostInfo] = None,
        initializer=None,
    ):
        # A config-less host builds the configuration matching its own system
        # family, which keeps `scheme` derived from the URI: `HostConfig`
        # defines it as a read-only computed property.
        if isinstance(config, str):
            raise TypeError(
                f"{type(self).__name__} takes a config, not a connection string; "
                "use Host(uri) or HostConfig(uri).open()"
            )
        if config is not None and not isinstance(config, HostConfig):
            raise TypeError(
                f"{type(self).__name__} takes a HostConfig, "
                f"not {type(config).__name__}"
            )
        self.config = config if config is not None else self.config_type()
        self._executor_selector = ProviderSelector(executor_providers)
        self._path_selector = ProviderSelector(path_providers)
        self._shell_resolver = (
            shell if callable(shell) and not isinstance(shell, type) else None
        )
        self._shell = (
            None
            if self._shell_resolver is not None
            else (
                shell_flavour(shell)
                if shell is not None
                else (
                    shell_flavour(getattr(self.config, "shell", None))
                    if getattr(self.config, "shell", None)
                    else self.default_shell
                )
            )
        )
        self._info = info
        config_options = getattr(self.config, "options", {})
        self._initializer = (
            initializer
            if initializer is not None
            else (
                config_options.get("initializer")
                if isinstance(config_options, dict)
                else None
            )
        )
        if self._initializer is not None and not callable(self._initializer):
            raise TypeError("initializer must be callable")
        self._initializer_generation = False
        self._initializing = False
        self._connected = False
        self._connected_providers = []
        self._closed_targets = set()
        # Serializes this host's connection *bookkeeping*: `_connected`,
        # `_connected_providers`, `_closed_targets`, and `_initializer_
        # generation`.  Without it, concurrent `run()` calls race the
        # check-then-append in `_ensure_provider_connected` and each append a
        # duplicate entry for the same provider, so the list grows without
        # bound and every racing caller repeats the connect round-trip.
        #
        # RLock, not Lock: these paths nest.  `connect()` runs the session
        # initializer, which is handed this same host and legitimately calls
        # `run()` -> `_ensure_provider_connected`; a plain Lock would deadlock
        # on that re-entry.
        #
        # Scope: the lock deliberately spans the provider `connect()` call.
        # Connecting is the operation being deduplicated, so releasing the
        # lock around it would reintroduce the very race it exists to close --
        # two callers would both observe "not connected" and both dial out.
        # Providers already own the slow part behind their own locks
        # (`_SSHTransport._ssh_lock`), and a `SystemHost` is one logical
        # target whose providers are ordered fallbacks for that same target,
        # not independent endpoints to be dialed in parallel.  Command
        # dispatch itself -- `run()`, `path()`, `spawn()` -- stays outside the
        # lock, so this never serializes actual remote work.
        self._lifecycle_lock = threading.RLock()

    @property
    def executor_providers(self) -> typing.Tuple[object, ...]:
        """The ordered command providers backing :meth:`run`."""
        return self._executor_selector.providers

    @property
    def path_providers(self) -> typing.Tuple[object, ...]:
        """The ordered filesystem providers backing :meth:`path`."""
        return self._path_selector.providers

    def replace_providers(self, *, executor=None, path=None) -> None:
        """Swap either provider set; ``None`` keeps it.

        Starts a new connection generation: the session initializer runs again
        before the next operation. A replaced provider's transport is closed
        unless a provider that stays shares it, and a transport shared by
        several replaced providers is closed once. A path obtained earlier
        still routes to the old providers, so obtain it again. An invalid
        set (duplicate names) raises before anything changes.
        """
        with self._lifecycle_lock:
            new_executor = ProviderSelector(executor) if executor is not None else None
            new_path = ProviderSelector(path) if path is not None else None
            old = [
                *(self._executor_selector.providers if new_executor else ()),
                *(self._path_selector.providers if new_path else ()),
            ]
            kept = [
                *(new_executor or self._executor_selector).providers,
                *(new_path or self._path_selector).providers,
            ]
            replaced = [
                provider
                for provider in old
                if not any(provider is item for item in kept)
            ]
            kept_targets = {id(getattr(item, "transport", item)) for item in kept}
            targets = []
            seen = set()
            for provider in replaced:
                target = getattr(provider, "transport", provider)
                if (
                    id(target) not in seen
                    and id(target) not in kept_targets
                    and id(target) not in self._closed_targets
                ):
                    seen.add(id(target))
                    targets.append(target)
            self._connected_providers = [
                item
                for item in self._connected_providers
                if not any(item is provider for provider in replaced)
            ]
            if new_executor is not None:
                self._executor_selector = new_executor
            else:
                self._executor_selector.invalidate()
            if new_path is not None:
                self._path_selector = new_path
            else:
                self._path_selector.invalidate()
            self._connected = False
            self._initializer_generation = False
            first_error = None
            for target in reversed(targets):
                close = getattr(target, "close", None)
                if close:
                    try:
                        close()
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
                self._closed_targets.add(id(target))
            if first_error is not None:
                raise first_error

    @property
    def shell_flavour(self):
        if self._shell is None and self._shell_resolver is not None:
            self._shell = shell_flavour(self._shell_resolver())
        if self._shell is None:
            raise NotImplementedError(
                f"{type(self).__name__} does not configure a shell"
            )
        return self._shell

    @property
    def executor_capabilities(self):
        values = set()
        for provider in self._executor_selector.providers:
            if self._provider_probe(self._executor_selector, provider).usable:
                values.update(self._executor_selector.effective_capabilities(provider))
        return frozenset(values)

    @staticmethod
    def _provider_probe(selector, provider):
        return selector.probe(provider)

    @property
    def provider_details(self):
        """Return deterministic, non-dispatching provider availability details."""
        details = []
        for kind, selector in (
            ("executor", self._executor_selector),
            ("path", self._path_selector),
        ):
            for provider in selector.providers:
                probe = self._provider_probe(selector, provider)
                capabilities = selector.effective_capabilities(provider)
                details.append(
                    {
                        "kind": kind,
                        "name": ProviderSelector.redact(provider.name),
                        "availability": probe.availability,
                        "reason": ProviderSelector.redact(probe.reason),
                        "capabilities": tuple(sorted(capabilities)),
                        "system_hint": probe.system_hint,
                        "policy": "ordered",
                    }
                )
        return tuple(details)

    @property
    def capabilities(self):
        values = set()
        executor_probes = [
            (provider, self._provider_probe(self._executor_selector, provider))
            for provider in self._executor_selector.providers
        ]
        path_probes = [
            (provider, self._provider_probe(self._path_selector, provider))
            for provider in self._path_selector.providers
        ]
        if any(probe.usable for _, probe in executor_probes):
            values.add("run")
        # `spawn` and `tty` were never reported, so an SSH-backed host
        # advertised {run, path} while `spawn()` and TTY sessions worked --
        # and contracts.md tells callers to gate on exactly this set.
        # `ContainerHost` reported them all along; the two now agree.
        for capability in ("runspace", "spawn", "tty"):
            if any(
                probe.usable
                and capability
                in self._executor_selector.effective_capabilities(provider)
                for provider, probe in executor_probes
            ):
                values.add(capability)
        if any(probe.usable for _, probe in path_probes):
            values.add("path")
        return frozenset(values)

    @property
    def executor(self):
        """The selected provider, presented as an `Executor`.

        A bare `ExecutorProvider` has no `executor_capabilities`, so a
        `Shell` built on one inferred CWD/ENV support from the callable's
        signature instead of reading the provider's declared set -- and
        inferred it wrongly. The wrapper carries the declared capabilities
        and dispatches through `run()`, so the host's own rendering and
        failover still apply.
        """
        selected = self._executor_selector.select()
        provider = selected.provider
        return _HostExecutor(self, frozenset(provider.capabilities), provider)

    def connect(self):
        with self._lifecycle_lock:
            self._connect_locked()

    def _connect_locked(self):
        if self._connected:
            return
        connected = []
        # A selector that offered a candidate is a connection that was meant
        # to happen; if none of them did, saying so beats a `_connected` host
        # that fails on its first use.
        candidates = 0
        declined: typing.List[str] = []
        first_decline: typing.Optional[OperationNotStartedError] = None
        try:
            for selector in (self._executor_selector, self._path_selector):
                excluded = []
                while True:
                    try:
                        provider = selector.select(exclude=excluded).provider
                    except OperationNotStartedError:
                        break
                    candidates += 1
                    try:
                        connect = getattr(provider, "connect", None)
                        if connect:
                            connect()
                    except OperationNotStartedError as exc:
                        excluded.append(provider.name)
                        declined.append(
                            f"{ProviderSelector.redact(provider.name)}: "
                            f"{ProviderSelector.redact(exc)}"
                        )
                        first_decline = first_decline or exc
                        continue
                    target = getattr(provider, "transport", provider)
                    self._closed_targets.discard(id(target))
                    connected.append(provider)
                    break
        except BaseException:
            for provider in reversed(connected):
                close = getattr(provider, "close", None)
                if close:
                    close()
            raise
        if candidates and not connected:
            raise OperationNotStartedError(
                "no provider connected: " + "; ".join(declined),
                cause=first_decline,
            ) from first_decline
        self._connected_providers = connected
        self._run_initializer_locked(connected)
        self._connected = True

    def _run_initializer_locked(self, connected) -> None:
        """Run the session initializer once per connection generation.

        Called from the explicit `connect()` *and* from the lazy path, because
        `run()`, `path()` and `spawn()` all open a generation without going
        through `connect()` -- and providers.md promises the initializer runs
        once per generation, before the host is handed to the caller. It used
        to run only from `connect()`, so a host used lazily was bootstrapped
        silently not at all.

        The `_initializing` guard is what makes the lazy path safe: the
        initializer is handed this host and legitimately calls `run()`, which
        re-enters here through `_ensure_provider_connected` (the lock is an
        RLock for exactly that), and `_initializer_generation` is only set
        afterwards -- so without the guard the nested call would start the
        initializer again.
        """
        if (
            self._initializer is None
            or self._initializer_generation
            or self._initializing
        ):
            return
        self._initializing = True
        try:
            # `SessionInitializer` is itself callable and applies its own
            # default timeout, so it and a plain callable are invoked
            # identically -- there is no separate branch to take.
            self._initializer(self)
            self._initializer_generation = True
        except BaseException:
            for provider in reversed(connected):
                close = getattr(provider, "close", None)
                if close:
                    try:
                        close()
                    except BaseException:
                        pass
            self._connected_providers = []
            self._executor_selector.invalidate()
            self._path_selector.invalidate()
            raise
        finally:
            self._initializing = False

    def close(self):
        with self._lifecycle_lock:
            self._close_locked()

    def _close_locked(self):
        targets = []
        # Dedupe by identity, matching the `id(target)` check below. Value
        # equality would collapse two distinct transports that compare equal
        # (e.g. a user-supplied dataclass transport registered through
        # `register_system_provider`) into one entry and leak the other.
        seen_targets = set()
        for provider in (
            *self._connected_providers,
            *self._executor_selector.providers,
            *self._path_selector.providers,
        ):
            target = getattr(provider, "transport", provider)
            if id(target) not in seen_targets:
                seen_targets.add(id(target))
                targets.append(target)
        first_error = None
        for target in reversed(targets):
            if id(target) in self._closed_targets:
                continue
            close = getattr(target, "close", None)
            if close:
                try:
                    close()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            self._closed_targets.add(id(target))
        self._connected = False
        self._connected_providers = []
        self._initializer_generation = False
        self._executor_selector.invalidate()
        self._path_selector.invalidate()
        if self._shell_resolver is not None:
            self._shell = None
        if first_error is not None:
            raise first_error

    def info(self) -> HostInfo:
        return self._collect_info()[0]

    def _collect_info(
        self,
    ) -> typing.Tuple[HostInfo, bool, typing.List[BaseException]]:
        """`info()` with what it learned about the providers.

        Returns the `HostInfo`, whether some provider answered, and the
        transport errors raised by those that did not. `info()` stays best
        effort; a caller that must tell "nothing answered" from an answer
        (the command line's exit status) reads the rest.
        """
        answered = False
        failures: typing.List[BaseException] = []
        fields = {name: None for name in HostInfo.__dataclass_fields__}
        if self._info is not None:
            fields.update(
                {
                    name: value
                    for name, value in dataclasses.asdict(self._info).items()
                    if value is not None
                }
            )
        for provider in self._executor_selector.providers:
            if all(value is not None for value in fields.values()):
                # Everything is known. Dialling a backup transport merely to
                # read `HostInfo` could prompt for 2FA or wait out a TCP
                # timeout on a host whose primary already answered.
                break
            if not self._provider_probe(self._executor_selector, provider).usable:
                continue
            callback = getattr(provider, "info", None)
            if callback is None:
                continue
            try:
                self._ensure_provider_connected(provider)
                value = callback()
            # Transport failures only. A bare `except Exception` here also
            # swallowed programming errors, so a broken provider reported
            # "no information available" instead of its own traceback.
            except (
                OperationNotStartedError,
                ConnectionError,
                TimeoutError,
                PermissionError,
                OSError,
            ) as exc:
                log.debug(
                    "provider %s could not report info: %s",
                    ProviderSelector.redact(provider.name),
                    type(exc).__name__,
                )
                failures.append(exc)
                continue
            if not isinstance(value, HostInfo):
                continue
            answered = True
            for name, item in dataclasses.asdict(value).items():
                if fields[name] is None and item is not None:
                    fields[name] = item
        authority = getattr(self.config, "authority", None) or getattr(
            self.config, "host", None
        )
        # The host, not the authority: `root@node:2222` is a target, and
        # reporting it as `HostInfo.hostname` put userinfo -- a username, and
        # with it a hint at the credential -- into every diagnostic that
        # printed the hostname.
        hostname = authority
        if isinstance(authority, str) and authority:
            from netimps import split_host

            hostname = authority.rpartition("@")[2]
            try:
                hostname = split_host(hostname)[0]
            except ValueError:
                pass
        if fields["hostname"] is None:
            fields["hostname"] = hostname
        if fields["os_family"] is None:
            fields["os_family"] = self.system_family
        return HostInfo(**fields), answered, failures

    def _composite_path_type(self):
        """The path grammar this system family actually has.

        Any other family raises: an `IOSHost` is session/command-only until
        an IOS path grammar is designed, and POSIX rules would misread
        `flash:/config.text`, where `:` is a device separator rather than part
        of a name.
        """
        if self.system_family == "windows":
            return CompositeWindowsPath
        if self.system_family == "posix":
            return CompositePosixPath
        raise NotImplementedError(
            f"{type(self).__name__} has no path grammar; "
            f"the {self.system_family!r} family is command-only"
        )

    def path(self, *segments: StrPath, backend: typing.Optional[str] = None) -> Path:
        if not self._path_selector.providers:
            raise NotImplementedError(
                f"{type(self).__name__} does not provide the 'path' capability"
            )
        if backend is None:
            provider = self._path_selector.select().provider
        else:
            provider = next(
                (
                    item
                    for item in self._path_selector.providers
                    if item.name == backend
                ),
                None,
            )
            if provider is None:
                raise ValueError(f"unknown path provider: {backend}")
            # Through the selector: it caches the probe, honours a decline, and
            # turns a probe that raises into "unavailable" instead of letting a
            # transport error escape from a routing choice.
            if not self._path_selector.probe(provider).usable:
                raise OperationNotStartedError(
                    f"path provider {backend!r} is unavailable"
                )
        path_type = self._composite_path_type()
        excluded: typing.List[str] = []
        while True:
            try:
                # Note the provider as in use WITHOUT connecting: the host does
                # no I/O to build a path (a provider that must, as SSH does for
                # a relative one, connects itself and declines on failure). The marker is what matters
                # here -- a transport these paths later reopen lazily was left
                # on `_closed_targets`, so every later close() skipped it and
                # the connection lived until process exit.
                self._note_provider_in_use(provider)
                value = provider.path(*segments)
            except OperationNotStartedError as exc:
                if backend is not None:
                    raise
                # Every candidate, not just one. Falling back exactly once
                # meant three ordered providers with the first two offline
                # failed although the third was usable -- while `run()`,
                # which loops, succeeded on the same host.
                self._path_selector.decline(provider.name, str(exc))
                excluded.append(provider.name)
                provider = self._path_selector.select(exclude=excluded).provider
                continue
            # A named backend pins, as `.via(name)` does: every operation on
            # the path goes to the provider the caller chose.
            return path_type.from_path(
                value,
                provider,
                provider.path,
                self._path_selector.providers,
                self._path_selector,
                pinned=backend is not None,
                logical_segments=segments,
            )

    @staticmethod
    def _out_of_band_env(provider, rendered, options):
        """Carry a flavour's out-of-band environment, or refuse to lose it.

        `ShellCommand.environment` means "the environment is NOT embedded in
        this command; send it separately" -- which is what `_SSHTransport.run`
        does. `SystemHost` passed only `.command`, so a registered flavour
        for a shell that cannot export variables lost every one of them in
        silence. Latent for the four built-in flavours, which all return
        `None`; that is a reason to fix it cheaply, not to keep it.
        """
        environment = getattr(rendered, "environment", None)
        if not environment:
            # `None` means embedded, and an empty mapping says the same
            # thing with fewer words -- neither may overwrite an environment
            # the provider is already carrying natively.
            return options
        if "env" not in provider.capabilities:
            raise NotImplementedError(
                f"shell flavour sends its environment out of band, and executor "
                f"provider {provider.name!r} cannot carry one"
            )
        return {**options, "env": environment}

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
        timeout: typing.Optional[float] = None,
        text: typing.Optional[bool] = None,
    ) -> subprocess.CompletedProcess:
        if not self._executor_selector.providers:
            raise NotImplementedError(
                f"{type(self).__name__} does not provide the 'run' capability"
            )
        # Before any provider is selected: whether a name is valid must not
        # depend on which provider happens to carry the call.
        validate_environment_names(env)
        # No `retry_declined()` here. Clearing every refusal per call re-dialled
        # a dead primary on EVERY run() -- ~9 s each against a refused port --
        # although the fallback was serving. The selector re-admits an earlier
        # refusal only when nothing else is left, which is what keeps a
        # single-provider host from bricking on one transient failure.
        excluded: list[str] = []
        while True:
            provider = self._executor_selector.select(exclude=excluded).provider
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
                # Providers may be retried only when they prove no operation
                # was dispatched; planning is repeated for the next provider.
                self._executor_selector.decline(provider.name, str(exc), cause=exc)
                excluded.append(provider.name)

    @staticmethod
    def _shell_context(provider, cwd, env) -> _ShellContext:
        """Which of cwd/env the shell must embed for this provider."""
        return _ShellContext(
            None if "cwd" in provider.capabilities else cwd,
            None if "env" in provider.capabilities else env,
            wants_epilogue(provider.capabilities),
        )

    def _execute_command_line(
        self, provider, flavour, cmds, *, executable, context, options
    ):
        """Render one finalized command line as `(command, args, options)`.

        The one place a `ShellCommand` is turned into a dispatch: it carries
        an out-of-band environment for providers that cannot embed one, and
        it is the only path that needs the login-shell layer.
        """
        login_shell = getattr(provider, "login_shell", None)
        rendered = render_command(
            flavour,
            cmds,
            executable=executable,
            cwd=context.cwd,
            env=context.env,
            login_shell=login_shell,
            # Only a transport that parses with a login shell reads a script
            # from standard input, and only when the call sends none itself.
            stdin_free=login_shell is not None
            and options.get("input") is None
            and options.get("stdin") is None,
        )
        options = self._out_of_band_env(provider, rendered, options)
        if rendered.stdin is not None:
            options["input"] = rendered.stdin
        return rendered.command, (), options

    def _run_with_provider(self, provider, cmds, **kwargs):
        self._ensure_provider_connected(provider)
        command, args, options = self._render_dispatch(provider, cmds, kwargs)
        env = kwargs.get("env")
        if env and isinstance(env, collections.abc.Mapping):
            # A value embedded in the command text would otherwise reach the
            # log and any exception that carries the command; render again with
            # each value masked and show that instead.
            masked = {**kwargs, "env": masked_environment(env)}
            shown = self._render_dispatch(provider, cmds, masked)
            if shown[:2] != (command, args):
                return run_displayed(
                    (shown[0], *shown[1]),
                    lambda: provider.execute(command, *args, **options),
                )
        return provider.execute(command, *args, **options)

    def _render_dispatch(self, provider, cmds, kwargs):
        """The `(command, args, options)` this call sends to `provider`.

        Pure: it connects nothing and sends nothing, so it can be run again
        with a masked environment.
        """
        cwd = kwargs.get("cwd")
        env = kwargs.get("env")
        options = dict(kwargs)
        options.pop("executable", None)
        options.pop("cwd", None)
        options.pop("env", None)
        if cwd is not None and "cwd" in provider.capabilities:
            options["cwd"] = cwd
        if env is not None and "env" in provider.capabilities:
            options["env"] = env
        direct = starts_direct_command(cmds)
        if direct is not None:
            command, args = direct
            if kwargs.get("executable") is not None:
                raise NotImplementedError(
                    "executable cannot be combined with a direct command"
                )
            # `cwd`/`env` were stripped from `options` above and put back only
            # for a provider that carries them natively. Whatever is left must
            # be embedded in a rendered script, or the call silently runs in
            # the wrong directory with the wrong environment.
            native_cwd = cwd is None or "cwd" in provider.capabilities
            native_env = env is None or "env" in provider.capabilities
            if "args" in provider.capabilities and native_cwd and native_env:
                return command, args, options
            if self._shell is None and self._shell_resolver is None:
                if args:
                    raise NotImplementedError(
                        f"executor provider {provider.name!r} does not support argv arguments"
                    )
                if not (native_cwd and native_env):
                    raise NotImplementedError(
                        f"executor provider {provider.name!r} cannot apply cwd or env "
                        "and no shell is configured to embed them"
                    )
                return command_text(command), (), options
            flavour = self.shell_flavour
            shell_executable = getattr(provider, "shell_executable", None)
            context = self._shell_context(provider, cwd, env)
            if "script" in provider.capabilities:
                script = flavour.script(
                    ((command, *args),),
                    cwd=context.cwd,
                    env=context.env,
                    epilogue=context.epilogue,
                )
                return script, (), options
            if "args" in provider.capabilities:
                leading = _shell_invocation(
                    flavour,
                    ((command, *args),),
                    cwd=context.cwd,
                    env=context.env,
                    epilogue=context.epilogue,
                    executable=shell_executable,
                    provider=provider,
                )
                return leading[0], tuple(leading[1:]), options
            if not args and native_cwd and native_env:
                # Nothing to embed. Send the program itself when it needs no
                # quoting -- a provider that is not a shell (a bare callable,
                # a container exec) must not be handed `sh -c ...`. A program
                # whose text would not survive a shell verbatim still goes
                # through the flavour below, because the one provider shape
                # that reaches here -- an SSH exec request -- is read by the
                # remote login shell, which word-split a spaced path.
                text = command_text(command)
                if flavour.quote(text) == text:
                    return text, (), options
            return self._execute_command_line(
                provider,
                flavour,
                ((command, *args),),
                executable=shell_executable,
                context=context,
                options=options,
            )

        if self._shell is None and self._shell_resolver is None:
            raise NotImplementedError(
                "buffered run requires a shell or a direct executable"
            )
        flavour = self.shell_flavour
        executable = kwargs.get("executable")
        context = self._shell_context(provider, cwd, env)
        if "script" in provider.capabilities:
            if executable is not None:
                raise NotImplementedError(
                    f"script executor provider {provider.name!r} "
                    "does not accept an executable override"
                )
            script = flavour.script(
                cmds,
                cwd=context.cwd,
                env=context.env,
                epilogue=context.epilogue,
            )
            return script, (), options
        shell_executable = executable or getattr(provider, "shell_executable", None)
        if "args" not in provider.capabilities:
            return self._execute_command_line(
                provider,
                flavour,
                cmds,
                executable=shell_executable,
                context=context,
                options=options,
            )
        # One shell layer, exactly as LocalHost renders it. `flavour.command()`
        # already contains the shell invocation, so feeding *that* to
        # `invocation()` -- whose argument is a script -- ran the target shell
        # twice: on Windows the outer PowerShell re-parsed the inner
        # `-Command "...; exit $LASTEXITCODE"` as its own double-quoted string,
        # so the status never came back and `check=True` passed for a command
        # that failed.
        leading = _shell_invocation(
            flavour,
            cmds,
            cwd=context.cwd,
            env=context.env,
            epilogue=context.epilogue,
            executable=shell_executable,
            provider=provider,
        )
        return leading[0], tuple(leading[1:]), options

    def _note_provider_in_use(self, provider) -> None:
        """Mark a provider's transport as live again, without connecting it.

        `close()` skips a transport it has already closed; a transport that
        reconnects lazily behind an operation would otherwise never be closed
        again.
        """
        with self._lifecycle_lock:
            target = getattr(provider, "transport", provider)
            self._closed_targets.discard(id(target))

    def _ensure_provider_connected(self, provider):
        # The check and the append must be one atomic step; see the lock's
        # rationale in __init__.  Membership is tested by identity because a
        # provider is a live object, not a value.
        with self._lifecycle_lock:
            if any(item is provider for item in self._connected_providers):
                # A kept provider after `replace_providers()` is already
                # connected but belongs to a new generation.
                self._run_initializer_locked([provider])
                return
            connect = getattr(provider, "connect", None)
            if connect is not None:
                connect()
            target = getattr(provider, "transport", provider)
            self._closed_targets.discard(id(target))
            self._connected_providers.append(provider)
            self._run_initializer_locked([provider])

    def _select_capable(self, capability: str, operation: str):
        """Select an executor provider offering `capability`.

        An unsupported operation is `NotImplementedError`, the documented
        contract `run()` and `path()` also follow; `select()` alone would
        report it as `OperationNotStartedError`.
        """
        try:
            return self._executor_selector.select(capability=capability).provider
        except OperationNotStartedError as exc:
            raise NotImplementedError(
                f"{type(self).__name__} does not provide the {operation!r} capability"
            ) from exc

    def spawn(self, *cmds, **options):
        # Filtered on the capability, as runspace() already was: selecting
        # blind meant a first provider without sessions ended the search, so a
        # WindowsHost composed as (winrm, ssh) could not open an SSH session
        # at all.
        provider = self._select_capable("spawn", "spawn")
        self._ensure_provider_connected(provider)
        spawn = getattr(provider, "spawn", None)
        if spawn is None:
            raise NotImplementedError(
                f"executor provider {provider.name!r} does not support sessions"
            )
        return spawn(*cmds, **options)

    def runspace(self):
        """Return a provider-owned typed runspace when one is available."""
        provider = self._select_capable("runspace", "runspace")
        self._ensure_provider_connected(provider)
        method = getattr(provider, "runspace", None)
        if method is None:
            raise NotImplementedError(
                f"executor provider {provider.name!r} does not support runspaces"
            )
        return method()
