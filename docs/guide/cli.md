# Command-line interface

The command-line surface is a thin adapter over the same public `Host`, `Path`,
and shell-session contracts used by Python callers. It is built on
[duho](https://github.com/jose-pr/duho) and needs the `cli` extra:

```console
pip install "hostctl[cli]"
hostctl run local: -- python -c "print('hello')"
hostctl info ssh://server
hostctl ls ssh://server /etc
hostctl cat ssh://server /etc/os-release
hostctl cp ./artifact ssh://server:/tmp/artifact
hostctl shell ssh://server
```

`run` runs the program after `--` directly, without a shell, and returns the
remote command's status; the `--` is required. Command output is copied without a
formatting layer. Everything the CLI itself reports uses one of four codes:

| code | meaning |
| ---- | ------- |
| 125  | connection, timeout, usage, or an operation this transport does not support |
| 126  | permission denied |
| 127  | not found |
| 130  | interrupted (Ctrl-C) |

125 is the catch-all, so it covers a usage error and an unsupported operation
as well as a connection failure; the message on stderr says which. A command
line the parser rejects (an unknown option, a missing operand) exits 2.

`hostctl --version` prints the installed version. Every subcommand takes
`-v`/`-q` to raise or lower log verbosity and `--loglevel NAME:LEVEL` for one
logger.

`hostctl --help` carries the same table, the URI forms, and the `URI:PATH`
grammar, so none of it lives only here.

Passwords are never accepted as command-line arguments. Set
`HOSTCTL_PASSWORD`, or pass `--ask-password` after a subcommand to use an
interactive hidden prompt. Transport extras remain optional and produce their
normal actionable import error when absent.

`cp` accepts local paths or `URI:PATH` operands and delegates to
`pathapi.Path.copy()`. The colon before the path is required even when the
URI carries a port — `ssh://host:2222:/tmp/x`, never `ssh://host:2222/tmp/x`,
which is rejected rather than read as port-less host plus a relative path. A
URI with no authority needs no second colon: `local:/tmp/x` is the operand, the
same spelling every other subcommand takes.
Use `--overwrite` and `--recursive` explicitly.
The copy is therefore only as streaming and atomic as the two selected path
backends; see the filesystem and transfer guides for provider-specific limits.

`shell` opens `host.shell.session(terminal=True)`, submits each input line using
the configured shell flavour, and copies output directly to the local terminal.
Ctrl-C returns 130 and Ctrl-D requests EOF before closing the session. A host
that cannot allocate a terminal — a raw serial console, which is what a
`serial:` URI builds — gets a session without one rather than an error.

Output stops when the session ends, not when a read comes back empty: a serial
line answers `b""` whenever its read timeout expires and the device has simply
said nothing.

A `serial:` URI is **session-only from the CLI**. `shell` and `info` work;
`run`, `ls`, `cat` and `cp` do not, because a framed `run()` needs a console
profile (prompt, login, status marker) and there is no URI spelling for one --
a profile is Python, passed as `SerialConfig(protocol=...)`. Reach a device
that needs framing through the library, not the CLI.

## As MCP tools

With `HOSTCTL_MCP=stdio` in its environment, `hostctl` serves every subcommand
as an MCP tool over stdio instead of running one: `hostctl.run`, `hostctl.ls`,
`hostctl.cat`, `hostctl.cp`, `hostctl.info` and `hostctl.shell`, each with a
schema generated from its options.

```json
{"command": "hostctl", "env": {"HOSTCTL_MCP": "stdio"}}
```

- `run` takes its program and arguments as the tool argument `"--"`, an array.
- A failure is an error result naming the exit status, so 125, 126 and 127 stay
  distinguishable.
- A tool call cannot prompt, so `--ask-password` is refused; put
  `HOSTCTL_PASSWORD` in the server's environment.
- A tool call has no standard input, so `shell` opens a session, sends nothing
  and returns what the host printed before it closed.
- Output that is not UTF-8 (`cat` of a binary file) goes to the server's
  output target, `HOSTCTL_MCP_OUTPUT` or descriptor 3; see duho's
  documentation.
