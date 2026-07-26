"""Small, transport-independent provider and selection contracts.

Providers are deliberately adapters: they own connection-specific behaviour,
while :mod:`hostctl.host.system` owns operating-system semantics and fallback
safety.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import typing

from pathlib_next import Path

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
        parts = (self._command, *self._args)
        return ProviderSelector.redact(" ".join(str(part) for part in parts))

    __repr__ = __str__


def _redacted_command(command, args=()) -> _LazyRedaction:
    """Wrap a command and its argv for redacted, deferred log formatting."""
    return _LazyRedaction(command, args)


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
        # The rendered command routinely carries credentials, so it is redacted
        # on the way to the record and never interpolated eagerly: with no
        # handler listening, `%`-style args mean the redaction never runs.
        log.debug(
            "provider %s dispatching: %s",
            ProviderSelector.redact(self.name),
            _redacted_command(command, args),
        )
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
        log.debug(
            "path provider %s building: %s",
            ProviderSelector.redact(self.name),
            _redacted_command("", segments),
        )
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
        # Trace entries accumulated across the failover attempts of one
        # caller-level operation, keyed by provider name so a later, more
        # informative record (a decline) supersedes the earlier optimistic one
        # (chosen).  See `select` for how an attempt sequence is delimited.
        self._attempt_trace: dict[str, dict[str, object]] = {}

    @property
    def generation(self) -> int:
        """The current connection generation; probes are cached per value."""
        return self._generation

    @property
    def declines(self) -> dict[str, str]:
        """Providers that refused before dispatch this generation, by name.

        Maps provider name to the redacted refusal reason.  A copy: mutating
        the result does not change selector state.
        """
        return {
            name: self._safe_name(reason) for name, reason in self._declined.items()
        }

    def decline(self, name: str, reason: str = "declined before dispatch") -> None:
        """Record that a provider refused *before* dispatch this generation.

        A provider which raised ``OperationNotStarted`` proved that nothing
        started, so it is safe to skip it for the rest of this generation
        rather than re-attempting it on every later operation.  The record is
        cleared by :meth:`invalidate` along with the probe cache.
        """
        self._declined[str(name)] = str(reason)
        log.debug(
            "provider %s declined before dispatch (generation %d): %s",
            self._safe_name(name),
            self._generation,
            self._safe_name(reason),
        )

    #: ``name=value`` and ``name: value`` credential assignments.
    _SECRET_ASSIGNMENT = re.compile(
        r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|key|credential)"
        r"\s*[=:]\s*(\"[^\"]*\"|'[^']*'|[^&\s,;]+)"
    )
    #: ``scheme://user:password@host`` URI userinfo.
    _SECRET_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@]+):[^/\s@]+@")

    @classmethod
    def _safe_name(cls, value: object) -> str:
        text = str(value)
        text = cls._SECRET_ASSIGNMENT.sub(r"\1=<redacted>", text)
        return cls._SECRET_USERINFO.sub(r"\1:<redacted>@", text)

    @classmethod
    def redact(cls, value: object) -> str:
        """Return a diagnostic string with credential-like values removed.

        Best effort, and deliberately so.  This recognizes credentials that
        *announce themselves* -- a ``password=``/``token=``/``api_key=``
        assignment, or the userinfo half of a ``scheme://user:secret@host``
        URI.  A bare positional secret (``mysql -p hunter2``, an argv element
        that is only a token) carries no marker and cannot be detected by
        inspection.  Treat debug output as sensitive.
        """
        return cls._safe_name(value)

    def probe(self, provider) -> ProviderProbe:
        """Return a provider probe cached for this selector generation.

        A provider that declined before dispatch this generation reports as
        ``unavailable`` with the refusal reason, overriding the cached probe.
        The probe describes whether the transport *could* work; the decline is
        the newer and stronger evidence that right now it does not, so any
        caller reading availability (``provider_details``, ``capabilities``)
        sees the refusal instead of a stale ``available``.
        """
        name = provider.name
        declined = self._declined.get(name)
        if declined is not None:
            return ProviderProbe("unavailable", self._safe_name(declined))
        if name in self._probe_cache:
            return self._probe_cache[name]
        try:
            result = provider.probe()
        except Exception as exc:
            result = ProviderProbe("unavailable", type(exc).__name__)
            log.debug(
                "provider %s probe raised %s; treating as unavailable",
                self._safe_name(name),
                type(exc).__name__,
            )
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
        # An empty `exclude` starts a new caller-level operation; a non-empty
        # one is a failover step within the operation that is already running,
        # because the only way a name lands in `exclude` is that this same
        # caller just tried it (`run()`, `_providers_in_order`).  Accumulating
        # across those steps is what makes the FIRST failing call report every
        # provider tried and every refusal, instead of the trace describing
        # only whichever provider happened to win.
        if not excluded:
            self._attempt_trace = {}
        for provider in self.providers:
            declined = self._declined.get(provider.name)
            if declined is not None:
                # Recorded even when the caller also excluded this provider:
                # `exclude` says "I already tried it", and the decline is the
                # reason it did not work.  Skipping the record here is exactly
                # what used to strip the refusal from the trace of the call
                # that suffered it.
                self._record(
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
            if provider.name in excluded:
                continue
            probe = self.probe(provider)
            allowed = probe.usable and (
                capability is None
                or capability in (probe.capabilities | provider.capabilities)
            )
            capabilities = frozenset(
                getattr(item, "value", item) for item in probe.capabilities
            )
            self._record(
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
            if not allowed:
                log.debug(
                    "provider %s skipped: availability=%s capability=%s reason=%s",
                    self._safe_name(provider.name),
                    probe.availability,
                    capability or "<any>",
                    self._safe_name(probe.reason) or "<none>",
                )
                continue
            result = ProviderSelection(
                provider,
                self.trace,
                generation=self._generation,
                policy=policy,
                pinned=bool(pin),
            )
            self.last_selection = result
            log.debug(
                "selected provider %s (generation %d, policy %s, pin %s) "
                "after %d candidate(s)",
                self._safe_name(provider.name),
                self._generation,
                self._safe_name(policy),
                bool(pin),
                len(self._attempt_trace),
            )
            return result
        log.debug(
            "no provider available (generation %d); candidates: %s",
            self._generation,
            ", ".join(
                "%s/%s" % (item["provider"], item["availability"])
                for item in self.trace
            )
            or "<none>",
        )
        raise OperationNotStarted("no provider is available")

    def _record(self, entry: dict[str, object]) -> None:
        """Merge one trace entry, letting a later record supersede an earlier.

        Ordering follows first appearance, so the trace still reads in
        provider precedence order after a decline rewrites an entry.
        """
        name = typing.cast(str, entry["provider"])
        if name in self._attempt_trace:
            self._attempt_trace[name].update(entry)
        else:
            self._attempt_trace[name] = dict(entry)

    @property
    def trace(self) -> tuple[dict[str, object], ...]:
        """The accumulated trace for the operation currently being selected."""
        return tuple(dict(entry) for entry in self._attempt_trace.values())

    def invalidate(self) -> None:
        """Start a new generation, dropping cached probes and declines."""
        self.last_selection = None
        self._probe_cache.clear()
        self._declined.clear()
        self._attempt_trace = {}
        self._generation += 1
        log.debug("selector invalidated; generation is now %d", self._generation)
