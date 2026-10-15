"""Small, transport-independent provider and selection contracts.

Providers are deliberately adapters: they own connection-specific behaviour,
while :mod:`hostctl.host.system` owns operating-system semantics and fallback
safety.
"""

from __future__ import annotations

import dataclasses
import inspect
import logging
import re
import threading
import typing

from pathapi import Path

from ..executor._common import MASK, dispatch_display

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
        cause: typing.Optional[Exception] = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.cause = cause


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


class ProviderSelector:
    """Ordered selector which never retries a dispatched operation."""

    def __init__(self, providers=()):
        self.providers = tuple(providers)
        if any(not getattr(p, "name", "") for p in self.providers):
            raise ValueError("providers must have names")
        names = tuple(provider.name for provider in self.providers)
        if len(set(names)) != len(names):
            raise ValueError("provider names must be unique within a selector")
        self._probe_cache: dict[str, ProviderProbe] = {}
        self._declined: dict[str, str] = {}
        self._decline_causes: dict[str, BaseException] = {}
        self._generation = 0
        # One lock over the shared dictionaries. Two threads sharing a host
        # is ordinary, and the probe/decline maps were mutated while another
        # thread iterated them -- reproducibly enough to raise
        # `RuntimeError: dictionary changed size during iteration` under a
        # short switch interval.
        self._lock = threading.RLock()
        # Trace entries accumulated across the failover attempts of one
        # caller-level operation, keyed by provider name so a later, more
        # informative record (a decline) supersedes the earlier optimistic one
        # (chosen).  See `select` for how an attempt sequence is delimited.
        #
        # PER THREAD: an operation's trace describes that operation. Shared,
        # a concurrent call's records leaked into it, so `host.last_selection` and
        # `path.selection_trace` named providers the caller never tried.
        self._traces = threading.local()

    @property
    def _attempt_trace(self) -> "dict[str, dict[str, object]]":
        """This thread's accumulating trace for the operation it is running."""
        trace = getattr(self._traces, "entries", None)
        if trace is None:
            trace = {}
            self._traces.entries = trace
        return trace

    @_attempt_trace.setter
    def _attempt_trace(self, value: "dict[str, dict[str, object]]") -> None:
        self._traces.entries = value

    @property
    def generation(self) -> int:
        """The current connection generation; probes are cached per value."""
        return self._generation

    def retry_declined(self) -> None:
        """Forget every refusal recorded this generation.

        Not needed to recover a host: `select()` already re-admits an earlier
        refusal when nothing else can serve. This is for a caller that knows
        the declined provider is back (sshd restarted) and wants it preferred
        over a fallback that is still serving.
        """
        with self._lock:
            self._declined.clear()
            self._decline_causes.clear()

    @property
    def declines(self) -> dict[str, str]:
        """Providers that refused before dispatch this generation, by name.

        Maps provider name to the redacted refusal reason.  A copy: mutating
        the result does not change selector state.
        """
        with self._lock:
            declined = dict(self._declined)
        return {name: self.redact(reason) for name, reason in declined.items()}

    def decline(
        self,
        name: str,
        reason: str = "declined before dispatch",
        *,
        cause: typing.Optional[BaseException] = None,
    ) -> None:
        """Record that a provider refused *before* dispatch this generation.

        A provider which raised ``OperationNotStarted`` proved that nothing
        started, so it is safe to skip it for the rest of this generation
        rather than re-attempting it on every later operation.  The record is
        cleared by :meth:`invalidate` along with the probe cache.
        """
        with self._lock:
            self._declined[str(name)] = str(reason)
            if cause is not None:
                self._decline_causes[str(name)] = cause
        log.debug(
            "provider %s declined before dispatch (generation %d): %s",
            _LazyName(name),
            self._generation,
            _LazyName(reason),
        )

    #: ``name=value`` and ``name: value`` credential assignments.
    #:
    #: The keyword may carry any prefix, because a `\b` anchor cannot help
    #: here: `_` is a word character, so `\bpassword` never matched inside
    #: `DB_PASSWORD`, and every realistic environment-variable spelling
    #: (`DB_PASSWORD=`, `MYSQL_PWD=`, `GITHUB_TOKEN=`,
    #: `AWS_SECRET_ACCESS_KEY=`, `$env:DB_PASSWORD='...'`) went to the log
    #: verbatim while the bare `PASSWORD=` form was redacted.
    #: A prefix must end at a separator, so `monkey=banana` is not a secret
    #: while `DB_PASSWORD=` and `$env:API_KEY=` are.
    #: A value is one or more adjacent quoted runs (a shell splits a value that
    #: holds `'` into `'a'"'"'b'`) or an unquoted run.  An unquoted run stops at
    #: `&`, `;`, `,` and whitespace, so a value holding them after cmd's
    #: unquoted `set NAME=value` is only partly masked: best effort.
    _SECRET_KEYWORD = r"(?:password|passwd|pwd|secret|token|api[_-]?key|key|credential)"
    _SECRET_ASSIGNMENT = re.compile(
        r"(?i)(?<![A-Za-z0-9])"
        r"((?:[A-Za-z0-9_.$:\-]*[_.$:\-])?" + _SECRET_KEYWORD + r"[A-Za-z0-9_.\-]*)"
        r"\s*[=:]\s*((?:\"[^\"]*\"|'[^']*')+|[^&\s,;]+)"
    )
    #: fish exports with `set -gx NAME value`, no `=`.
    _SECRET_FISH_SET = re.compile(
        r"(?i)(?<![A-Za-z0-9])(set(?:\s+-[A-Za-z]+)*\s+)"
        r"((?:[A-Za-z0-9_]*[_])?" + _SECRET_KEYWORD + r"[A-Za-z0-9_]*)"
        r"\s+((?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')+|[^&\s,;]+)"
    )
    #: ``scheme://user:password@host`` URI userinfo.
    _SECRET_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@]+):[^/\s@]+@")

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
        text = cls._SECRET_ASSIGNMENT.sub(rf"\1={MASK}", str(value))
        text = cls._SECRET_FISH_SET.sub(rf"\1\2 {MASK}", text)
        return cls._SECRET_USERINFO.sub(r"\1:<redacted>@", text)

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
            return ProviderProbe("unavailable", self.redact(declined))
        with self._lock:
            cached = self._probe_cache.get(name)
        if cached is not None:
            return cached
        # Not under the lock: a probe may be a network round trip.
        try:
            result = provider.probe()
        except Exception as exc:
            result = ProviderProbe("unavailable", type(exc).__name__)
            log.debug(
                "provider %s probe raised %s; treating as unavailable",
                _LazyName(name),
                type(exc).__name__,
            )
        with self._lock:
            self._probe_cache[name] = result
        return result

    def effective_capabilities(self, provider) -> frozenset:
        """What `provider` can do now: its declared set, narrowed by its probe.

        A probe may narrow the declared capabilities and must not invent any,
        so the answer is the intersection when the (cached) probe states a set,
        and the declared set when it states none. A provider declaring the
        `path` wildcard declares everything, so what its probe states stands.
        """
        return self._effective(provider, self.probe(provider))

    @staticmethod
    def _effective(provider, probe: ProviderProbe) -> frozenset:
        declared = provider.capabilities
        stated = probe.capabilities
        if not stated:
            return declared
        if "path" in declared:
            return stated
        return declared & stated

    def select(
        self,
        *,
        capability: typing.Optional[str] = None,
        exclude: typing.Iterable[str] = (),
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
            # A single dict lookup is atomic, so the loop reads without the lock.
            declined = self._declined.get(provider.name)
            if declined is not None:
                # Recorded even when the caller also excluded this provider:
                # `exclude` says "I already tried it", and the decline is the
                # reason it did not work, which that call's trace must keep.
                self._record(
                    {
                        "provider": self.redact(provider.name),
                        "availability": "unavailable",
                        "reason": self.redact(declined),
                        "capabilities": (),
                        "chosen": False,
                        "generation": self._generation,
                        "policy": "ordered",
                        "pin": bool(pin),
                    }
                )
                continue
            if provider.name in excluded:
                continue
            probe = self.probe(provider)
            allowed = probe.usable and (
                capability is None or capability in self._effective(provider, probe)
            )
            capabilities = frozenset(
                getattr(item, "value", item) for item in probe.capabilities
            )
            self._record(
                {
                    "provider": self.redact(provider.name),
                    "availability": probe.availability,
                    "reason": self.redact(probe.reason),
                    "capabilities": tuple(sorted(capabilities)),
                    "chosen": allowed,
                    "generation": self._generation,
                    "policy": "ordered",
                    "pin": bool(pin),
                }
            )
            if not allowed:
                log.debug(
                    "provider %s skipped: availability=%s capability=%s reason=%s",
                    _LazyName(provider.name),
                    probe.availability,
                    capability or "<any>",
                    _LazyName(probe.reason or "<none>"),
                )
                continue
            result = ProviderSelection(
                provider,
                self.trace,
                generation=self._generation,
                pinned=bool(pin),
            )
            log.debug(
                "selected provider %s (generation %d, pin %s) " "after %d candidate(s)",
                _LazyName(provider.name),
                self._generation,
                bool(pin),
                len(self._attempt_trace),
            )
            return result
        if self._readmit_earlier_declines(excluded, capability):
            # Nothing else can serve, so a refusal recorded by an EARLIER
            # operation gets another chance rather than failing this one
            # without dialling: one transient refusal (sshd restarting) on a
            # single-provider host otherwise bricked it until close(). A
            # decline this operation made itself is in `excluded` and stays.
            return self.select(capability=capability, exclude=excluded, pin=pin)
        log.debug(
            "no provider available (generation %d); candidates: %s",
            self._generation,
            ", ".join(
                "%s/%s" % (item["provider"], item["availability"])
                for item in self.trace
            )
            or "<none>",
        )
        # Name why: the real refusal -- a rejected host key, a refused
        # connection -- rides as `__cause__` and `.cause`, not only at DEBUG.
        with self._lock:
            causes = dict(self._decline_causes)
            declined_names = list(self._declined)
        detail = "; ".join(
            f"{name}: {reason}" for name, reason in self.declines.items()
        )
        cause = next((causes.get(name) for name in reversed(declined_names)), None)
        raise OperationNotStarted(
            "no provider is available" + (f" ({detail})" if detail else ""),
            cause=cause,
        ) from cause

    def _readmit_earlier_declines(
        self, excluded: typing.AbstractSet[str], capability: str | None
    ) -> bool:
        """Forget declines that the running operation did not make.

        A name the caller excluded is one it tried and saw refuse, so only
        declines outside `excluded` came from an earlier operation.
        """
        with self._lock:
            stale = [
                provider.name
                for provider in self.providers
                if provider.name in self._declined
                and provider.name not in excluded
                and (capability is None or capability in provider.capabilities)
            ]
            for name in stale:
                del self._declined[name]
                self._decline_causes.pop(name, None)
        for name in stale:
            log.debug(
                "provider %s re-admitted: every other candidate is unavailable",
                _LazyName(name),
            )
        return bool(stale)

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
        with self._lock:
            self._probe_cache.clear()
            self._declined.clear()
            self._decline_causes.clear()
            self._generation += 1
        self._attempt_trace = {}
        log.debug("selector invalidated; generation is now %d", self._generation)
