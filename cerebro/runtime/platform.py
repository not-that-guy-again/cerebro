"""Package manager interface used by ``ctx.pkg``.

The recorder targets this protocol so that the platform-specific
implementations (Brew, Apt, Pacman) defined by SPEC-05 can be slotted in
without changing the helpers or any plugin code.

``InstallResult.already_installed`` is the input the recorder uses to mark
an install operation as ``pre_existing``: a package that the user already
had before Cerebro touched the system must not be removed on uninstall.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class InstallResult:
    already_installed: bool


@runtime_checkable
class PackageManager(Protocol):
    def is_installed(self, package: str) -> bool: ...

    def install(self, package: str) -> InstallResult: ...

    def uninstall(self, package: str) -> None: ...


__all__ = ["InstallResult", "PackageManager"]
