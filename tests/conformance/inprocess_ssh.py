"""An asyncssh server in a thread, for tests that need a real SSH peer."""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import typing

import asyncssh

from hostctl.transports.ssh._config import SSHConfig


class _Server(asyncssh.SSHServer):
    def __init__(self, events: list) -> None:
        self._events = events

    def begin_auth(self, username: str) -> bool:
        self._events.append(("begin_auth", username))
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        return True


class InProcessSSHServer:
    """Serve `handler(process)` on 127.0.0.1 with host-key verification on.

    `events` is a list the handler may append to; the server records each
    `begin_auth` there too. `sftp_root` enables SFTP chrooted to that
    directory; `sftp_factory` overrides how the SFTP server is built, and
    `home` makes `realpath(".")` answer that path (recorded in `events`).
    """

    def __init__(
        self,
        handler: typing.Callable[..., typing.Any],
        *,
        sftp_root: typing.Optional[typing.Union[str, os.PathLike]] = None,
        sftp_factory: typing.Optional[typing.Callable[..., typing.Any]] = None,
        home: typing.Optional[str] = None,
    ) -> None:
        self.handler = handler
        self.events: list = []
        self._sftp_root = sftp_root
        self._sftp_factory = sftp_factory
        self._home = home
        self.port = 0
        self._dir = ""

    def __enter__(self) -> "InProcessSSHServer":
        self._dir = tempfile.mkdtemp(prefix="hostctl-ssh-")
        host_key = asyncssh.generate_private_key("ssh-ed25519")
        client_key = asyncssh.generate_private_key("ssh-ed25519")
        self.client_key_path = os.path.join(self._dir, "id_test")
        client_key.write_private_key(self.client_key_path)
        self._host_key = host_key
        self._loop = (
            asyncio.SelectorEventLoop() if os.name == "nt" else asyncio.new_event_loop()
        )
        ready = threading.Event()
        failure: list = []

        sftp_factory = self._sftp_factory
        if sftp_factory is None and self._sftp_root is not None:
            root = os.fspath(self._sftp_root).encode()
            events = self.events
            home = self._home

            class _Sftp(asyncssh.SFTPServer):
                def realpath(self, path):
                    # The login directory: what `realpath(".")` answers.
                    if home is not None and path == b".":
                        events.append(("realpath", path))
                        return home.encode()
                    return super().realpath(path)

            sftp_factory = lambda chan: _Sftp(chan, chroot=root)

        async def start() -> None:
            try:
                options: typing.Dict[str, typing.Any] = {}
                if sftp_factory is not None:
                    options["sftp_factory"] = sftp_factory
                self._server = await asyncssh.create_server(
                    lambda: _Server(self.events),
                    "127.0.0.1",
                    0,
                    server_host_keys=[host_key],
                    process_factory=self.handler,
                    encoding=None,
                    allow_scp=False,
                    **options,
                )
                self.port = self._server.sockets[0].getsockname()[1]
            except BaseException as exc:  # noqa: BLE001 - reported to the starter
                failure.append(exc)
            finally:
                ready.set()

        def run() -> None:
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(start())
            if not failure:
                self._loop.run_forever()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        ready.wait(15)
        if failure:
            self._cleanup()
            raise failure[0]
        public = host_key.export_public_key("openssh").decode().split()[:2]
        self.known_hosts_path = os.path.join(self._dir, "known_hosts")
        with open(self.known_hosts_path, "w", newline="") as handle:
            handle.write(f"[127.0.0.1]:{self.port} {public[0]} {public[1]}\n")
        return self

    def config(self, **overrides: typing.Any) -> SSHConfig:
        """An `SSHConfig` for this server; keywords override its defaults."""
        options: typing.Dict[str, typing.Any] = dict(
            host="127.0.0.1",
            port=self.port,
            username="tester",
            client_keys=self.client_key_path,
            known_hosts=self.known_hosts_path,
        )
        options.update(overrides)
        return SSHConfig(**options)

    def __exit__(self, *exc: object) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        loop = getattr(self, "_loop", None)
        server = getattr(self, "_server", None)
        if loop is not None and server is not None and loop.is_running():

            async def stop() -> None:
                server.close()
                await server.wait_closed()
                pending = [
                    task
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task()
                ]
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

            try:
                asyncio.run_coroutine_threadsafe(stop(), loop).result(10)
            except Exception:
                pass
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread = getattr(self, "_thread", None)
        if thread is not None:
            thread.join(10)
        if loop is not None and not loop.is_running():
            loop.close()
        shutil.rmtree(self._dir, ignore_errors=True)


