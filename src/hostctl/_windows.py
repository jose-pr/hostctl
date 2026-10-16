"""Windows process-creation details shared by the transports that start one."""

from __future__ import annotations

import subprocess
import sys


def no_window_flags() -> int:
    """`CREATE_NO_WINDOW` when this process has no console, else 0.

    Windows gives a console program started by a process without a console
    (a GUI app, `pythonw`, a service, an editor's background task) a new
    console window of its own; with the output captured it only flashes.
    """
    # `sys.platform`, not `os.name`: tests that emulate a Windows host patch
    # `os.name`, and the console APIs exist only on a real one.
    if sys.platform != "win32":
        return 0
    import ctypes

    if ctypes.windll.kernel32.GetConsoleWindow():
        return 0
    return subprocess.CREATE_NO_WINDOW
