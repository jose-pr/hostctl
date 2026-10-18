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
hostctl deploy ssh://server mytool /opt/mytool.pyz -- --version
```

`run` runs the program after `--` directly, without a shell, and returns the
remote command's status; the `--` is required. Command output is copied without a
formatting layer. Everything the CLI itself reports uses one of four codes:

| code | meaning |
| ---- | ------- |
| 125  | connection, timeout, usage, a missing extra, or an operation this transport does not support |
| 126  | permission denied |
| 127  | not found |
| 130  | interrupted (Ctrl-C) |

125 is the catch-all, so it covers a usage error and an unsupported operation
as well as a connection failure; the message on stderr says which. A host
that cannot be reached is 125 for every subcommand, `info` included (126 when
the login is refused): `info` does not print an identity made up from the URI. A command
line the parser rejects (an unknown option, a missing operand) exits 2.

`hostctl --version` prints the installed version. Every subcommand takes
`-v`/`-q` to raise or lower log verbosity and `--loglevel NAME:LEVEL` for one
logger.

`hostctl --help` carries the same table, the URI forms, and the `URI:PATH`
grammar, so none of it lives only here.

Credentials come from the options, the environment or a prompt:

| source | how |
| ------ | --- |
| `-u USERNAME`, `--username USERNAME` | the login user; replaces the URI's |
| `-p PASSWORD`, `--password PASSWORD` | the password; replaces the URI's and `HOSTCTL_PASSWORD`; `-p ''` is no password and no prompt |
| `HOSTCTL_PASSWORD` | the password when `-p` is not given; offered only to schemes that take one |
| the URI | `ssh://user:password@host`, used when neither of the above gives a password |
| a prompt | when a host asks for a password and standard input is a terminal: asked once, hidden, and shared by every host of the run |

The prompt waits until a host needs it: an SSH key login never asks, and a
WinRM host asks only for a leg that authenticates with a password. With no
terminal (a script, CI, a tool call) nothing prompts; a host that needs a
password fails at once with exit 126:

```console
$ hostctl run ssh://server -- uptime < /dev/null
hostctl: permission denied: a password is needed and there is no terminal to ask on; pass -p PASSWORD or set HOSTCTL_PASSWORD
```

Transport extras stay optional. Using a transport whose extra is missing exits
125 with one line naming the extra to install:

```console
$ hostctl run ssh://server -- uptime
hostctl: SSH needs the 'ssh' extra: pip install "hostctl[ssh]"
```

Only hostctl's own `MissingExtraError` is reported this way; an `ImportError`
raised inside an installed dependency is a defect and keeps its traceback.

`ls` prints one name per line, with backslashes and non-printable characters
escaped (`\x1b`, `\x0a`, `\\`), because the host chooses those bytes: a name
cannot forge a second line or drive your terminal.

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

`deploy URI SOURCE PATH` installs the distribution SOURCE, with what the
host's `python3` lacks of its dependencies, at the absolute PATH on the host;
it needs the `deploy` extra as well as `cli`, and the host must be POSIX.
`--mode dir` installs a tree with a `bin/PACKAGE` launcher instead of one
zipapp file; `--package` names the import package when it is not SOURCE with
`-` as `_`; `--extra` and `--skip` add an extra or leave out a distribution
(repeatable); `--python` names the host's interpreter; `--force` writes even
an unchanged bundle. `--tree` makes SOURCE a working directory shipped as it
is, filtered by its ignore files (`--ignore-file`, `--ignore-pattern`,
`--pythonpath`; `--mode dir` only). Without `--`, it prints one line,
`deployed PATH DIGEST` or `current PATH DIGEST` when nothing needed writing.
Arguments after `--` run the installed program, and its output and status
become the command's:

```console
hostctl deploy ssh://nas mytool /opt/mytool --mode dir -- status --json
```