def _end_tree(child: subprocess.Popen) -> None:
    """End `child` and whatever it started, as a closed SSH session does."""
    if os.name == "nt":
        if child.poll() is not None:
            return
        # TerminateProcess ends cmd.exe alone; its children keep the pipes.
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(child.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        # The shell may fork the command; its children hold the pipes too.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        child.kill()
    except OSError:
        pass


async def login_shell_handler(process: typing.Any) -> None:
    """Run the delivered command line through this machine's login shell.

    Windows gets `cmd.exe /c` with the line verbatim (a string, so
    `CreateProcess` adds no quoting), as Windows OpenSSH's stock server does;
    POSIX gets `/bin/sh -c`. The server must run with `encoding=None`.
    """
    loop = asyncio.get_running_loop()
    command = process.command
    if os.name == "nt":
        argv: typing.Any = "cmd.exe /d /s /c " + command if command else "cmd.exe /d"
    else:
        argv = ["/bin/sh", "-c", command] if command else ["/bin/sh"]
    env = dict(os.environ)
    for key, value in (process.env or {}).items():
        key = key.decode() if isinstance(key, bytes) else key
        env[key] = value.decode() if isinstance(value, bytes) else value
    child = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=os.name != "nt",
    )

    def on_signal(name: str) -> None:
        if name in ("TERM", "KILL", "INT", "HUP"):
            _end_tree(child)

    process.signal_received = on_signal

    def pump(source: typing.Any, sink: typing.Any, done: asyncio.Future) -> None:
        try:
            while True:
                data = source.read1(65536)
                if not data:
                    break
                loop.call_soon_threadsafe(sink.write, data)
        except (OSError, ValueError):
            pass
        finally:
            source.close()
            loop.call_soon_threadsafe(done.set_result, None)

    drained = []
    for source, sink in (
        (child.stdout, process.stdout),
        (child.stderr, process.stderr),
    ):
        done = loop.create_future()
        threading.Thread(target=pump, args=(source, sink, done), daemon=True).start()
        drained.append(done)

    def feed(data: bytes) -> None:
        try:
            child.stdin.write(data)
            child.stdin.flush()
        except (OSError, ValueError):
            pass

    async def forward_stdin() -> None:
        try:
            while True:
                data = await process.stdin.read(65536)
                if not data:
                    break
                await loop.run_in_executor(None, feed, data)
        except (asyncssh.Error, OSError):
            pass
        finally:
            try:
                child.stdin.close()
            except OSError:
                pass

    feeder = loop.create_task(forward_stdin())
    closed = loop.create_task(process.wait_closed())
    output = asyncio.ensure_future(asyncio.gather(*drained))
    await asyncio.wait({output, closed}, return_when=asyncio.FIRST_COMPLETED)
    if not output.done():
        # The client went away first: end the command it abandoned.
        _end_tree(child)
        await output
    returncode = await loop.run_in_executor(None, child.wait)
    feeder.cancel()
    await asyncio.gather(feeder, return_exceptions=True)
    if not closed.done():
        process.exit(returncode if returncode >= 0 else 255)
    await asyncio.gather(closed, return_exceptions=True)


_shared: typing.Optional[InProcessSSHServer] = None
_shared_lock = threading.Lock()


def shared_login_shell_server() -> InProcessSSHServer:
    """One `login_shell_handler` server per test session, SFTP in a temp root.

    `sftp_root` on the returned server is that root; it and the server are
    removed at interpreter exit.
    """
    global _shared
    with _shared_lock:
        if _shared is None:
            root = tempfile.mkdtemp(prefix="hostctl-ssh-root-")
            server = InProcessSSHServer(login_shell_handler, sftp_root=root)
            server.__enter__()
            server.sftp_root = root

            def close() -> None:
                server.__exit__(None, None, None)
                shutil.rmtree(root, ignore_errors=True)

            atexit.register(close)
            _shared = server
        return _shared
