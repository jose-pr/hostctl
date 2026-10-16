"""Run a command on the local host and print its output."""

from hostctl import LocalConfig

with LocalConfig() as host:
    result = host.run("echo hello from hostctl")
    print(result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout)
