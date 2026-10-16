"""Small, transport-independent provider contracts.

Providers are deliberately adapters: they own connection-specific behaviour,
while :mod:`hostctl.system` owns operating-system semantics and fallback
safety."""

from __future__ import annotations

import dataclasses
import inspect
import logging
import typing

from pathapi import Path


from ..executor import dispatch_display

#: Selection, probe, and decline diagnostics.  Library convention: this module
#: never configures a handler and never calls ``basicConfig``; an application
#: opts in with ``logging.getLogger("hostctl").setLevel(logging.DEBUG)``.
log = logging.getLogger("hostctl.provider")


@dataclasses.dataclass(frozen=True)
class ProviderProbe:
    availability: typing.Literal["available", "unavailable", "degraded"]
    reason: str = ""
    capabilities: frozenset[str] = frozenset()
    system_hint: typing.Optional[str] = None

    @property
    def usable(self) -> bool:
        return self.availability in ("available", "degraded")


@dataclasses.dataclass(frozen=True)
class ProviderSelection:
    provider: typing.Union["ExecutorProvider", "PathProvider"]
    trace: tuple[dict[str, object], ...]
    generation: int = 0
    pinned: bool = False


def _accepts_keyword(hook: typing.Callable[..., object], name: str) -> bool:
    """Whether `hook` declares `name`, or takes arbitrary keywords.

    A hook whose signature cannot be introspected (a builtin, a C callable)
    is given the benefit of the doubt: it then raises its own TypeError,
    which is no worse than calling it directly.
    """
    try:
        parameters = inspect.signature(hook).parameters
    except (TypeError, ValueError):
        return True
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return True
    parameter = parameters.get(name)
    return parameter is not None and parameter.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


@dataclasses.dataclass(frozen=True)
class SessionInitializer:
    """Optional post-connect bootstrap hook receiving the connected system host."""

    initialize: typing.Callable[..., object]
    timeout: typing.Optional[float] = None

    def __call__(self, host, **options):
        """Run the hook, passing `timeout` only to a hook that accepts it.

        The documented example,
        `SessionInitializer(lambda h: h.run("sudo -v"), timeout=10)`, takes
        no `timeout` keyword, and passing one would raise at `connect()`.

        The value is advisory: it is handed to a hook that declares it, and
        nothing here enforces a deadline. A hook that wants one applies it to
        the calls it makes (`host.run(..., timeout=...)`).
        """
        if self.timeout is not None and _accepts_keyword(self.initialize, "timeout"):
            options.setdefault("timeout", self.timeout)
        return self.initialize(host, **options)


class _LazyRedaction:
    """A log argument that redacts only if a handler actually formats it.

    ``log.debug("... %s", _LazyRedaction(cmd, args))`` costs one object
    allocation when nobody is listening; the regex work happens inside
    ``__str__``, which ``logging`` calls only while rendering a record it has
    decided to emit.
    """

    __slots__ = ("_command", "_args")

    def __init__(self, command, args=()):
        self._command = command
        self._args = tuple(args)

    def __str__(self) -> str:
        from ._selector import ProviderSelector

        parts = (self._command, *self._args)
        return ProviderSelector.redact(" ".join(str(part) for part in parts))

    __repr__ = __str__


class _LazyName:
    """One name, redacted only if a handler formats the record.

    `ProviderSelector.redact(name)` as a log argument runs the regexes
    whether or not anything is listening -- six times per `run()` with debug
    off, against a documented promise that "the redaction work never runs at
    all". Same shape as `_LazyRedaction`, for the scalar case.
    """

    __slots__ = ("_value",)

    def __init__(self, value):
        self._value = value

    def __str__(self) -> str:
        from ._selector import ProviderSelector

        return ProviderSelector.redact(self._value)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_LazyName({str(self)!r})"


