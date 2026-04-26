"""Platform detection, package manager protocol, and platform factory.

The protocol (``PackageManager``) is what ``ctx.pkg`` and the recorder
target. Concrete implementations live in ``cerebro.runtime.pkg_managers``
and ``cerebro.runtime.schedulers``; the factory ``make_platform_components``
selects them based on the detected platform.

``InstallResult.already_installed`` is the input the recorder uses to mark
an install operation as ``pre_existing``: a package that the user already
had before Cerebro touched the system must not be removed on uninstall.

Unsupported platforms return ``Platform.UNSUPPORTED`` from
``current_platform()`` and any code path that needs a concrete manager
raises ``UnsupportedPlatformError`` immediately.
"""

from __future__ import annotations

import enum
import platform as _platform
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from cerebro.runtime.scheduling import Scheduler


class Platform(enum.Enum):
    MACOS = "macos"
    LINUX_APT = "linux_apt"
    LINUX_PACMAN = "linux_pacman"
    UNSUPPORTED = "unsupported"


class UnsupportedPlatformError(RuntimeError):
    """Raised when a concrete platform component is requested on an unsupported OS."""


@dataclass(frozen=True)
class InstallResult:
    already_installed: bool


@runtime_checkable
class PackageManager(Protocol):
    def is_installed(self, package: str) -> bool: ...

    def install(self, package: str) -> InstallResult: ...

    def uninstall(self, package: str) -> None: ...


def current_platform() -> Platform:
    """Detect the current platform.

    Linux variants are distinguished by which package manager binary is on
    PATH. ``apt-get`` wins over ``pacman`` if both are present; that is a
    deliberate choice because Debian-derived distros are the common case
    and ``pacman`` is rarely co-installed.
    """
    system = _platform.system()
    if system == "Darwin":
        return Platform.MACOS
    if system == "Linux":
        if shutil.which("apt-get") is not None:
            return Platform.LINUX_APT
        if shutil.which("pacman") is not None:
            return Platform.LINUX_PACMAN
        return Platform.UNSUPPORTED
    return Platform.UNSUPPORTED


@dataclass(frozen=True)
class PlatformComponents:
    platform: Platform
    package_manager: PackageManager
    scheduler: Scheduler


def make_platform_components(
    *,
    platform: Platform | None = None,
    package_manager: PackageManager | None = None,
    scheduler: Scheduler | None = None,
) -> PlatformComponents:
    """Return the package manager + scheduler appropriate for ``platform``.

    Tests inject mocks via ``package_manager`` / ``scheduler``. Production
    callers pass nothing and let ``current_platform()`` decide.

    Raises ``UnsupportedPlatformError`` when no overrides are supplied for
    a platform that has no concrete implementation.
    """
    plat = platform if platform is not None else current_platform()
    pm = package_manager if package_manager is not None else _default_package_manager(plat)
    sch = scheduler if scheduler is not None else _default_scheduler(plat)
    return PlatformComponents(platform=plat, package_manager=pm, scheduler=sch)


def _default_package_manager(plat: Platform) -> PackageManager:
    from cerebro.runtime.pkg_managers import (
        AptPackageManager,
        BrewPackageManager,
        PacmanPackageManager,
    )

    if plat is Platform.MACOS:
        return BrewPackageManager()
    if plat is Platform.LINUX_APT:
        return AptPackageManager()
    if plat is Platform.LINUX_PACMAN:
        return PacmanPackageManager()
    raise UnsupportedPlatformError(
        "No package manager available: this OS is not supported by Cerebro "
        "(macOS, Debian/Ubuntu, and Arch are supported)."
    )


def _default_scheduler(plat: Platform) -> Scheduler:
    from cerebro.runtime.schedulers import LaunchdScheduler, SystemdUserScheduler

    if plat is Platform.MACOS:
        return LaunchdScheduler()
    if plat in (Platform.LINUX_APT, Platform.LINUX_PACMAN):
        return SystemdUserScheduler()
    raise UnsupportedPlatformError(
        "No scheduler available: this OS is not supported by Cerebro "
        "(macOS launchd and Linux systemd-user are supported)."
    )


__all__ = [
    "InstallResult",
    "PackageManager",
    "Platform",
    "PlatformComponents",
    "UnsupportedPlatformError",
    "current_platform",
    "make_platform_components",
]
