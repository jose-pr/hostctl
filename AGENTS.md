# Working in this checkout

Contributor orientation for a checkout. It does not ship and is not the API
reference; the shipped header is:

| header | covers |
| ---- | ---- |
| [`src/hostctl/AGENTS.md`](src/hostctl/AGENTS.md) | the public API of `hostctl`, every public module; ships in the wheel as `hostctl/AGENTS.md` |

## Layout

| path | what lives there |
| ---- | ---------------- |
| `src/hostctl/host/` | the `Host`/`HostConfig` contracts and `Host(uri)`/`HostConfig(uri)` dispatch |
| `src/hostctl/transports/` | one package per transport (`local`, `ssh`, `winrm`, `container`, `qemu`, `serial`): config, host, executor, process, path backend, providers |
| `src/hostctl/system/` | `SystemHost` and the POSIX/Windows/IOS hosts composed from providers; system-provider resolvers |
| `src/hostctl/paths/` | composite paths (routing over providers), staged whole-file I/O, the backend path base |
| `src/hostctl/provider/` | the provider/selector contracts every transport plugs into |
| `src/hostctl/uri/` | URI parsing, credentials, redaction, `ConnectionString` |
| `src/hostctl/shell/` | shell flavours: quoting, script assembly, invocation |
| `src/hostctl/executor.py`, `process.py` | the executor and process contracts and option types |
| `src/hostctl/exceptions.py` | the `HostctlError` tree |
| `src/hostctl/sync.py` | checksum and byte-progress helpers for `pathapi` |
| `src/hostctl/_async.py`, `_extras.py`, `_registry.py` | internal: the asyncssh event-loop bridge, the table of extras, the URI-scheme registry listing |
| `src/hostctl/cli/` | the `hostctl` command on `duho`, one module per subcommand |
| `tests/conformance/` | the cross-transport battery: one contract, every provider |
| `tests/conformance/inprocess_ssh.py` | an asyncssh server on 127.0.0.1 with a real login shell; backs the `ssh-inproc` leg |
| `benchmarks/` | the on-demand performance suite and its committed results |
| `examples/` | runnable examples; deliberately not packaged |
| `docs/` | the guide published to GitHub Pages |

`tests/conformance/` is where a behaviour that must hold **everywhere** belongs.
A test there is parametrized over every registered provider and skips itself
when a provider does not advertise the capability, so a transport whose
capabilities drift silently stops being tested — which is why
`tests/conformance/test_registry.py` exists to check the registry itself.

## Environment

```bash
py -3.14 -m venv .venv/3.14-nt-amd64
.venv/3.14-nt-amd64/Scripts/python -m pip install -e ".[dev,docs]"
py -3.9 -m venv .venv/3.9-nt-amd64
.venv/3.9-nt-amd64/Scripts/python -m pip install -e ".[dev]"
```

3.14 is the development interpreter; **3.9 is the supported floor** and a change
is not done until it passes there too. A venv is named for its interpreter
version and platform (`-nt-arm64` under a native ARM64 interpreter). The `dev`
extra pulls every transport, so `".[dev]"` is the whole list — do not re-spell
the extras anywhere.

## Checks

Run before committing; CI runs the same.

```bash
python -m pytest -q -rs          # on 3.14 and on 3.9
python -m black --check src tests
python -m mkdocs build --strict  # when docs, docstrings or the header changed
```

Every text file is LF: `git ls-files --eol` shows no `w/crlf` (`dotagents eol
check` reports the same). `tests/test_packaging.py` compares every signature in
the shipped header with the code, and `tests/test_comment_hygiene.py` rejects
comments and docs that narrate history.

## Conventions

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
- **A module without a leading underscore is public** and declares `__all__`;
  `import X as X` appears only for names in it, and the contract modules never
  import `transports` or `system` at module level. `tests/test_module_hygiene.py`
  checks all three. A module stays under 600 lines unless one class alone is
  bigger (`SystemHost`, `_CompositePathMixin`).
- **The shipped header changes with the API**, in the same commit.
- Measure before claiming. Several comments in this tree cite a measured
  number because the intuitive answer was wrong.

## Releasing

Three workflows, one per concern:

- `test.yml` — `workflow_dispatch` (with a `ref` input) and throwaway `ci-*`
  tags, plus a Linux job that runs the conformance battery against **live**
  sshd, Docker and serial loopback legs, and an informative
  `downstream-pytruenas` job that runs the pytruenas suite against this
  checkout.
- `release.yml` — on a `v*` tag: test gate → build → GitHub release → PyPI,
  plus a strict docs **build** that gates the release without deploying, and a
  final job that dispatches `docs.yml` at the tag. An `-rc.N` tag publishes a
  PyPI pre-release too (pip installs it only with `--pre`); its docs are not
  deployed.
- `docs.yml` — owns every Pages deploy: on a push to `main` touching
  `docs/**`, `src/**`, `mkdocs.yml`, `README.md`, `CHANGELOG.md` or the
  workflow itself, and on `workflow_dispatch` (with a `ref` input), which is
  how a release ships its docs.

Pushing a `ci-*` tag is routine; delete it afterwards. **Pushing a `v*` tag is
a release and needs the maintainer's explicit consent for that version** —
publishing is irreversible.

Before a `v*` tag the `downstream-pytruenas` job is green, or its failures are
all named in the CHANGELOG's upgrade notes for that release.

To prepare a release, set the version in `pyproject.toml` and move the
`[Unreleased]` sections of `CHANGELOG.md` and `RELEASENOTES.md` under the new
version heading in the same commit (`RELEASENOTES.md` carries the migration
detail, benchmark table and validation evidence); the release workflow publishes that
changelog section as the GitHub release body.
