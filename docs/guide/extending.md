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
  `strict_uri_credentials` and `uri_host`.
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
