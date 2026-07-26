# Command-line interface

The command-line surface is a thin adapter over the same public `Host`, `Path`,
and shell-session contracts used by Python callers:

```console
hostctl run local: -- python -c "print('hello')"
hostctl info ssh://server
hostctl ls ssh://server /etc
hostctl cat ssh://server /etc/os-release
hostctl cp ./artifact ssh://server:/tmp/artifact
hostctl shell ssh://server
```

`run` returns the remote command's status. Connection and timeout failures use
125, permission failures use 126, and missing resources use 127. Command output
is copied without a formatting layer.

Passwords are never accepted as command-line arguments. Set
`HOSTCTL_PASSWORD`, or pass `--ask-password` after a subcommand to use an
interactive hidden prompt. Transport extras remain optional and produce their
normal actionable import error when absent.

`cp` accepts local paths or `URI:PATH` operands and delegates to
`pathlib_next.Path.copy()`. Use `--overwrite` and `--recursive` explicitly.
The copy is therefore only as streaming and atomic as the two selected path
backends; see the filesystem and transfer guides for provider-specific limits.

`shell` opens `host.shell.session(terminal=True)`, submits each input line using
the configured shell flavour, and copies output directly to the local terminal.
Ctrl-C returns 130 and Ctrl-D requests EOF before closing the session.
