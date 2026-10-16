import io
import shutil
import subprocess

import pytest
from pathapi.mempath import MemPath, MemPathBackend
from pathapi.sync import PathAndStat, PathSyncer

from hostctl.provider._contracts import ExecutorProvider, PathProvider
from hostctl.system._kinds import PosixHost, WindowsHost
from hostctl.sync import ProgressReader, host_checksum, stat_checksum


class CountingMemPath(MemPath):
    opens = 0

    def _open(self, *args, **kwargs):
        type(self).opens += 1
        return super()._open(*args, **kwargs)


def _memory_file(backend, name, content):
    path = MemPath(name, backend=backend)
    path.write_bytes(content)
    return path


def test_stat_checksum_uses_cached_stat_without_opening():
    backend = MemPathBackend()
    path = _memory_file(backend, "value.bin", b"value")
    entry = PathAndStat(path)

    assert stat_checksum(entry) == (5, entry.stat.st_mtime)


@pytest.mark.parametrize(
    ("host_type", "output", "command_fragment"),
    [
        (PosixHost, "a" * 32 + "  /srv/data\n", "md5sum"),
        (WindowsHost, "A" * 32 + "\r\n", "Get-FileHash"),
    ],
)
def test_host_checksum_uses_owned_provider_and_shell(
    host_type, output, command_fragment
):
    calls = []
    backend = MemPathBackend()

    def execute(command, *args, **options):
        calls.append((command, args, options))
        return subprocess.CompletedProcess((command, *args), 0, output, "")

    provider = PathProvider("memory", lambda *parts: MemPath(*parts, backend=backend))
    host = host_type(
        executor_providers=(ExecutorProvider("fake", execute),),
        path_providers=(provider,),
    )
    path = host.path("srv", "data")
    entry = PathAndStat.from_stat(path, object())

    assert host_checksum(host)(entry) == "a" * 32
    invocation = " ".join(
        (_decoded_powershell(calls[0][0]), *(str(arg) for arg in calls[0][1]))
    )
    assert command_fragment in invocation


def test_host_checksum_falls_back_to_streaming_for_an_unowned_path():
    owned_backend = MemPathBackend()
    external_backend = MemPathBackend()
    external = _memory_file(external_backend, "external", b"content")
    calls = []
    host = PosixHost(
        executor_providers=(
            ExecutorProvider("unused", lambda *args, **kwargs: calls.append(args)),
        ),
        path_providers=(
            PathProvider(
                "owned", lambda *parts: MemPath(*parts, backend=owned_backend)
            ),
        ),
    )

    digest = host_checksum(host)(PathAndStat(external))

    assert digest == "9a0364b9e99bb480dd25e1f0284c8555"
    assert calls == []


def test_host_checksum_surfaces_a_missing_file_after_falling_back():
    """Unparseable output falls back to reading; a missing file then reports.

    The fallback must not convert a real "this path does not exist" answer
    into a checksum or a confusing parse error.
    """
    backend = MemPathBackend()
    provider = PathProvider("memory", lambda *parts: MemPath(*parts, backend=backend))
    host = PosixHost(
        executor_providers=(
            ExecutorProvider(
                "fake",
                lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "no", ""),
            ),
        ),
        path_providers=(provider,),
    )

    with pytest.raises(FileNotFoundError):
        host_checksum(host)(PathAndStat.from_stat(host.path("data"), object()))


def test_digest_parsing_tolerates_blank_and_noisy_lines():
    """Real tools pad their output; a blank line must not crash the parser."""
    from hostctl.sync import _parse_digest

    md5sum = "\n\nd41d8cd98f00b204e9800998ecf8427e  /srv/some file.txt\n\n"
    assert _parse_digest(md5sum, "md5") == "d41d8cd98f00b204e9800998ecf8427e"

    certutil = (
        "MD5 hash of file data:\r\n\r\n"
        "d4 1d 8c d9 8f 00 b2 04 e9 80 09 98 ec f8 42 7e\r\n"
        "CertUtil: -hashfile command completed successfully.\r\n"
    )
    assert _parse_digest(certutil, "md5") == "d41d8cd98f00b204e9800998ecf8427e"

    get_filehash = "\r\nD41D8CD98F00B204E9800998ECF8427E\r\n"
    assert _parse_digest(get_filehash, "md5") == "d41d8cd98f00b204e9800998ecf8427e"

    with pytest.raises(ValueError, match="unable to parse"):
        _parse_digest("\n\n  \n", "md5")


