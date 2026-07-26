"""Copy or synchronize files between any two hostctl connection URIs."""

import os

from pathlib_next.utils.sync import PathSyncer

from hostctl import Host, host_checksum


def copy_file(source_uri, source_name, target_uri, target_name):
    """Copy one file, for example from ``local:`` to ``docker://name``."""
    with Host(source_uri) as source, Host(target_uri) as target:
        return source.path(source_name).copy(
            target.path(target_name),
            overwrite=True,
        )


def sync_tree(source_uri, source_name, target_uri, target_name):
    """Mirror a tree, including an SSH-to-SSH transfer."""
    with Host(source_uri) as source, Host(target_uri) as target:
        PathSyncer(
            host_checksum(source, target, algorithm="sha256"),
            remove_missing=True,
        ).sync(source.path(source_name), target.path(target_name))


if __name__ == "__main__":
    # Example:
    # HOSTCTL_SOURCE_URI=ssh://source.example
    # HOSTCTL_TARGET_URI=ssh://target.example
    # HOSTCTL_SOURCE_PATH=/srv/export
    # HOSTCTL_TARGET_PATH=/srv/mirror
    sync_tree(
        os.environ["HOSTCTL_SOURCE_URI"],
        os.environ["HOSTCTL_SOURCE_PATH"],
        os.environ["HOSTCTL_TARGET_URI"],
        os.environ["HOSTCTL_TARGET_PATH"],
    )
