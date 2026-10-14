"""``hostctl shell``: an interactive session on the host."""

from __future__ import annotations

import sys
import threading
import time

from ._common import HostCommand, write


class Shell(HostCommand):
    """Open an interactive session on the host; each stdin line is one command."""

    _parsername_ = "shell"

    def __call__(self) -> int:
        with self.connect() as host:
            try:
                session = host.shell.session(terminal=True)
            except NotImplementedError:
                # A raw serial console cannot allocate a PTY, and a URI-built
                # serial host is always raw. A console without a terminal is
                # still a console.
                session = host.shell.session()
            stopped = threading.Event()
            reader = threading.Thread(
                target=self._pump,
                args=(session, stopped),
                name="hostctl-shell-output",
                daemon=True,
            )
            reader.start()
            try:
                for line in sys.stdin:
                    session.send(line.rstrip("\r\n"))
            except KeyboardInterrupt:
                return 130
            finally:
                try:
                    session.send_eof()
                except NotImplementedError:
                    pass
                stopped.set()
                session.close()
                reader.join(timeout=1)
        return 0

    @staticmethod
    def _pump(session, stopped: threading.Event) -> None:
        """Copy session output to stdout until the session ends."""
        try:
            while not stopped.is_set():
                data = session.read(65536)
                if data:
                    write(sys.stdout, data)
                    continue
                # An empty read on a serial line is an idle moment -- the read
                # timeout expired -- not EOF; the process says when it is over.
                if getattr(session, "returncode", 0) is not None:
                    return
                time.sleep(0.01)
        except (OSError, ValueError):
            if not stopped.is_set():
                raise
