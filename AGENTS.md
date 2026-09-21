# Working in this checkout

Contributor orientation. The **shipped API header** is
[`src/hostctl/AGENTS.md`](src/hostctl/AGENTS.md) — that one goes in the wheel and
describes the public surface for a consumer. This one describes the repository,
and is not published anywhere.

## Layout

| path | what lives there |
| ---- | ---------------- |
| `src/hostctl/host/` | `Host`, `HostConfig`, URI dispatch, per-transport hosts, composite paths |
| `src/hostctl/executor/` | one buffered command executor per transport |
| `src/hostctl/process/` | persistent process adapters (`spawn`, sessions, consoles) |
| `src/hostctl/shell/` | shell flavours: quoting, script assembly, invocation |
| `src/hostctl/provider/` | the provider/selector contracts every transport plugs into |
| `src/hostctl/serial/` | console profiles (prompt, login, status framing) |
| `src/hostctl/sync/` | checksum and byte-progress helpers for `pathlib_next` |
| `tests/conformance/` | the cross-transport battery: one contract, every provider |
| `examples/` | runnable examples; deliberately not packaged |
| `docs/` | the guide published to GitHub Pages |

`tests/conformance/` is where a behaviour that must hold **everywhere** belongs.
A test there is parametrized over every registered provider and skips itself
when a provider does not advertise the capability, so a transport whose
capabilities drift silently stops being tested — which is why
`tests/conformance/test_registry.py` exists to check the registry itself.

## Environments

```bash
py -3.14 -m venv .venv/3.14-nt-amd64
.venv/3.14-nt-amd64/Scripts/python -m pip install -e ".[dev,docs]"
.venv/3.14-nt-amd64/Scripts/python -m pytest -q
```

3.14 is the development interpreter; **3.9 is the supported floor** and a change
is not done until it passes there too (`.venv/3.9-nt-amd64`). The `dev` extra
pulls every transport, so `".[dev]"` is the whole list — do not re-spell the
extras anywhere.

Before committing: `python -m black src tests` (CI checks it) and
`python -m mkdocs build --strict` if you touched docs.

## CI — three workflows, one per concern

- `test.yml` — `workflow_dispatch` (with a `ref` input) and throwaway `ci-*`
  tags, plus a Linux job that runs the conformance battery against **live**
  sshd, Docker and serial loopback legs.
- `release.yml` — on a `v*` tag: test gate → build → GitHub release → PyPI,
  plus a strict docs **build** that gates the release without deploying.
- `docs.yml` — owns every Pages deploy: on a published release, on a push to
  `main` touching docs sources, and on manual dispatch.

Pushing a `ci-*` tag is routine; delete it afterwards. **Pushing a `v*` tag is
a release and needs the maintainer's explicit consent for that version** —
publishing is irreversible.

## Conventions that are not obvious from the source

- **Files are LF-only**, every file, including on Windows.
- **A divergence is documented or it is a bug.** Where a transport cannot
  honour the common contract, add a row to `docs/guide/contracts.md`'s ledger
  saying what differs and *why the transport cannot do otherwise* — not a
  sentence saying it differs.
- **Composite paths forward the method that was called.** Adding an operation
  is a row in `_FORWARDED`, not a method body; decomposing a call into
  primitives discards transport-native implementations.
- **Capabilities are strings**, and a provider may declare one this package's
  enum does not know.
- Measure before claiming. Several comments in this tree cite a measured
  number because the intuitive answer was wrong.