def test_powershell_checksum_falls_back_to_certutil():
    backend = MemPathBackend()
    outputs = iter(
        (
            ("", "Get-FileHash unavailable"),
            (
                "MD5 hash of file data:\r\nAA AA AA AA AA AA AA AA "
                "AA AA AA AA AA AA AA AA\r\nCertUtil: command completed\r\n",
                "",
            ),
        )
    )
    calls = []

    def execute(command, *args, **options):
        calls.append((command, args))
        stdout, stderr = next(outputs)
        return subprocess.CompletedProcess((command, *args), 0, stdout, stderr)

    provider = PathProvider("memory", lambda *parts: MemPath(*parts, backend=backend))
    host = WindowsHost(
        executor_providers=(ExecutorProvider("fake", execute),),
        path_providers=(provider,),
    )

    result = host_checksum(host)(PathAndStat.from_stat(host.path("data"), object()))

    assert result == "a" * 32
    assert len(calls) == 2
    assert "certutil.exe" in _decoded_powershell(calls[1][0])


def test_host_checksum_degrades_to_reading_when_the_remote_tool_fails():
    """An owned path whose host cannot hash in place must still be checksummed.

    Remote hashing is an optimization. A host missing ``md5sum`` (or refusing
    the command) must not abort the whole sync.
    """
    backend = MemPathBackend()

    def execute(command, *args, **options):
        raise FileNotFoundError("md5sum")

    host = PosixHost(
        executor_providers=(ExecutorProvider("broken", execute),),
        path_providers=(
            PathProvider("memory", lambda *parts: MemPath(*parts, backend=backend)),
        ),
    )
    path = host.path("payload")
    path.write_bytes(b"content")

    assert host_checksum(host)(PathAndStat(path)) == (
        "9a0364b9e99bb480dd25e1f0284c8555"
    )


def test_host_checksum_degrades_when_the_remote_output_is_unparseable():
    """A host answering with garbage falls back rather than raising."""
    backend = MemPathBackend()
    host = PosixHost(
        executor_providers=(
            ExecutorProvider(
                "noisy",
                lambda *args, **kwargs: subprocess.CompletedProcess(
                    args, 0, "command not found\n", ""
                ),
            ),
        ),
        path_providers=(
            PathProvider("memory", lambda *parts: MemPath(*parts, backend=backend)),
        ),
    )
    path = host.path("payload")
    path.write_bytes(b"content")

    assert host_checksum(host)(PathAndStat(path)) == (
        "9a0364b9e99bb480dd25e1f0284c8555"
    )


def test_remote_to_remote_sync_uses_both_hosts_without_content_reads():
    CountingMemPath.opens = 0
    source_backend = MemPathBackend()
    target_backend = MemPathBackend()
    _memory_file(source_backend, "same", b"same")
    _memory_file(target_backend, "same", b"same")

    def make_host(name, backend):
        return PosixHost(
            executor_providers=(
                ExecutorProvider(
                    name,
                    lambda *args, **kwargs: subprocess.CompletedProcess(
                        args, 0, "51037a4a37730f52c8732586d3aaa316  same\n", ""
                    ),
                ),
            ),
            path_providers=(
                PathProvider(
                    name,
                    lambda *parts: CountingMemPath(*parts, backend=backend),
                ),
            ),
        )

    source_host = make_host("source", source_backend)
    target_host = make_host("target", target_backend)
    PathSyncer(host_checksum(source_host, target_host)).sync(
        source_host.path("same"), target_host.path("same")
    )

    assert CountingMemPath.opens == 0


