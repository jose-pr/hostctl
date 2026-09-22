# Filesystem paths

`Host.path(...)` returns a `pathlib`-compatible path:

- **local host** → a `pathlib_next.Path` local implementation which is also a
  stdlib `pathlib.Path`;
- **SSH host** → an SFTP-backed `pathlib_next` path (the `ssh` extra);
- **WinRM host** → a Windows-semantic `WinRMPath` backed by PowerShell.
- **container host** → a POSIX- or Windows-semantic archive-backed
  `pathlib_next.Path`, selected from container inspection.
- **QEMU guest** → a POSIX- or Windows-semantic QGA file-RPC path, selected
  from guest OS information.

```python
for entry in host.path("/etc").iterdir():
    print(entry)

host.path("/tmp/example.txt").write_text("hello")
```

Container archive paths support metadata, traversal, streaming reads, and
buffered write/append/exclusive-create without requiring a shell in the image.
Docker's archive API cannot remove, rename, chmod, or create an empty directory;
those operations raise `NotImplementedError` explicitly.

QGA paths take their metadata and namespace operations from a guest-side
helper, probed during discovery: GNU `stat`/`find` on a POSIX guest, PowerShell
on a Windows one. A guest that fails the probe keeps content access and loses
only the operations that genuinely need a helper, and
`QemuConfig(path_helper=...)` replaces the probe with your own.

QGA paths use bounded `guest-file-open/read/write/seek/flush/close` requests and
always close remote handles. Content access needs no in-guest shell. A reader
from `open("rb")` is seekable wherever the agent advertises `guest-file-seek`,
so `zipfile` and `tarfile` work against a guest path; where it does not,
`seekable()` is `False` and the reader is forward-only. Metadata, listing,
rename, removal, and permissions require a positively probed helper;
unavailable operations raise `NotImplementedError` -- including the exclusive
`x` modes, which are refused by `open()` itself rather than at close-time.

`WinRMPath.open("rb")` fetches bounded binary ranges through PowerShell.
Writable modes stage data for close-time chunked write-back. Write-back happens
only on an explicit `close()` (or the context-manager exit); `flush()` does not
commit, and an abandoned writable stream is discarded with a
`ResourceWarning`. Windows read-only attributes provide the supported
`chmod()` subset; ownership and general POSIX permission semantics do not
apply. Reparse points are reported as symlink-style metadata; following one
resolves its target once and raises `OSError` when unresolved.

`WinRMPath.symlink_to()` issues `New-Item -ItemType SymbolicLink`. Windows
only permits this from an elevated session or with Developer Mode enabled;
otherwise the privilege failure is normalized to `PermissionError`.
`readlink()` reports the reparse point's stored target.

Container archive paths create symlinks by shipping a `SYMTYPE` tar member
through `put_archive()`, and `readlink()` reads the member's `linkname` (or
Docker's `linkTarget` metadata when supplied). Reads through a container
symlink follow it lazily: the link is recognized from its member header, so a
regular file still costs exactly one archive pull.

QGA paths raise `NotImplementedError` for `symlink_to()` and `readlink()` --
the guest agent's `guest-file-*` protocol has no symlink RPC, and emulating
one through `guest-exec` would be a different transport with different
permission semantics.

For SSH, `SshConfig.path_flavor` explicitly selects
the `PosixPathname` or `WindowsPathname` constructor; it is independent of the
command dialect. Concrete stdlib `PurePosixPath` and `PureWindowsPath`
constructors are accepted too.