A PATH that exists and was not written by a deploy is refused (125), as is a
bundle that cannot be built (a compiled extension, for instance) and a step
that fails on the host, whose last stderr line is in the message. What the
command does, step by step, is in [Deploying Python](deploy.md).

A `serial:` URI is **session-only from the CLI**. `shell` and `info` work;
`run`, `ls`, `cat` and `cp` do not, because a framed `run()` needs a console
profile (prompt, login, status marker) and there is no URI spelling for one --
a profile is Python, passed as `SerialConfig(profile=...)`. Reach a device
that needs framing through the library, not the CLI.

## Several hosts

`run` and `info` take more than one host: further URI operands, and in any
operand hosts separated by commas or written as a range.

```console
hostctl run ssh://admin@nas1,nas2 ssh://backup -- uptime
hostctl run 'ssh://admin@node[1-8]' --parallel 4 -- systemctl is-active app
hostctl info 'ssh://web[a-c].example' docker://cache
```

- A range is `[1-3]` or `[a-c]`; `[1-3:02d]` pads (`01`, `02`, `03`). Several
  ranges in one URI give every combination. Quote the operand so the shell
  does not glob it.
- A host after a comma that names no scheme of its own takes the previous
  host's scheme and user: `ssh://admin@nas1,nas2:2222` is `admin` on both.
  To give it another user, repeat the scheme (`ssh://a@h1,ssh://b@h2`) or
  use another operand.
- Commas and brackets in the user part are credentials, not target syntax.
  Anywhere else, write a literal comma as `%2C`.
- `--parallel N` works on N hosts at once (default 1, one after another).

With more than one host, every line `run` prints, on stdout and on stderr,
starts with `[URI] `, the URI as you wrote it without its password; a host's
lines come out together when it finishes. `info` prints one JSON object per
line, `{"target": URI, "result": {...}}`. A host that fails gets a line on
stderr saying so:

```text
hostctl: [ssh://nas2] exit status 3
hostctl: [ssh://nas3] exit status 125: no route to host
```

The command's exit status is the worst of the hosts': 130 (interrupted) if
any was, else the highest of 125-127 if hostctl itself failed on any, else the
remote status largest in magnitude, else 0. One host — including an operand
that expands to a single host — is exactly the single-host command: no
prefix, no wrapper, the same exit statuses.

## As MCP tools

With `HOSTCTL_MCP=stdio` in its environment, `hostctl` serves every subcommand
as an MCP tool over stdio instead of running one: `hostctl.run`, `hostctl.ls`,
`hostctl.cat`, `hostctl.cp`, `hostctl.info`, `hostctl.shell` and
`hostctl.deploy`, each with a schema generated from its options.

```json
{"command": "hostctl", "env": {"HOSTCTL_MCP": "stdio"}}
```

- `uri` stays a string; further hosts are the array `more`, and `parallel`
  is an integer.
- `run` takes its program and arguments as the tool argument `"--"`, an array;
  so does `deploy` for the arguments of the program it installed.
- `deploy` is served like the rest: it needs no terminal, and it lets a tool
  caller do nothing `cp` (upload a file from the server's machine) and `run`
  (execute it) do not already. A tool caller can make the server bundle any
  distribution installed in its environment, or any directory it can read
  with `tree`, and install it on any host it can reach.
- A failure is an error result naming the exit status, so 125, 126 and 127 stay
  distinguishable.
- `username` and `password` are tool parameters like any other option; a
  tool call never prompts, and a host that needs a password nobody gave
  fails with exit 126.
- A `HOSTCTL_PASSWORD` in the server's environment applies to tool calls as
  well; a tool caller can read it (`run local: -- ...` prints the server's
  environment) and can name any host for it to be offered to.
- A tool call has no standard input, so `shell` opens a session, sends nothing
  and returns what the host printed before it closed.
- Output that is not UTF-8 (`cat` of a binary file) goes to the server's
  output target, `HOSTCTL_MCP_OUTPUT` or descriptor 3; see duho's
  documentation.