def test_path_syncer_stat_checksum_copies_changed_files_and_honors_dry_run():
    source_backend = MemPathBackend()
    target_backend = MemPathBackend()
    source = _memory_file(source_backend, "value", b"new value")
    target = _memory_file(target_backend, "value", b"old")
    syncer = PathSyncer(stat_checksum)

    syncer.sync(source, target, dry_run=True)
    assert target.read_bytes() == b"old"

    syncer.sync(source, target)
    assert target.read_bytes() == b"new value"


def test_progress_reader_reports_copy_progress():
    events = []
    destination = io.BytesIO()

    with ProgressReader(
        io.BytesIO(b"abcdef"),
        lambda current, total: events.append((current, total)),
        total=6,
    ) as source:
        shutil.copyfileobj(source, destination, length=2)

    assert destination.getvalue() == b"abcdef"
    assert events[-1] == (6, 6)


def _memory_host(name, backend):
    """A host whose only path provider reads and writes `backend`."""
    return PosixHost(
        executor_providers=(
            ExecutorProvider(
                name,
                lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
            ),
        ),
        path_providers=(
            PathProvider(
                name,
                lambda *parts: MemPath(*parts, backend=backend),
                capabilities=PathProvider.DEFAULT_CAPABILITIES | {"open_read"},
            ),
        ),
    )


def test_two_hosts_are_two_filesystems_even_when_paths_are_spelled_alike():
    """`/app.conf` on one host is not `/app.conf` on another.

    pathapi decides "same file" through `_same_filesystem()`; without an
    answer it falls back to `(type, segments)` equality and refuses the copy
    with `EINVAL "Source and target are the same file"`.
    """
    source_backend, target_backend = MemPathBackend(), MemPathBackend()
    _memory_file(source_backend, "app.conf", b"new config")
    _memory_file(target_backend, "app.conf", b"old config")
    source = _memory_host("source", source_backend).path("app.conf")
    target = _memory_host("target", target_backend).path("app.conf")

    assert not source._same_filesystem(target)
    source.copy(target, overwrite=True)

    assert target.read_bytes() == b"new config"


def test_one_host_is_one_filesystem_so_copying_a_file_onto_itself_is_refused():
    """The other direction: the guard that protects a file from itself."""
    backend = MemPathBackend()
    _memory_file(backend, "app.conf", b"payload")
    host = _memory_host("host", backend)
    path, same_path = host.path("app.conf"), host.path("app.conf")

    assert path._same_filesystem(same_path)
    with pytest.raises(OSError, match="same file"):
        path.copy(same_path, overwrite=True)
    with pytest.raises(ValueError, match="overlap"):
        PathSyncer(stat_checksum).sync(path, same_path)

    assert path.read_bytes() == b"payload"


def test_a_via_pin_does_not_change_which_filesystem_a_path_is_on():
    """`.via()` selects a route to the host, not a different host."""
    backend = MemPathBackend()
    host = PosixHost(
        path_providers=(
            PathProvider("first", lambda *parts: MemPath(*parts, backend=backend)),
            PathProvider("second", lambda *parts: MemPath(*parts, backend=backend)),
        )
    )
    path = host.path("app.conf")

    assert path._same_filesystem(path.via("second"))


def test_supported_checksums_reports_none_instead_of_raising():
    """`NativeChecksum` documents this as advisory and never-raising.

    Forwarded like an ordinary operation it raised `NotImplementedError` --
    which `PathSyncer`'s default policy calls bare, so a sync between two
    composite paths aborted on the first file present on both sides.
    """
    backend = MemPathBackend()
    _memory_file(backend, "app.conf", b"data")
    host = _memory_host("only", backend)

    assert host.path("app.conf").supported_checksums() == frozenset()


