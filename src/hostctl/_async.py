"""Run an awaitable to completion from synchronous code.

``asyncssh`` is fully async, but :class:`~hostctl.host.Host` presents a
synchronous API, so its direct-asyncssh operations (opening the connection,
running a remote command) are driven to completion here. SFTP is NOT among
them: :meth:`~hostctl.host.Host.path` uses `pathlib_next`'s own ``SftpPath``
(with its own async bridge), not this one.

The bridge is a single **shared background event loop** running in a daemon
thread; each call submits its coroutine with ``run_coroutine_threadsafe`` and
blocks on the result. This works whether or not the *calling* thread already
has a running loop (the coroutine runs on the dedicated background loop, not
the caller's), so no ``nest_asyncio`` re-entrancy hack is needed -- and it
avoids the deprecated ``asyncio.get_event_loop()``/policy APIs (slated for
removal in 3.16). asyncssh objects created through this bridge are bound to
this loop, so all subsequent operations on them must also go through here.
"""

from __future__ import annotations

import asyncio as _asyncio
import os as _os
import subprocess as _subprocess
import sys as _sys
import threading as _threading
import typing as _ty

_T = _ty.TypeVar("_T")

#: No bridge-level timeout by default (block until the awaitable completes).
#: Callers that want a bound pass their own timeout= through to the
#: operation (e.g. asyncssh's own per-command timeout=, which Host.run forwards).
_DEFAULT_TIMEOUT = None

_loop: _ty.Optional[_asyncio.AbstractEventLoop] = None
_loop_pid: _ty.Optional[int] = None
_loop_lock = _threading.Lock()


def asyncssh():
    """Import ``asyncssh`` on demand (the ``ssh`` extra)."""
    try:
        import asyncssh
    except ImportError as exc:  # pragma: no cover - only without the extra
        raise ImportError(
            "SSH support requires the 'ssh' extra: pip install hostctl[ssh]"
        ) from exc
    return asyncssh


def normalize_asyncssh_error(
    exc: Exception,
    *,
    command: _ty.Optional[str] = None,
    timeout: _ty.Optional[float] = None,
) -> Exception:
    """Map AsyncSSH transport/process errors to hostctl's standard contract."""
    module = asyncssh()
    if isinstance(exc, (TimeoutError, module.TimeoutError)):
        if command is not None:
            return _subprocess.TimeoutExpired(
                command,
                timeout,
                output=getattr(exc, "stdout", None),
                stderr=getattr(exc, "stderr", None),
            )
        return exc
    if isinstance(exc, module.PermissionDenied):
        return PermissionError(str(exc))
    if isinstance(exc, module.ProcessError):
        return _subprocess.CalledProcessError(
            getattr(exc, "returncode", None),
            command or getattr(exc, "command", None),
            output=getattr(exc, "stdout", None),
            stderr=getattr(exc, "stderr", None),
        )
    connection_errors = (
        module.ChannelOpenError,
        module.DisconnectError,
        module.HostKeyNotVerifiable,
        module.KeyExchangeFailed,
        module.ProtocolError,
    )
    if isinstance(exc, connection_errors):
        return ConnectionError(str(exc))
    return exc


def _new_loop() -> _asyncio.AbstractEventLoop:
    if _sys.platform == "win32":
        # SelectorEventLoop, not the Windows-default Proactor loop: this
        # bridge only makes plain-TCP SSH client connections (no subprocess
        # pipes), and Proactor's pipe transports emit a benign-but-noisy
        # "Exception ignored in _ProactorBasePipeTransport.__del__" on GC.
        return _asyncio.SelectorEventLoop()
    return _asyncio.new_event_loop()


def _ensure_loop() -> _asyncio.AbstractEventLoop:
    global _loop, _loop_pid
    pid = _os.getpid()
    with _loop_lock:
        if _loop is not None and not _loop.is_closed() and _loop_pid == pid:
            return _loop
        # First call, or a fork()'d child that inherited a now-dead loop thread.
        loop = _new_loop()
        thread = _threading.Thread(
            target=loop.run_forever,
            name="hostctl-asyncssh-loop",
            daemon=True,
        )
        thread.start()
        _loop, _loop_pid = loop, pid
        return loop


def async_to_sync(
    awaitable: _ty.Awaitable[_T],
    *,
    timeout: _ty.Optional[float] = _DEFAULT_TIMEOUT,
) -> _T:
    """Drive ``awaitable`` to completion on the shared background loop.

    Safe to call from any thread and from inside an already-running event
    loop (the coroutine runs on the dedicated background loop, not the
    caller's). Blocks up to ``timeout`` seconds; ``None`` waits indefinitely.
    """
    loop = _ensure_loop()
    future = _asyncio.run_coroutine_threadsafe(_ensure_coro(awaitable), loop)
    return future.result(timeout)


def _ensure_coro(
    awaitable: _ty.Awaitable[_T],
) -> _ty.Coroutine[_ty.Any, _ty.Any, _T]:
    """Adapt any awaitable to a coroutine (``run_coroutine_threadsafe`` requires
    a genuine coroutine object; asyncssh's ``@async_context_manager`` awaitables
    and futures are not)."""
    if _asyncio.iscoroutine(awaitable):
        return awaitable

    async def _wrap() -> _T:
        return await awaitable

    return _wrap()
