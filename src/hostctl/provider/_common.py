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
    provider: typing.Union["ExecutorProvider", "PathProvider"]
    trace: tuple[dict[str, object], ...]
    generation: int = 0
    policy: str = "ordered"
    pinned: bool = False


@dataclasses.dataclass(frozen=True)
class SessionInitializer:
    """Optional post-connect bootstrap hook receiving the connected system host."""

    initialize: typing.Callable[..., object]
    timeout: float | None = None

    def __call__(self, host, **options):
        if self.timeout is not None:
            options.setdefault("timeout", self.timeout)
        return self.initialize(host, **options)


class ExecutorProvider:
    """Named callable executor with a conservative probe hook."""

    def __init__(
        self,
        name: str,
        executor: typing.Callable[..., object],
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
        names = tuple(provider.name for provider in self.providers)
        if len(set(names)) != len(names):
            raise ValueError("provider names must be unique within a selector")
        self.last_selection: ProviderSelection | None = None
        self._probe_cache: dict[str, ProviderProbe] = {}
        self._declined: dict[str, str] = {}
        self._generation = 0

    @property
    def generation(self) -> int:
        """The current connection generation; probes are cached per value."""
        return self._generation

    def decline(self, name: str, reason: str = "declined before dispatch") -> None:
        """Record that a provider refused *before* dispatch this generation.

        A provider which raised ``OperationNotStarted`` proved that nothing
        started, so it is safe to skip it for the rest of this generation
        rather than re-attempting it on every later operation.  The record is
        cleared by :meth:`invalidate` along with the probe cache.
        """
        self._declined[str(name)] = str(reason)

    @staticmethod
    def _safe_name(value: object) -> str:
        text = str(value)
        return re.sub(
            r"(?i)(password|secret|token|key)=([^&\s]+)", r"\1=<redacted>", text
        )

    @classmethod
    def redact(cls, value: object) -> str:
        """Return a diagnostic string with credential-like values removed."""
        return cls._safe_name(value)

    def probe(self, provider) -> ProviderProbe:
        """Return a provider probe cached for this selector generation."""
        name = provider.name
        if name in self._probe_cache:
            return self._probe_cache[name]
        try:
            result = provider.probe()
        except Exception as exc:
            result = ProviderProbe("unavailable", type(exc).__name__)
        self._probe_cache[name] = result
        return result

    def select(
        self,
        *,
        capability: str | None = None,
        exclude: typing.Iterable[str] = (),
        policy: str = "ordered",
        pin: bool = False,
    ) -> ProviderSelection:
        excluded = set(exclude)
        trace = []
        for provider in self.providers:
            if provider.name in excluded:
                continue
            declined = self._declined.get(provider.name)
            if declined is not None:
                trace.append(
                    {
                        "provider": self._safe_name(provider.name),
                        "availability": "unavailable",
                        "reason": self._safe_name(declined),
                        "capabilities": (),
                        "chosen": False,
                        "generation": self._generation,
                        "policy": self._safe_name(policy),
                        "pin": bool(pin),
                    }
                )
                continue
            probe = self.probe(provider)
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
                    "generation": self._generation,
                    "policy": self._safe_name(policy),
                    "pin": bool(pin),
                }
            )
            if allowed:
                result = ProviderSelection(
                    provider,
                    tuple(trace),
                    generation=self._generation,
                    policy=policy,
                    pinned=bool(pin),
                )
                self.last_selection = result
                return result
        raise OperationNotStarted("no provider is available")

    def invalidate(self) -> None:
        """Start a new generation, dropping cached probes and declines."""
        self.last_selection = None
        self._probe_cache.clear()
        self._declined.clear()
        self._generation += 1
