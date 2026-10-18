"""A POSIX host over a sandbox directory, for the deploy tests.

`sandbox_host()` is a real `PosixHost`: commands go through its executor
provider as `Exec` argv and paths through sftppath's real `SFTPPath` over the
conformance suite's `LocalSFTPBackend`. Both land in the same sandbox, so a
test asserts what the target looks like afterwards, not which commands went
out.

`SandboxPrograms` interprets the programs a deploy runs with GNU coreutils
9.x semantics, `mv` moving INTO an existing directory included. A deployed
zipapp or tree launcher is run with this interpreter, so a test can run what
it installed. Anything else exits 127, as a shell would.
"""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import typing
from pathlib import Path

from hostctl import ExecutorProvider, PathProvider, PosixHost
from hostctl.executor import BufferedExecutor, ExecutorCapability

Reply = typing.Tuple[bytes, bytes, int]


class SandboxPrograms(BufferedExecutor):
    """Run deploy's argv commands against `sandbox`."""

    executor_capabilities = frozenset((ExecutorCapability.ARGS,))

    def __init__(self, sandbox) -> None:
        self.sandbox = sandbox
        self.ran: typing.List[typing.Tuple[str, ...]] = []
        #: Lines the probe answers with; `None` runs the real probe here.
        self.probe_reply: typing.Optional[typing.List[str]] = None
        #: argv prefix -> reply, consulted first, for a failing step.
        self.fail: typing.Dict[typing.Tuple[str, ...], Reply] = {}

    def local(self, remote: str) -> Path:
        return self.sandbox.local(remote)

    def _exchange(self, request):
        argv = (request.command, *request.args)
        self.ran.append(argv)
        for prefix, reply in self.fail.items():
            if argv[: len(prefix)] == prefix:
                return reply
        program = posixpath.basename(argv[0])
        handler = getattr(self, "_" + program, None)
        if handler is not None:
            return handler(list(argv[1:]))
        if program.startswith("python"):
            return self._python(list(argv[1:]), request.input)
        if "/bin/" in argv[0]:
            return self._launcher(argv[0], list(argv[1:]))
        return b"", f"sh: 1: {argv[0]}: not found\n".encode(), 127

    @staticmethod
    def _operands(argv):
        assert "--" in argv, f"path operands must follow `--`: {argv!r}"
        cut = argv.index("--")
        return argv[:cut], argv[cut + 1 :]

    def _mkdir(self, argv):
        options, (remote,) = self._operands(argv)
        assert "-p" in options
        self.local(remote).mkdir(parents=True, exist_ok=True)
        return b"", b"", 0

    def _rm(self, argv):
        options, operands = self._operands(argv)
        assert "-rf" in options
        for remote in operands:
            local = self.local(remote)
            if local.is_dir() and not local.is_symlink():
                shutil.rmtree(local)
            elif os.path.lexists(local):
                local.unlink()
        return b"", b"", 0

    def _mv(self, argv):
        _, (source, target) = self._operands(argv)
        src, dst = self.local(source), self.local(target)
        if not os.path.lexists(src):
            text = f"mv: cannot stat '{source}': No such file or directory\n"
            return b"", text.encode(), 1
        if dst.is_dir():
            dst = dst / src.name
        os.replace(src, dst)
        return b"", b"", 0

    def _chmod(self, argv):
        # BSD chmod ends its options at the mode, so `--` must precede it.
        options, (mode, remote) = self._operands(argv)
        assert not options, f"chmod's mode must follow `--`: {argv!r}"
        self.local(remote).chmod(int(mode, 8))
        return b"", b"", 0

    def _tar(self, argv):
        assert argv[0] == "-xf" and argv[2] == "-C"
        archive, directory = self.local(argv[1]), self.local(argv[3])
        strip = int(argv[4].partition("=")[2])
        if not directory.is_dir():
            text = f"tar: {argv[3]}: Cannot open: No such file or directory\n"
            return b"", text.encode(), 2
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                parts = member.name.split("/")[strip:]
                if not parts:
                    continue
                destination = directory.joinpath(*parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(tar.extractfile(member).read())
                    destination.chmod(member.mode)
        return b"", b"", 0

    def _python(self, argv, stdin):
        if argv == ["-"]:
            if self.probe_reply is not None:
                return ("\n".join(self.probe_reply) + "\n").encode(), b"", 0
            return self._spawn([sys.executable, "-"], stdin)
        script, *rest = argv
        return self._spawn([sys.executable, str(self.local(script)), *rest], None)

    def _launcher(self, program, argv):
        text = self.local(program).read_text()
        here = self.local(posixpath.dirname(posixpath.dirname(program)))
        roots = re.search(r'PYTHONPATH="(.*?)\$\{PYTHONPATH', text).group(1)
        package = re.search(r" -m (\S+) ", text).group(1)
        entries = [
            str(here / entry.replace("$here/", "", 1))
            for entry in roots.split(":")
            if entry
        ]
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(entries))
        return self._spawn([sys.executable, "-m", package, *argv], None, env)

    @staticmethod
    def _spawn(argv, stdin, env=None) -> Reply:
        done = subprocess.run(argv, input=stdin, capture_output=True, env=env)
        return done.stdout, done.stderr, done.returncode


