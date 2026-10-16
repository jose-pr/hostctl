# Copying and synchronizing between hosts

Hostctl paths use the existing `pathapi` copy and synchronization
machinery. There is deliberately no separate transfer engine:

```python
from hostctl import Host

with Host("ssh://source.example") as source, Host(
    "winrms://operator@target.example", password=password
) as target:
    source.path("/srv/report.csv").copy(
        target.path(r"C:\Imports\report.csv"),
        overwrite=True,
    )
```

`Path.copy()` works between any two path implementations and supports
`overwrite`, `recursive`, `follow_symlinks`, metadata preservation, and error
handling. `Path.move()` first tries a backend rename and falls back to copy plus
remove where supported. A directory copied within one SFTP server runs
as `sftppath`'s concurrent native requests.

Container, WinRM, and QGA readers fetch bounded chunks rather than buffering a
whole source file. Their writable streams stage data and commit on close, so an
interrupted copy before close does not replace the prior destination content.
SFTP and local files follow their backend's normal write behavior and do not
gain that commit-on-close property.

## Synchronizing trees

Use `pathapi.sync.PathSyncer` for one-way tree synchronization:

```python
from pathapi.sync import PathSyncer

from hostctl import host_checksum

checksum = host_checksum(source, target, algorithm="sha256")
PathSyncer(checksum, remove_missing=True).sync(
    source.path("/srv/export"),
    target.path("/srv/mirror"),
)
```

`host_checksum()` recognizes paths by provider/backend identity. For a path
owned by one of its hosts, it runs the platform hash tool beside the data:

| Target shell | Command |
| --- | --- |
| POSIX, Bash, Zsh, Fish | `md5sum`, `sha256sum`, and related `*sum` tools |
| PowerShell 5/7 | `Get-FileHash` |
| cmd.exe | `certutil -hashfile` |

Paths from another `pathapi` implementation fall back to bounded
read-side hashing. Pass both hosts to avoid downloading either unchanged side.
Supported remote algorithms are MD5, SHA-1, SHA-256, SHA-384, and SHA-512.

For a fast comparison which reads no file content and launches no remote
commands, use `stat_checksum`. It compares cached `(size, modification time)`
metadata:

```python
from hostctl import stat_checksum

PathSyncer(stat_checksum).sync(source.path("/src"), target.path("/dst"))
```

Two caveats decide whether it fits. It can **miss** a content change which
preserves both size and modification time. And it settles only where the
target can take a modification time: `PathSyncer` copies the source's
modification time onto a target that supports pathapi's `Utime`, so a
repeated `stat_checksum` sync there is a no-op. Local, SSH and WinRM paths do;
a QEMU guest path does when the guest has a path helper, and a container path
when the container has `touch` (Linux) or `powershell.exe` (Windows). Where a
target does not, a file copied there lands with a fresh modification time,
compares unequal on the next run, and is copied again.

Choose `host_checksum` where the target cannot hold the time and hostctl's own
copies must converge.

The default `PathSyncer` checksum uses a server-side digest when both paths
implement pathapi's `NativeChecksum` (an SSH host's path does) and reads both
files completely otherwise, which is often the most expensive choice for
remote-to-remote synchronization. `dry_run=True`, `remove_missing`, hooks, and
`ignore_error` remain `PathSyncer` options and need no hostctl wrapper.

## Byte progress

`Path.copy(target, progress=report)` calls `report(path, bytes_copied,
total_size)` for each chunk of each file it streams. A native transfer that
bypasses the streaming copy -- the SFTP fan-out of a directory copied within
one server -- does not call it.

`ProgressReader` reports progress on a stream copy of your own:

```python
import shutil

from hostctl import ProgressReader

size = source_path.stat().st_size
with ProgressReader(source_path.open("rb"), report, total=size) as reader:
    with target_path.open("wb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
```

`report(bytes_read, total_bytes)` is called after every read.

## Current limitations

- `PathSyncer(follow_symlinks=False)` recreates a source link on the target
  only where the target path supports `symlink_to()`; elsewhere that entry is
  a `NotImplementedError`, offered to `ignore_error`.
- Commit-on-close writers (WinRM, the Docker archive API, QGA) buffer the
  destination content until close even though their read sides stream.
