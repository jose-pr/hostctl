import io
import shutil
import subprocess

import pytest
from pathlib_next.mempath import MemPath, MemPathBackend
from pathlib_next.utils.sync import PathAndStat, PathSyncer

from hostctl import ExecutorProvider, PathProvider, PosixHost, WindowsHost
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
    invocation = " ".join((str(calls[0][0]), *(str(arg) for arg in calls[0][1])))
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
    assert "certutil.exe" in str(calls[1])


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
