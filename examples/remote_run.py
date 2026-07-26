"""Run a command and list a directory on a remote host over SSH.

Needs the `ssh` extra: pip install hostctl[ssh]
"""

import os

from hostctl import SshConfig

config = SshConfig(
    host=os.environ.get("HOSTCTL_EXAMPLE_HOST", "example.com"),
    username=os.environ.get("HOSTCTL_EXAMPLE_USER", "root"),
    password=os.environ.get("HOSTCTL_EXAMPLE_PASSWORD"),
)

with config as host:
    result = host.run("uname -a")
    print(result.stdout)
    for entry in host.path("/etc").iterdir():
        print(entry)
