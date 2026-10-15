"""An asyncssh server in a thread, for tests that need a real SSH peer."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import threading
import typing

import asyncssh

from hostctl import SshConfig


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
    directory; `sftp_factory` overrides how the SFTP server is built.
    """

    def __init__(
        self,
        handler: typing.Callable[..., typing.Any],
        *,
        sftp_root: typing.Optional[typing.Union[str, os.PathLike]] = None,
        sftp_factory: typing.Optional[typing.Callable[..., typing.Any]] = None,
    ) -> None:
        self.handler = handler
        self.events: list = []
        self._sftp_root = sftp_root
        self._sftp_factory = sftp_factory
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
            sftp_factory = lambda chan: asyncssh.SFTPServer(chan, chroot=root)

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

    def config(self, **overrides: typing.Any) -> SshConfig:
        """An `SshConfig` for this server; keywords override its defaults."""
        options: typing.Dict[str, typing.Any] = dict(
            host="127.0.0.1",
            port=self.port,
            username="tester",
            client_keys=self.client_key_path,
            known_hosts=self.known_hosts_path,
        )
        options.update(overrides)
        return SshConfig(**options)

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
