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
metadata and can miss a content change which preserves both values:

```python
from hostctl import stat_checksum

PathSyncer(stat_checksum).sync(source.path("/src"), target.path("/dst"))
```

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