class _Provider:
    """What an executor provider and a path provider share: a name, a declared
    set of capabilities, and a probe."""

    def __init__(self, name: str, *, capabilities, probe=None):
        if not name:
            raise ValueError("provider name must not be empty")
        self.name = name
        self.capabilities = frozenset(
            getattr(item, "value", item) for item in capabilities
        )
        self._probe = probe

    def probe(self) -> ProviderProbe:
        if self._probe is None:
            return ProviderProbe("available", capabilities=self.capabilities)
        result = self._probe()
        if isinstance(result, ProviderProbe):
            return dataclasses.replace(
                result,
                capabilities=frozenset(
                    getattr(item, "value", item) for item in result.capabilities
                ),
            )
        return ProviderProbe(
            "available" if result else "unavailable", capabilities=self.capabilities
        )


class ExecutorProvider(_Provider):
    """Named callable executor with a conservative probe hook."""

    def __init__(
        self,
        name: str,
        executor: typing.Callable[..., object],
        *,
        capabilities=None,
        probe=None,
    ):
        self.executor = executor
        super().__init__(
            name,
            capabilities=(
                getattr(executor, "executor_capabilities", ())
                if capabilities is None
                else capabilities
            ),
            probe=probe,
        )

    def execute(self, command, *args, **options):
        # The rendered command routinely carries credentials, so it is redacted
        # on the way to the record and never interpolated eagerly: with no
        # handler listening, `%`-style args mean the redaction never runs.
        # A dispatch whose environment is embedded in the command text carries
        # its masked rendering, which is what is logged.
        display = dispatch_display()
        log.debug(
            "provider %s dispatching: %s",
            _LazyName(self.name),
            (
                _LazyRedaction(command, args)
                if display is None
                else _LazyRedaction(display[0], display[1:])
            ),
        )
        return self.executor(command, *args, **options)

    __call__ = execute


class PathProvider(_Provider):
    """Named path factory.  ``path`` must return a pathapi.Path."""

    DEFAULT_CAPABILITIES = frozenset(
        (
            "stat",
            "scandir",
            "open",
            "open_read",
            "open_write",
            "read",
            "write",
            "exists",
            "is_file",
            "is_dir",
            "mkdir",
            "chmod",
            "unlink",
            "rmdir",
            "rename",
            # Both were missing, so a provider that enumerates its operations
            # -- the shipped LocalPathProvider among them -- reported "no path
            # provider supports symlink_to" however capable its backend was.
            # SFTP and WinRM escaped only by declaring the `path` wildcard.
            "symlink_to",
            "readlink",
            "utime",
            "hardlink_to",
        )
    )

    def __init__(
        self,
        name: str,
        factory: typing.Callable[..., Path],
        *,
        capabilities=None,
        probe=None,
    ):
        self.factory = factory
        super().__init__(
            name,
            capabilities=(
                self.DEFAULT_CAPABILITIES if capabilities is None else capabilities
            ),
            probe=probe,
        )

    def path(self, *segments):
        log.debug(
            "path provider %s building: %s",
            _LazyName(self.name),
            _LazyRedaction("", segments),
        )
        value = self.factory(*segments)
        if not isinstance(value, Path):
            raise TypeError(
                f"path provider {self.name!r} returned {type(value).__name__}, expected pathapi.Path"
            )
        return value


#: Operations a POSIX-complete filesystem backend performs.  Intentionally the
#: same set as :attr:`PathProvider.DEFAULT_CAPABILITIES`: "everything a path
#: backend can do" is one answer, and keeping it one object stops the two from
#: drifting apart the next time an operation is added.
FULL_PATH_OPERATIONS = PathProvider.DEFAULT_CAPABILITIES


#: Content reads only, for a read-only endpoint such as an application
#: download. Mutations are absent, so a composite path rejects them rather than
#: routing them to another provider that might apply them to different state.
#: `is_file` and `is_dir` derive from `stat`, which these answer; leaving them
#: out broke every pathapi helper that asks `is_dir()` before doing anything.
READ_ONLY_PATH_OPERATIONS = frozenset(
    ("stat", "read", "open", "open_read", "exists", "is_file", "is_dir")
)
