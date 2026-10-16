# Extending hostctl

`Host` is the operational contract; `HostConfig` owns secret-safe connection
identity, URI selection, and lifecycle. Installed integrations register config
subclasses through the `hostctl.configs` entry-point group. The registry state
(`_uri_schemes`, the caches) is a protected implementation detail; the two
hooks below are the documented override points.

## Implementing a config

A config declares its scheme and overrides two hooks:

- `@classmethod _from_parsed_uri(cls, parsed, **credentials) -> MyConfig` builds
  the config from a `urlsplit` result. Use `strict_uri_query`,
  `strict_uri_credentials`, `uri_host` and `uri_hostname`.
- `_create_host(self) -> Host` returns the host. Callers reach it through
  `config.open()` or `with config as host`, never directly.

The single underscore means "override in a subclass, never call". Declare
`uri_credentials = ("password", ...)` so dispatch rejects any other credential
before construction. There is no `from_uri`: `HostConfig(uri)` is the one
entry point, and a narrowed parse is
`config = HostConfig(uri); isinstance(config, MyConfig)`.

```python
from hostctl import HostConfig, PosixHost, strict_uri_query


class PlugConfig(HostConfig, schemes=("plug",)):
    uri_credentials = ("password",)

    def __init__(self, host, password=None):
        super().__init__()
        self.host = host
        self.password = password

    @property
    def connection_uri(self):
        return f"plug://{self.host}"

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials):
        strict_uri_query(parsed, ())
        return cls(parsed.hostname, credentials.get("password"))

    def _create_host(self):
        return PosixHost(self)
```

Register it under the `hostctl.configs` entry-point group, with the scheme as
the entry point's name:

```toml
[project.entry-points."hostctl.configs"]
plug = "my_package:PlugConfig"
```

Three registries take extensions, each with the same shape: a name is
registered once (`replace=True` replaces it deliberately), a plugin comes in
through an entry-point group, and a listing returns the sorted names.

| kind | register | entry-point group | listing |
| ---- | -------- | ----------------- | ------- |
| URI scheme | `class C(HostConfig, schemes=(...))` | `hostctl.configs` | `host_schemes()` |
| system provider | `register_system_provider(name, resolver)` | `hostctl.system_providers` | `system_provider_names()` |
| shell flavour | `register_shell_flavour(flavour)` | `hostctl.shell_flavours` | |

A second config claiming a scheme that is already registered raises
`ValueError` when the class is defined. A plugin that fails to import is
warned about and skipped: it takes down only its own scheme, which then raises
`RuntimeError` naming the failed entry point.

An entry point whose **name** equals the requested scheme is loaded first,
which is the cheap path for the common one-scheme plugin. A plugin may
declare several schemes on one config (`schemes=("plug", "plug+tls")`) under
a single entry point: a scheme no loaded config claims falls back to loading
the remaining entry points, so the secondary schemes work in a fresh process
rather than only after something dispatched the primary one. Name the entry
point after the scheme users will reach for most; the rest still resolve.

A subclass that defines its own `__init__` need not call `super().__init__()`
-- `with config as host:` works either way -- but calling it is cheaper and
keeps the lifecycle state on the instance.

## A worked application host

An application that manages one kind of machine usually wants both halves: a
config with its own URI scheme, and a host that is a `PosixHost` (so `run()`,
`path()`, `spawn()` and provider failover are inherited) plus the operations
only that machine has. This is the shape of an appliance reached over SSH,
with a second, application-specific path provider behind SFTP:

```python
from hostctl import (
    HostConfig,
    PathProvider,
    PosixHost,
    SSHConfig,
    ssh_providers,
    strict_uri_query,
    uri_host,
    uri_hostname,
)


class ApplianceConfig(HostConfig, schemes=("appliance",)):
    """`appliance://user@host:port`: an appliance reached over SSH."""

    uri_credentials = ("password",)

    def __init__(self, host, *, port=22, username="admin", password=None):
        super().__init__()
        self.host = host
        self.port = port
        self.username = username
        self.password = password

    @property
    def connection_uri(self):
        port = "" if self.port == 22 else f":{self.port}"
        return f"appliance://{self.username}@{uri_host(self.host)}{port}"

    def __repr__(self):
        return f"ApplianceConfig({self.connection_uri!r})"

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials):
        strict_uri_query(parsed, ())
        return cls(
            uri_hostname(parsed),
            port=parsed.port or 22,
            username=parsed.username or "admin",
            password=credentials.get("password"),
        )

    def _create_host(self):
        return ApplianceHost(self)


class ApplianceHost(PosixHost):
    """POSIX semantics over SSH, plus what only this appliance has."""

    config_type = ApplianceConfig

    def __init__(self, config, **options):
        ssh = SSHConfig(
            config.host,
            port=config.port,
            username=config.username,
            password=config.password,
        )
        # One call, so both providers share one SSH transport.
        executor, sftp = ssh_providers(ssh)
        api = PathProvider("api", self._api_path, capabilities={"open_read", "read"})
        super().__init__(
            config,
            executor_providers=(executor,),
            path_providers=(sftp, api),
            **options,
        )

    def _api_path(self, *segments):
        raise NotImplementedError("the appliance's file API")

    def firmware(self):
        return self.run(["cat", "/etc/version"], encoding="utf-8").stdout.strip()


