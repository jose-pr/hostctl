# hostctl

Run commands and access files on a host, local or remote, protocol-agnostic.

```python
from hostctl import LocalConfig, SshConfig

with LocalConfig() as host:
    host.run("echo hello")

with SshConfig(host="nas.example.com", username="admin", password="secret") as host:
    host.run("df -h")
    for entry in host.path("/etc").iterdir():
        print(entry)
```

See the guide for [running commands](guide/run.md), [filesystem paths](guide/path.md),
and [extending `Host`](guide/extending.md) with your own project's transport.
WinRM provides PowerShell execution and a Windows-semantic filesystem path
transport backed by PowerShell.
