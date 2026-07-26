# Systems and providers

A **system** is what you are talking to; a **provider** is how you reach it.
Keeping those separate is the point of `PosixHost`, `WindowsHost`, and
`IosHost`: the operating-system semantics stay put when the access mechanism
changes.

## System versus transport

Historically a host was named for its transport — `SshHost`, `WinRMHost`. But a
transport does not identify the target. SSH reaches Linux, BSD, network
appliances, and application command environments alike, and execution and file
access often travel over different channels (SSH plus SFTP, WinRM plus
PowerShell file helpers, container exec plus archive).

The system class owns shell flavour, pathname flavour, and host-info
normalization. Providers own only the access mechanism:

```python
from hostctl import ExecutorProvider, PathProvider, PosixHost

host = PosixHost(
    executor_providers=(
        ExecutorProvider("ssh", ssh_executor),
        ExecutorProvider("agent", agent_executor),
    ),
    path_providers=(
        PathProvider("sftp", sftp_path),
        PathProvider("rpc", rpc_path),
    ),
)
```

Provider collections are immutable and **ordered**: list order is precedence.
There is no second priority number to reconcile.

The transport facades remain fully supported. They are *compatible*, not
deprecated, and no removal is scheduled:

```python
from hostctl import PosixHost, SshConfig, WindowsHost, WinRMConfig

posix = PosixHost.from_ssh(SshConfig("server.example", username="root"))
windows = WindowsHost.from_winrm(WinRMConfig("server.example", "admin"))
```

## Configuration and URIs

Legacy transport URIs are unchanged. System URIs use a logical authority plus
ordered, percent-encoded, secret-free provider descriptors in repeated
`executor=` and `path=` queries:

```python
from hostctl import HostConfig, SshConfig

config = HostConfig(
    "posix://node?executor=ssh&path=sftp&path=rpc",
    provider_options={"ssh": SshConfig("node", username="root")},
)
```

Descriptor order round-trips exactly. Credentials are **constructor-only** and
never appear in `connection_uri` or a `repr()`; reconstructing a URI accepts
only `provider_options=` and `initializer=`.

## The no-replay safety rule

This is the rule that governs every fallback decision:

!!! danger "After dispatch, failure is terminal"
    Once a command or path mutation has been dispatched, a failure is **never**
    retried through the next provider. A transport that dropped mid-command may
    have already run it; replaying it could duplicate or corrupt state.

Falling through to the next provider is permitted in exactly two cases:

1. The provider **declines during probe or preflight** — it was never dispatched
   to at all.
2. The provider raises **`OperationNotStarted`**, which a provider may raise
   *only when it can prove* no remote operation began.

```python
from hostctl import OperationNotStarted

def execute(command, *args, **options):
    if not transport.is_connected():
        # Provably nothing was sent, so fallback is safe.
        raise OperationNotStarted("transport not connected")
    return transport.run(command, *args, **options)
```

Any other exception — `ConnectionResetError`, `TimeoutError`, `OSError`, or an
unknown error — is treated as uncertain and propagates to the caller. Reads use
the same conservative default.

A provider that declines is remembered for the current connection generation, so
later operations skip it instead of re-attempting it every time. Reconnecting or
replacing a provider starts a new generation and re-probes everything.

## Selection traces

Every selection records why each candidate was or was not chosen. Traces are
redacted: credential-like values are replaced with `<redacted>` before they
reach a log.

```python
path = host.path("/etc/hosts")
path.read_bytes()

for item in path.selection_trace:
    print(item["provider"], item["availability"], item["chosen"], item["reason"])
```

Each entry carries the provider name, availability, reason, capabilities,
whether it was chosen, the connection generation, the policy, and the pin flag.

`host.provider_details` reports availability for every provider **without
dispatching an operation**, so capability queries are always side-effect free:

```python
for detail in host.provider_details:
    print(detail["kind"], detail["name"], detail["availability"])
```

## Backend pinning

Composite paths select a provider per operation until they are pinned.
`.via(name)` returns a derived path pinned to one backend, and `.provider`
reports the current selection:

```python
path = host.path("/var/lib/app/state.db")
pinned = path.via("sftp")
print(pinned.provider.name)  # 'sftp'
```

A pin survives every derivation — `/`, `joinpath()`, `parent`, `parents`,
`with_name()`, `with_suffix()`, and the descendants produced by `glob()`,
`rglob()`, `walk()`, and `iterdir()`:

```python
for child in pinned.rglob("*.log"):
    assert child.provider.name == "sftp"
```

Pinning is not only ergonomic, it is a safety mechanism. Every mutation pins its
backend *before* dispatch, and each open stream owns its provider for the
lifetime of the stream, so a multi-step write can never be split across two
backends. `str(path)` is unaffected by pinning — the logical path is stable
regardless of which channel served it.

Cross-backend `rename()` is rejected outright, since no backend can rename into
another's namespace. Copying between providers is explicit and buffered through
`copy()`.

## Operation-level capabilities

Capabilities are declared per operation, not as one host-level set. A read-only
provider advertises only what it can do, and a mutation against it is rejected
explicitly rather than silently falling through to another backend:

```python
download = PathProvider(
    "download",
    download_path,
    capabilities=("stat", "read", "open_read", "exists", "is_file", "is_dir"),
)

host.path("payload").via("download").write_bytes(b"...")
# NotImplementedError: provider 'download' does not support write
```

That explicit refusal is deliberate. Silently rerouting a mutation would hide
which channel performed a stateful operation.

## Authoring a provider

An executor provider needs a name, a probe, declared capabilities, and an
`execute()` callable:

```python
from hostctl import ExecutorProvider, ProviderProbe

def probe():
    if not endpoint.reachable():
        return ProviderProbe("unavailable", "endpoint offline")
    return ProviderProbe("available", system_hint="linux")

provider = ExecutorProvider(
    "rpc",
    execute,
    capabilities=("args", "cwd", "env"),
    probe=probe,
)
```

A probe returns `available`, `degraded`, or `unavailable` with a reason, the
capabilities it can honor, and an optional `system_hint`. Probes are cached for
one connection generation, so they may perform real round trips.

A path provider returns a `pathlib_next.Path`; returning anything else is a
`TypeError`:

```python
from hostctl import PathProvider

paths = PathProvider("rpc", lambda *segments: RpcPath(*segments), probe=probe)
```

Declare only the operations you actually implement. Anything you leave out is
refused explicitly instead of being attempted and failing deeper in the stack.

For a worked example composing metadata, download, and SFTP legs with ordered
selection and pinning, see `examples/application_provider.py`.

## Session initialization

A bootstrap hook is modelled separately from both the shell flavour and the
console profile, because a raw serial session may deliberately have neither:

```python
from hostctl import SessionInitializer

host = PosixHost(
    executor_providers=(provider,),
    initializer=SessionInitializer(lambda h: h.run("sudo -v"), timeout=10),
)
```

The initializer runs once per connection generation, after the transport
connects and before the host is handed to the caller. It never changes provider
identity. If it raises, the connection is torn down and the failure propagates
rather than being retried against another provider.
