"""Small, transport-independent provider and selection contracts.

Providers are deliberately adapters: they own connection-specific behaviour,
while :mod:`hostctl.host.system` owns operating-system semantics and fallback
safety.
"""

from __future__ import annotations

import dataclasses
import re
import typing

from pathlib_next import Path


@dataclasses.dataclass(frozen=True)
class ProviderProbe:
    availability: typing.Literal["available", "unavailable", "degraded"]
    reason: str = ""
    capabilities: frozenset[str] = frozenset()
    system_hint: typing.Optional[str] = None

    @property
    def usable(self) -> bool:
        return self.availability in ("available", "degraded")


class OperationNotStarted(RuntimeError):
    """A provider declined before a remote operation could start."""

    def __init__(
        self,
        reason: str = "operation was not started",
        *,
        cause: Exception | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.cause = cause


@dataclasses.dataclass(frozen=True)
class ProviderSelection:
    provider: typing.Any
    trace: tuple[dict[str, object], ...]


@dataclasses.dataclass(frozen=True)
class SessionInitializer:
    """Optional post-connect bootstrap hook for a persistent session."""

    initialize: typing.Callable[..., typing.Any]
    timeout: float | None = None

    def __call__(self, session, **options):
        if self.timeout is not None:
            options.setdefault("timeout", self.timeout)
        return self.initialize(session, **options)


class ExecutorProvider:
    """Named callable executor with a conservative probe hook."""

    def __init__(
        self,
        name: str,
        executor: typing.Callable[..., typing.Any],
        *,
        capabilities=None,
        probe=None,
    ):
        if not name:
            raise ValueError("provider name must not be empty")
        self.name = name
        self.executor = executor
        raw = (
            getattr(executor, "executor_capabilities", ())
            if capabilities is None
            else capabilities
        )
        self.capabilities = frozenset(getattr(item, "value", item) for item in raw)
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

    def execute(self, command, *args, **options):
        return self.executor(command, *args, **options)

    __call__ = execute


class PathProvider:
    """Named path factory.  ``path`` must return a pathlib_next.Path."""

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
        if not name:
            raise ValueError("provider name must not be empty")
        self.name = name
        self.factory = factory
        raw_capabilities = (
            self.DEFAULT_CAPABILITIES if capabilities is None else capabilities
        )
        self.capabilities = frozenset(
            getattr(item, "value", item) for item in raw_capabilities
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

    def path(self, *segments):
        value = self.factory(*segments)
        if not isinstance(value, Path):
            raise TypeError(
                f"path provider {self.name!r} returned {type(value).__name__}, expected pathlib_next.Path"
            )
        return value


class ProviderSelector:
    """Ordered selector which never retries a dispatched operation."""

    def __init__(self, providers=()):
        self.providers = tuple(providers)
        if any(not getattr(p, "name", "") for p in self.providers):
            raise ValueError("providers must have names")
        self.last_selection: ProviderSelection | None = None
        self._probe_cache: dict[str, ProviderProbe] = {}
        self._generation = 0

    @staticmethod
    def _safe_name(value: object) -> str:
        text = str(value)
        return re.sub(
            r"(?i)(password|secret|token|key)=([^&\s]+)", r"\1=<redacted>", text
        )

    def select(
        self, *, capability: str | None = None, exclude: typing.Iterable[str] = ()
    ) -> ProviderSelection:
        excluded = set(exclude)
        trace = []
        for provider in self.providers:
            if provider.name in excluded:
                continue
            if provider.name in self._probe_cache:
                probe = self._probe_cache[provider.name]
            else:
                try:
                    probe = provider.probe()
                except Exception as exc:
                    probe = ProviderProbe("unavailable", type(exc).__name__)
                self._probe_cache[provider.name] = probe
            allowed = probe.usable and (
                capability is None
                or capability in (probe.capabilities | provider.capabilities)
            )
            capabilities = frozenset(
                getattr(item, "value", item) for item in probe.capabilities
            )
            trace.append(
                {
                    "provider": self._safe_name(provider.name),
                    "availability": probe.availability,
                    "reason": self._safe_name(probe.reason),
                    "capabilities": tuple(sorted(capabilities)),
                    "chosen": allowed,
                }
            )
            if allowed:
                result = ProviderSelection(provider, tuple(trace))
                self.last_selection = result
                return result
        raise OperationNotStarted("no provider is available")

    def invalidate(self) -> None:
        self.last_selection = None
        self._probe_cache.clear()
        self._generation += 1
