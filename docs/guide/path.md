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

Container archive paths support metadata, traversal, buffered reads, and
buffered write/append/exclusive-create without requiring a shell in the image.
Docker's archive API cannot remove, rename, chmod, or create an empty directory;
those operations raise `NotImplementedError` explicitly.

QGA paths use bounded `guest-file-open/read/write/seek/flush/close` requests and
always close remote handles. Content access needs no in-guest shell. Metadata,
listing, rename, removal, and permissions require a positively probed helper;
unavailable operations raise `NotImplementedError`.

`WinRMPath.open()` is buffered: reads and close-time write-back transfer binary
chunks through PowerShell. Windows read-only attributes provide the supported
`chmod()` subset; ownership and general POSIX permission semantics do not apply.

For SSH, `SshConfig.path_flavor` explicitly selects
the `PosixPathname` or `WindowsPathname` constructor; it is independent of the
command dialect. Concrete stdlib `PurePosixPath` and `PureWindowsPath`
constructors are accepted too.