config = HostConfig("appliance://admin:secret@nas.example:2222")
assert isinstance(config, ApplianceConfig)
assert str(config) == "appliance://admin@nas.example:2222"

host = config.open()  # nothing connects until the first operation
assert isinstance(host, PosixHost)
assert [provider.name for provider in host.path_providers] == ["sftp", "api"]
host.close()
```

`host.firmware()` would then open the SSH connection and run the command.

What each piece does:

- `schemes=("appliance",)` registers the scheme when the class is defined, so
  `HostConfig("appliance://...")` and `Host("appliance://...")` dispatch to it
  in this process; an entry point (above) makes it work in a fresh one.
- `uri_credentials` is the list of secrets dispatch lets through: the URI's
  password arrives as `credentials["password"]`, and `HostConfig(uri,
  passwrd=...)` raises instead of building a config without one.
- `connection_uri` renders the config with no secret, and `str(config)` is the
  same string; `repr` is overridden so the password stays out of logs.
- `_from_parsed_uri` gets the `urlsplit` result. `strict_uri_query` refuses a
  query key the scheme does not define, and `uri_hostname` keeps the host's
  case where `parsed.hostname` would fold it.
- `_create_host` is what `config.open()` and `with config as host` call.
- `config_type` names the config class the host belongs to.
- `ssh_providers()` returns the executor and SFTP path providers sharing one
  transport; building them separately would open two connections. Further
  providers follow in order: a path operation the SFTP provider declines
  before dispatch goes to `api`, and nothing that may have started is ever
  retried. `host.path_providers` and `host.last_selection` show what was
  composed and chosen.
- `firmware()` is the application's own operation, built on the inherited
  `run()`.

A host built this way is a `PosixHost`, so it can be handed to anything
written against `hostctl` -- `host_checksum()`, `Path.copy()` between hosts,
the command line's `hostctl run appliance://...` once the scheme is installed.

An API client and a transport are usually different objects. Prefer composition:

```python
from hostctl import SSHConfig


class ManagementClient:
    def __init__(self, api, host_config):
        self.api = api
        self.host_config = host_config

    def deploy(self, payload):
        with self.host_config as host:
            host.path("/tmp/payload").write_bytes(payload)
            return host.run(["install", "/tmp/payload"], check=True)


client = ManagementClient(api=management_api, host_config=SSHConfig("server"))
```

Subclass a concrete host only when the machine itself has additional transport
behavior—for example, a management WebSocket filesystem fallback:

```python
from hostctl import PosixHost, SSHConfig


class ManagedHost(PosixHost):
    def path(self, *segments, backend=None):
        if backend in (None, "sftp"):
            return super().path(*segments, backend=backend)
        if backend == "management":
            return ManagementPath(self.management_client, *segments)
        raise ValueError(f"unknown path backend: {backend}")
```

Such a path must implement the `pathapi.Path` contract. Advertise only
usable operations in `capabilities`; unsupported base `run()` and `path()` calls
raise `NotImplementedError`. Connection URIs never contain secrets—pass those
separately to `Host(connection_string, **options)`.

To add a new access mechanism rather than new host behavior, write an
`ExecutorProvider` or `PathProvider` instead of subclassing a host — see
[Systems and providers](providers.md) for the authoring contract, per-operation
capabilities, and the no-replay safety rule.

Both `with config as host:` and `with config.open() as host:` connect the
transport and guarantee `close()` on normal exit or exceptions. Hosts also
support their own context manager, `connect()`, and `close()` lifecycle.

## Writing an executor

An `ExecutorProvider` takes any callable matching `Executor`, but a callable
written from scratch has to repeat everything the built-in executors agree
on: the output type `text=`/`encoding=`/`errors=` select, `capture_output=`
and stream targets, `input=` versus `stdin=`, `check=True`, and a timeout
that raises `subprocess.TimeoutExpired` with the output so far. A
`SystemHost` can send the same call through any of its providers, so an
executor that differs returns results that depend on which provider won.

`hostctl.executor` has two base classes that do all of that, so a transport
writes only its primitive:

- `BufferedExecutor`: the transport runs a command and returns its whole
  output. Implement `_exchange(request) -> (stdout, stderr, returncode)`.
- `StreamingExecutor`: output arrives while the command runs. Implement
  `_send(request)`, `_receive(request, wait) -> (stdout, stderr, returncode)`
  and, when the transport can stop a command, `_abandon(request)`.

This one runs commands over a channel of frames (a websocket, say), where a
frame starting `1` is stdout, `2` stderr and `3` the exit status. A loopback
channel stands in for the network so this page runs:

