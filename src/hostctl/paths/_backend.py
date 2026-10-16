"""One base for the whole-file backend paths.

WinRM, the Docker archive API and the QEMU guest agent each hand pathapi a
path class over a backend object that speaks whole files. What is common to
them lives here -- construction, derivations, listing, `open()`, copy,
`rename()` and `chmod()` -- so a fix lands once; a transport keeps only what
it does differently.
"""

from __future__ import annotations

import errno
import typing
from pathlib import PurePath as _StdPurePath

from pathapi import FileStat, Path, PosixPathname, WindowsPathname
from pathapi.providers import is_safe_child_name, is_windows_flavoured, parse_mode

from ._staged_io import copy_from, staged_open


class BackendPathMixin:
    """Behaviour shared by every whole-file backend path, either flavour.

    A concrete class names its backend type in `_backend_type`; a path
    segment lends its backend to a new path only when it is of the same
    family, so a container path handed to a QGA constructor lends nothing.
    """

    __slots__ = ()

    _backend_type: typing.ClassVar[type] = object

    def __init__(self, *segments, backend=None):
        # Python 3.14's pathlib.PurePath.__init__ takes no keywords; `__new__`
        # has already initialised the path state and attached the backend.
        if not hasattr(self, "_raw_paths") and not hasattr(self, "_parts"):
            _StdPurePath.__init__(self, *segments)

    def __new__(cls, *segments, backend=None):
        inherited = next(
            (
                segment.backend
                for segment in segments
                if isinstance(segment, BackendPathMixin)
                and segment._backend_type is cls._backend_type
            ),
            None,
        )
        self = super().__new__(cls, *segments)  # type: ignore[misc]
        self._backend = backend or inherited
        if self._backend is None:
            raise TypeError(f"{cls.__name__} requires a backend")
        return self

    @property
    def backend(self):
        return self._backend

    def with_segments(self, *segments):
        return type(self)(*segments, backend=self.backend)

    def __truediv__(self, key):
        return type(self)(self, key, backend=self.backend)

    def joinpath(self, *args):
        return type(self)(self, *args, backend=self.backend)

    @property
    def parent(self):
        return type(self)(str(super().parent), backend=self.backend)

    # Before 3.12, pathlib builds these with `object.__new__`, skipping
    # `__new__` and so the backend; pathapi's safe overwrite names its
    # temporary sibling with `with_name()`.
    @property
    def parents(self):
        return tuple(
            type(self)(str(item), backend=self.backend) for item in super().parents
        )

    def _rebind(self, derived):
        return type(self)(str(derived), backend=self.backend)

    def with_name(self, name):
        return self._rebind(super().with_name(name))

    def with_stem(self, stem):
        return self._rebind(super().with_stem(stem))

    def with_suffix(self, suffix):
        return self._rebind(super().with_suffix(suffix))

    def relative_to(self, *other, **kwargs):
        return self._rebind(super().relative_to(*other, **kwargs))

    def stat(self, *, follow_symlinks: bool = True) -> FileStat:
        return self.backend.stat(str(self), follow_symlinks=follow_symlinks)

    def _scandir(self):
        # A listing is untrusted input: a name that is not one path component
        # (`..`, `a/b`) would make `self / name` address outside the directory.
        windows = is_windows_flavoured(self)
        for name, value in self.backend.scandir(str(self)):
            if is_safe_child_name(name, windows=windows):
                yield name, value

    def iterdir(self):
        for name, _ in self._scandir():
            yield self / name

    def _check_open(self, mode: str) -> None:
        """Refuse a mode before any work happens; the default accepts all."""

    def _open(self, mode="r", buffering=-1):
        del buffering
        self._check_open(mode)
        return staged_open(self.backend, str(self), mode)

    def _copy_from(self, source, **kwargs):
        """CPython 3.14's `Path.copy()` destination hook."""
        return copy_from(self, source, **kwargs)

    def _rename_compatible(self, target) -> bool:
        # `rename()` runs inside one backend; `move()` copies and deletes
        # for anything else, including another backend of the same class.
        return type(target) is type(self) and target.backend is self.backend

    def rename(self, target):
        if isinstance(target, (str, _StdPurePath)) and not isinstance(target, Path):
            target = self.with_segments(target)
        if not self._rename_compatible(target):
            raise OSError(errno.EXDEV, "cannot rename across backends", str(target))
        self._rename_on_backend(target)
        return target

    def _rename_on_backend(self, target) -> None:
        self.backend.rename(str(self), str(target))

    def chmod(self, mode, *, follow_symlinks: bool = True):
        self._chmod_on_backend(parse_mode(mode), follow_symlinks)

    def _chmod_on_backend(self, mode: int, follow_symlinks: bool) -> None:
        self.backend.chmod(str(self), mode, follow_symlinks=follow_symlinks)

    def unlink(self, missing_ok=False):
        self.backend.unlink(str(self), missing_ok=missing_ok)

    def rmdir(self):
        self.backend.rmdir(str(self))


class PosixBackendPath(BackendPathMixin, PosixPathname, Path):
    """A POSIX-flavoured whole-file backend path."""

    __slots__ = ("_backend",)


class WindowsBackendPath(BackendPathMixin, WindowsPathname, Path):
    """A Windows-flavoured whole-file backend path."""

    __slots__ = ("_backend",)