def test_the_default_sync_policy_runs_between_two_composite_paths():
    source_backend, target_backend = MemPathBackend(), MemPathBackend()
    _memory_file(source_backend, "app.conf", b"new")
    _memory_file(target_backend, "app.conf", b"old")
    source = _memory_host("source", source_backend).path("app.conf")
    target = _memory_host("target", target_backend).path("app.conf")

    PathSyncer().sync(source, target, dry_run=True)

    assert MemPath("app.conf", backend=target_backend).read_bytes() == b"old"


def _decoded_powershell(command):
    """The script inside a rendered PowerShell command.

    `PowerShellFlavour.command()` submits `-EncodedCommand <base64 utf-16le>`
    so a remote login shell cannot reinterpret it; assertions about what was
    rendered look inside rather than at the wrapper.
    """
    import base64

    text = str(command)
    marker = "-EncodedCommand "
    if marker not in text:
        return text
    payload = text.split(marker, 1)[1].split()[0]
    return base64.b64decode(payload).decode("utf-16-le")


def test_a_remote_checksum_separates_options_from_the_filename():
    """Without `--`, a file named `-z` is read as an OPTION: `md5sum -z`
    hashes stdin, which at EOF is the empty-input digest -- accepted by the
    parser and equal on both sides, so two different files compared as
    identical and were never copied."""
    calls = []

    def execute(command, *args, **options):
        calls.append((command, args))
        return subprocess.CompletedProcess(
            (command, *args), 0, "d41d8cd98f00b204e9800998ecf8427e  -z\n", ""
        )

    backend = MemPathBackend()
    _memory_file(backend, "-z", b"content")
    host = PosixHost(
        executor_providers=(ExecutorProvider("fake", execute),),
        path_providers=(
            PathProvider("memory", lambda *parts: MemPath(*parts, backend=backend)),
        ),
    )
    path = host.path("-z")

    host_checksum(host)(PathAndStat.from_stat(path, object()))

    rendered = " ".join((str(calls[0][0]), *(str(arg) for arg in calls[0][1])))
    assert "-- " in rendered or "'--'" in rendered


def test_a_composite_path_reports_whether_it_is_local():
    """`PathSyncer` applies its size+mtime quick check only when a side says
    it is not local, and pathapi answers `True` for a path that does not
    implement `is_local` -- so every remote-to-remote sync through composite
    paths checksummed every existing pair, discarding the answer `SFTPPath`
    already had."""
    backend = MemPathBackend()
    _memory_file(backend, "data", b"content")

    class _RemoteMemPath(MemPath):
        def is_local(self):
            return False

    remote = PathProvider(
        "remote", lambda *parts: _RemoteMemPath(*parts, backend=backend)
    )
    host = PosixHost(path_providers=(remote,))

    assert host.path("data").is_local() is False


def test_a_composite_path_without_a_backend_answer_defaults_to_local():
    backend = MemPathBackend()
    _memory_file(backend, "data", b"content")
    host = PosixHost(
        path_providers=(
            PathProvider("memory", lambda *parts: MemPath(*parts, backend=backend)),
        )
    )

    assert host.path("data").is_local() is True


def test_progress_is_reported_for_every_way_of_reading(tmp_path):
    """Only `read()` counted. `readinto`/`read1`/`readline` went through
    `__getattr__` uncounted and the dunders bypassed it, so
    `hashlib.file_digest`, a `BufferedReader` wrapper and any line-based
    consumer moved a whole file and reported no progress at all."""
    import hashlib

    from hostctl.sync import ProgressReader

    source = tmp_path / "data.bin"
    source.write_bytes(b"line one\nline two\n")

    events = []
    with source.open("rb") as raw:
        reader = ProgressReader(raw, lambda done, total: events.append(done))
        if hasattr(hashlib, "file_digest"):  # 3.11+
            hashlib.file_digest(reader, "sha256")
        else:
            # What `file_digest` does, and the path a `BufferedReader`
            # wrapper takes: `readinto` into a reusable buffer.
            buffer = bytearray(8)
            while reader.readinto(buffer):
                pass
    assert events and events[-1] == 18

    events.clear()
    with source.open("rb") as raw:
        reader = ProgressReader(raw, lambda done, total: events.append(done))
        assert [line for line in reader] == [b"line one\n", b"line two\n"]
    assert events and events[-1] == 18