#: `hcd-app` and the dependencies it declares, as installed distributions.
#: `hcd-absent` is declared and not installed; `hcd-lib` has extras of its own.
_DISTRIBUTIONS = {
    "hcd-app": (
        "1.0",
        [
            "hcd-dep",
            "hcd-lib[fast]",
            'hcd-ssh; extra == "ssh"',
            'hcd-win; sys_platform == "win32"',
            "hcd-absent",
        ],
    ),
    "hcd-dep": ("2.0", []),
    "hcd-lib": ("1.1", ['hcd-speed; extra == "fast"', 'hcd-paramiko; extra == "ssh"']),
    "hcd-speed": ("0.1", []),
    "hcd-ssh": ("0.1", []),
    "hcd-win": ("0.1", []),
    "hcd-paramiko": ("0.1", []),
}

_APP_MAIN = (
    "import sys\n"
    "from importlib.metadata import version\n"
    "print('hcd-app', version('hcd-app'), *sys.argv[1:])\n"
)


def make_site(root: Path) -> Path:
    """Install the `hcd-*` distributions into a fresh directory and return it.

    `hcd-app` is a package found through RECORD (whose console script line
    leaves site-packages), with a subpackage and stale caches; `hcd-dep` is
    a single module named by `top_level.txt`; the rest are packages.
    """
    site = root / "site"
    site.mkdir()
    for name, (version, requires) in _DISTRIBUTIONS.items():
        module = name.replace("-", "_")
        info = site / f"{module}-{version}.dist-info"
        info.mkdir()
        lines = [f"Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
        lines += [f"Requires-Dist: {requirement}" for requirement in requires]
        lines += ["Provides-Extra: fast", "Provides-Extra: ssh"]
        (info / "METADATA").write_text("\n".join(lines) + "\n")
        if name == "hcd-dep":
            (site / f"{module}.py").write_text("VALUE = 2\n")
            (info / "top_level.txt").write_text(module + "\n")
            continue
        package = site / module
        package.mkdir()
        (package / "__init__.py").write_text(f"NAME = {name!r}\n")
        record = [f"{module}/__init__.py,,", f"{info.name}/METADATA,,"]
        if name == "hcd-app":
            (package / "__main__.py").write_text(_APP_MAIN)
            (package / "cmd").mkdir()
            (package / "cmd" / "__init__.py").write_text("")
            (package / "cmd" / "call.py").write_text("CALL = 1\n")
            (package / "__pycache__").mkdir()
            (package / "__pycache__" / "x.cpython-39.pyc").write_bytes(b"\0")
            (package / "stray.pyc").write_bytes(b"\0")
            record += [
                f"{module}/__main__.py,,",
                f"{module}/cmd/__init__.py,,",
                "../../bin/hcd-app,,",
            ]
        (info / "RECORD").write_text("\n".join(record) + "\n")
    return site


def sandbox_host() -> typing.Tuple[PosixHost, SandboxPrograms]:
    """A POSIX host over a fresh sandbox, and its program interpreter.

    Needs sftppath (the `ssh` extra); call `host._sandbox_cleanup()` after
    `host.close()`.
    """
    from sftppath import SFTPPath

    from conformance.path_fakes import LocalSFTPBackend

    backend = LocalSFTPBackend()
    programs = SandboxPrograms(backend.client().sandbox)

    def factory(*segments):
        remote = posixpath.join("/", *[str(segment) for segment in segments])
        return SFTPPath(remote, backend=backend)

    host = PosixHost(
        executor_providers=(ExecutorProvider("sandbox", programs),),
        path_providers=(PathProvider("sftp", factory),),
    )
    host._sandbox_cleanup = backend.close
    return host, programs