```python
import queue
import subprocess

from hostctl import ExecutorProvider, OperationNotStartedError, PosixHost
from hostctl.executor import StreamingExecutor


class FrameExecutor(StreamingExecutor):
    """Commands over a channel of frames: 1 stdout, 2 stderr, 3 status."""

    def __init__(self, connect):
        self._connect = connect
        self._channel = None

    def _send(self, request):
        if self._channel is None:
            try:
                self._channel = self._connect()
            except OSError as exc:
                # Nothing was sent, so the host may try its next provider.
                raise OperationNotStartedError(f"cannot connect: {exc}") from exc
        payload = request.command.encode() + b"\0" + (request.input or b"")
        self._channel.send(b"0" + payload)

    def _receive(self, request, wait):
        frame = self._channel.recv(wait)
        if frame is None:
            return b"", b"", None
        kind, body = frame[:1], frame[1:]
        if kind == b"1":
            return body, b"", None
        if kind == b"2":
            return b"", body, None
        return b"", b"", int(body)

    def _abandon(self, request):
        channel, self._channel = self._channel, None
        if channel is not None:
            channel.close()  # the remote ends a command whose channel closes
        return False  # so nothing is left running


class LoopbackChannel:
    """Stands in for the remote: it reports what it was asked to run."""

    def __init__(self):
        self._frames = queue.Queue()

    def send(self, message):
        command, _, stdin = message[1:].partition(b"\0")
        self._frames.put(b"1ran: " + command + b"\n")
        self._frames.put(b"2stdin: %d bytes\n" % len(stdin))
        self._frames.put(b"30")

    def recv(self, timeout):
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        pass


host = PosixHost(
    executor_providers=(ExecutorProvider("frames", FrameExecutor(LoopbackChannel)),)
)
result = host.run("echo hello", input="abc", text=True)
assert result.stdout == "ran: /bin/sh -c 'echo hello'\n"
assert result.stderr == "stdin: 3 bytes\n"
```

The host renders the command for its shell and the base does the rest. A
remote that never answers shows the timeout half:

```python
class SilentChannel(LoopbackChannel):
    def send(self, message):
        pass  # the command runs and never finishes


try:
    FrameExecutor(SilentChannel)("sleep 60", timeout=0.1)
except subprocess.TimeoutExpired as exc:
    assert exc.orphaned is False  # `_abandon` closed the channel
    assert exc.output == b""
else:
    raise AssertionError("no timeout")
```

What the base does with a call, so a subclass need not:

- **Options.** An option the base does not know raises `TypeError`. `args`,
  `cwd` and `env` raise `NotImplementedError` unless the class declares the
  matching `executor_capabilities` (`"args"`, `"cwd"`, `"env"`); declared,
  they reach the request as text, `env` validated and `str`-keyed. A
  provider built with `ExecutorProvider(name, executor)` advertises that
  same set.
- **Input.** `input=` (any of `bytes`, `bytearray`, `memoryview`, `str`) and
  a `stdin=` stream, read to EOF, arrive as `request.input` bytes; `None`
  means the call sent none, so give the command an empty stdin. Set
  `input_unsupported = "why"` on a transport that cannot carry input: the
  call then raises before the caller's stream is read.
- **Output.** Return `bytes` or `str`; the base turns them into the type the
  call asked for, captures or writes them where `capture_output=`,
  `stdout=` and `stderr=` say (`None` is the console), and merges stderr into
  stdout for `stderr=STDOUT`. `request.merge_stderr` says when, so a
  transport that can merge natively, keeping the order, does. A streaming
  executor's uncaptured output is written as each piece arrives, decoded
  incrementally in text mode.
- **Status.** `check=True` is the default, as everywhere in hostctl; a
  missing status (`None`) is reported as `-1`.
- **Timeouts.** `request.timeout` is the call's limit (`None`: none) and
  `request.remaining()` what is left of it. A call without `timeout=` uses
  `_default_timeout()` (no limit unless overridden), so a host whose
  config leaves the total at `DEFAULT` gets the transport's own default. A
  streaming executor's deadline is the base's: it calls `_abandon` and
  raises with the captured output. A buffered one raises
  `subprocess.TimeoutExpired` from `_exchange` (`expired()` sets `orphaned`,
  `pid` and partial output), and the base re-raises it with the call's
  command and output type; `orphaned` is `True` unless the transport said
  otherwise. Set `timeout_unsupported = "why"` on a transport that cannot
  bound a command.
- **Errors.** `_normalize_error(exc)` maps a transport's exception to the one
  to raise (a returned `TimeoutExpired` gets the timeout treatment).
  `OperationNotStartedError` is the one exception that lets the host try its
  next provider, so raise it only when no byte of the command was sent: from
  `_send`, or from `_exchange` before it sends. Raised from `_receive` the
  command is already out, and the base raises `ConnectionError` instead,
  which the host does not retry. Every failure other than a decline from
  `_send` calls `_abandon`.
