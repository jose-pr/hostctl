# Copying and synchronizing between hosts

Hostctl paths use the existing `pathlib_next` copy and synchronization
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
remove where supported. Recursive SFTP-to-SFTP copies retain
`pathlib_next`'s concurrent child fan-out.

Container, WinRM, and QGA readers fetch bounded chunks rather than buffering a
whole source file. Their writable streams stage data and commit on close, so an
interrupted copy before close does not replace the prior destination content.
SFTP and local files follow their backend's normal write behavior and do not
gain that commit-on-close property.

## Synchronizing trees

Use `pathlib_next.utils.sync.PathSyncer` for one-way tree synchronization:

```python
from pathlib_next.utils.sync import PathSyncer

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

Paths from another `pathlib_next` implementation fall back to bounded
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
preserves both size and modification time. It can also **report** a change
which is not one, and whether it does depends on the interpreter. Where
`Path.copy()` preserves `st_mode` but not timestamps, a file it copies lands
with a fresh modification time and compares unequal on the next run, so a
repeated `stat_checksum` sync re-copies what it copied before rather than
settling into a no-op. Python 3.14 added a stdlib `Path.copy()` which does
preserve timestamps, and a local path resolves to it there, so the same sync
converges. Do not rely on either behavior across versions.

Choose it when source modification times are meaningful on both sides — a tree
replicated by something which preserves them. Choose `host_checksum` when
hostctl's own copies must converge.

The default `PathSyncer` checksum reads both files completely. It is strongest
when no platform hash tool exists, but often the most expensive choice for
remote-to-remote synchronization. `dry_run=True`, `remove_missing`, hooks, and
`ignore_error` remain `PathSyncer` options and need no hostctl wrapper.

## Byte progress

`pathlib_next.Path.copy()` does not yet expose a byte-progress hook. Use
`ProgressReader` when progress matters more than retaining `Path.copy()`'s
overwrite and metadata handling:

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

- `PathSyncer` cannot preserve source symlinks and raises
  `NotImplementedError`. A direct `Path.copy(..., follow_symlinks=True)` copies
  the target content where the backend supports it.
- Byte-progress integration and a backend-native checksum protocol belong in
  `pathlib_next`; hostctl tracks those upstream rather than forking its copy
  implementation.
- Commit-on-close writers may buffer the destination content until close even
  though their read sides stream.

Three gaps are tracked as `pathlib_next` requests rather than hostctl features,
because each one lives inside machinery hostctl deliberately does not fork.
Drafts are kept in the repository under `.agents/upstream/`:

| Request | Draft |
| --- | --- |
| Byte-progress hook in `BinaryOpen.copy` / `Path.copy()` | `pathlib_next-copy-progress-hook.md` |
| Symlink handling in `PathSyncer` (plus its unreachable `ignore_error`) | `pathlib_next-symlink-sync.md` |
| Optional backend-native checksum protocol | `pathlib_next-backend-checksum-protocol.md` |
