# Extending hostctl

`Host` is the operational contract; `HostConfig` owns secret-safe connection
identity, URI selection, and lifecycle. Installed integrations register config
subclasses through the `hostctl.configs` entry-point group. Registry hooks are
protected implementation details.

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

An API client and a transport are usually different objects. Prefer composition:

```python
from hostctl import SshConfig


class ManagementClient:
    def __init__(self, api, host_config):
        self.api = api
        self.host_config = host_config

    def deploy(self, payload):
        with self.host_config as host:
            host.path("/tmp/payload").write_bytes(payload)
            return host.run(["install", "/tmp/payload"], check=True)


client = ManagementClient(api=management_api, host_config=SshConfig("server"))
```

Subclass a concrete host only when the machine itself has additional transport
behavior—for example, a management WebSocket filesystem fallback:

```python
from hostctl import PosixHost, SshConfig


class ManagedHost(PosixHost):
    def path(self, *segments, backend=None):
        if backend in (None, "sftp"):
            return super().path(*segments, backend=backend)
        if backend == "management":
            return ManagementPath(self.management_client, *segments)
        raise ValueError(f"unknown path backend: {backend}")
```

Such a path must implement the `pathlib_next.Path` contract. Advertise only
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