def test_a_local_host_hashes_in_process(tmp_path):
    """`host_checksum(LocalHost())` spawned `md5sum` -- or, on Windows,
    certutil -- once per file, to hash bytes this process can read
    directly."""
    import hashlib

    from hostctl.transports.local._host import LocalHost

    spawned = []

    host = LocalHost()
    original = host.run

    def watched(*cmds, **options):
        spawned.append(cmds)
        return original(*cmds, **options)

    host.run = watched  # type: ignore[method-assign]
    target = tmp_path / "data.bin"
    target.write_bytes(b"content")

    digest = host_checksum(host)(
        PathAndStat.from_stat(host.path(str(target)), object())
    )

    assert digest == hashlib.md5(b"content").hexdigest()
    assert spawned == []


@pytest.mark.parametrize("dialect_name", ("cmd", "powershell"))
@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("C:\\data\\file.txt", "C:\\data\\file.txt"),
        ("\\data\\file.txt", "\\data\\file.txt"),
    ],
)
def test_a_windows_shell_is_handed_a_windows_spelling_of_the_backend_path(
    dialect_name, given, expected
):
    """SFTP spells `C:\\data\\file.txt` as `/C:/data/file.txt`; `certutil`
    reads a `/`-led argument as an option."""
    from pathlib import PureWindowsPath

    from hostctl.shell import CMD, POWERSHELL
    from hostctl.transports.ssh._config import SSHConfig
    from hostctl.sync import _remote_checksum

    dialect = {"cmd": CMD, "powershell": POWERSHELL}[dialect_name]
    host = SSHConfig(
        "winbox", path_flavor=PureWindowsPath, dialect=dialect
    )._create_host()
    seen = []

    def fake_run(*cmds, **options):
        seen.append(cmds[0])
        return subprocess.CompletedProcess(cmds, 0, "", "")

    host.run = fake_run
    with pytest.raises(ValueError):
        _remote_checksum(host, host.path(given), "md5")

    assert seen
    for command in seen:
        if isinstance(command, str):
            assert f"-LiteralPath '{expected}'" in command
        else:
            assert command[2] == expected


def test_a_composed_local_host_hashes_in_process(tmp_path, monkeypatch):
    """The in-process branch tested the host's scheme, but a composed local
    host is `windows://` or `posix://`, so it forked once per file."""
    import hashlib

    import hostctl.sync as sync_module
    from hostctl.host._config import HostConfig

    payload = b"local payload"
    target = tmp_path / "f.bin"
    target.write_bytes(payload)
    calls = []

    def remote(host, path, algorithm):
        calls.append(path)
        raise AssertionError("a local file must not be hashed by a process")

    monkeypatch.setattr(sync_module, "_remote_checksum", remote)
    host = HostConfig("posix://node?executor=local&path=local")._create_host()

    digest = host_checksum(host)(PathAndStat(host.path(str(target))))

    assert digest == hashlib.md5(payload).hexdigest()
    assert calls == []


def test_a_composed_host_over_a_non_local_backend_still_hashes_remotely(monkeypatch):
    import hostctl.sync as sync_module

    calls = []
    backend = MemPathBackend()
    _memory_file(backend, "x.bin", b"data")

    def remote(host, path, algorithm):
        calls.append(path)
        return "a" * 32

    monkeypatch.setattr(sync_module, "_remote_checksum", remote)
    host = PosixHost(
        executor_providers=(
            ExecutorProvider(
                "fake", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
            ),
        ),
        path_providers=(
            PathProvider("mem", lambda *parts: MemPath(*parts, backend=backend)),
        ),
    )

    assert host_checksum(host)(PathAndStat(host.path("x.bin"))) == "a" * 32
    assert len(calls) == 1
