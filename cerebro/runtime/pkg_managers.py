"""Concrete ``PackageManager`` implementations for brew, apt, and pacman.

Each implementation is a thin shell over ``subprocess.run`` calls to the
platform's package manager binary. The binary path can be overridden in
tests; ``runner`` can be replaced with a fake to avoid actually invoking
the package manager during unit tests.

Brew's macOS-specific cask methods (``is_cask_installed`` /
``install_cask`` / ``uninstall_cask``) live on ``BrewPackageManager``
only; they are not part of the cross-platform ``PackageManager``
protocol because apt and pacman have no equivalent.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Protocol

from cerebro.runtime.platform import InstallResult


class _CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        check: bool,
    ) -> _CompletedProcessLike: ...


def _default_runner(argv: Sequence[str], *, check: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), check=check, capture_output=True, text=True)


class BrewPackageManager:
    """Homebrew on macOS."""

    def __init__(
        self,
        *,
        binary: str = "brew",
        runner: CommandRunner | None = None,
    ) -> None:
        self._binary = binary
        self._run = runner or _default_runner

    def is_installed(self, package: str) -> bool:
        result = self._run([self._binary, "list", "--versions", package], check=False)
        return result.returncode == 0 and bool(result.stdout.strip())

    def install(self, package: str) -> InstallResult:
        if self.is_installed(package):
            return InstallResult(already_installed=True)
        self._run([self._binary, "install", package], check=True)
        return InstallResult(already_installed=False)

    def uninstall(self, package: str) -> None:
        self._run([self._binary, "uninstall", package], check=True)

    # macOS-specific cask helpers; not part of the cross-platform protocol.

    def is_cask_installed(self, package: str) -> bool:
        result = self._run(
            [self._binary, "list", "--cask", "--versions", package], check=False
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    def install_cask(self, package: str) -> InstallResult:
        if self.is_cask_installed(package):
            return InstallResult(already_installed=True)
        self._run([self._binary, "install", "--cask", package], check=True)
        return InstallResult(already_installed=False)

    def uninstall_cask(self, package: str) -> None:
        self._run([self._binary, "uninstall", "--cask", package], check=True)


class AptPackageManager:
    """``apt-get`` on Debian/Ubuntu.

    Uses ``dpkg-query`` for presence checks because ``apt-get`` itself does
    not expose a quiet "is this installed?" mode. Install / uninstall use
    ``sudo`` because ``apt-get`` requires root.
    """

    def __init__(
        self,
        *,
        apt_binary: str = "apt-get",
        dpkg_binary: str = "dpkg-query",
        sudo_binary: str = "sudo",
        runner: CommandRunner | None = None,
    ) -> None:
        self._apt = apt_binary
        self._dpkg = dpkg_binary
        self._sudo = sudo_binary
        self._run = runner or _default_runner

    def is_installed(self, package: str) -> bool:
        result = self._run(
            [self._dpkg, "-W", "-f=${Status}", package],
            check=False,
        )
        return result.returncode == 0 and "install ok installed" in result.stdout

    def install(self, package: str) -> InstallResult:
        if self.is_installed(package):
            return InstallResult(already_installed=True)
        self._run([self._sudo, self._apt, "install", "-y", package], check=True)
        return InstallResult(already_installed=False)

    def uninstall(self, package: str) -> None:
        self._run([self._sudo, self._apt, "remove", "-y", package], check=True)


class PacmanPackageManager:
    """``pacman`` on Arch Linux."""

    def __init__(
        self,
        *,
        binary: str = "pacman",
        sudo_binary: str = "sudo",
        runner: CommandRunner | None = None,
    ) -> None:
        self._binary = binary
        self._sudo = sudo_binary
        self._run = runner or _default_runner

    def is_installed(self, package: str) -> bool:
        result = self._run([self._binary, "-Q", package], check=False)
        return result.returncode == 0

    def install(self, package: str) -> InstallResult:
        if self.is_installed(package):
            return InstallResult(already_installed=True)
        self._run(
            [self._sudo, self._binary, "-S", "--noconfirm", package],
            check=True,
        )
        return InstallResult(already_installed=False)

    def uninstall(self, package: str) -> None:
        self._run(
            [self._sudo, self._binary, "-R", "--noconfirm", package],
            check=True,
        )


__all__ = [
    "AptPackageManager",
    "BrewPackageManager",
    "CommandRunner",
    "PacmanPackageManager",
]
