from __future__ import annotations

import pytest

from cerebro.runtime.platform import (
    InstallResult,
    Platform,
    UnsupportedPlatformError,
    current_platform,
    make_platform_components,
)


class _StubPM:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def is_installed(self, package: str) -> bool:
        self.calls.append(f"is:{package}")
        return False

    def install(self, package: str) -> InstallResult:
        self.calls.append(f"install:{package}")
        return InstallResult(already_installed=False)

    def uninstall(self, package: str) -> None:
        self.calls.append(f"uninstall:{package}")


class _StubScheduler:
    def is_registered(self, name: str) -> bool:
        return False

    def register(self, name: str, schedule: str, command: str) -> None:
        return None

    def unregister(self, name: str) -> None:
        return None


def test_current_platform_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cerebro.runtime.platform._platform.system", lambda: "Darwin")
    assert current_platform() is Platform.MACOS


def test_current_platform_linux_apt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cerebro.runtime.platform._platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "cerebro.runtime.platform.shutil.which",
        lambda binary: "/usr/bin/apt-get" if binary == "apt-get" else None,
    )
    assert current_platform() is Platform.LINUX_APT


def test_current_platform_linux_pacman(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cerebro.runtime.platform._platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "cerebro.runtime.platform.shutil.which",
        lambda binary: "/usr/bin/pacman" if binary == "pacman" else None,
    )
    assert current_platform() is Platform.LINUX_PACMAN


def test_current_platform_linux_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cerebro.runtime.platform._platform.system", lambda: "Linux")
    monkeypatch.setattr("cerebro.runtime.platform.shutil.which", lambda _: None)
    assert current_platform() is Platform.UNSUPPORTED


def test_current_platform_windows_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cerebro.runtime.platform._platform.system", lambda: "Windows")
    assert current_platform() is Platform.UNSUPPORTED


def test_current_platform_apt_wins_when_both_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cerebro.runtime.platform._platform.system", lambda: "Linux")
    # Both apt-get and pacman available; apt-get wins because Debian-derived
    # distros are the common case.
    monkeypatch.setattr(
        "cerebro.runtime.platform.shutil.which",
        lambda binary: f"/usr/bin/{binary}" if binary in {"apt-get", "pacman"} else None,
    )
    assert current_platform() is Platform.LINUX_APT


def test_factory_with_injected_mocks_skips_default_construction() -> None:
    pm = _StubPM()
    sch = _StubScheduler()
    components = make_platform_components(
        platform=Platform.UNSUPPORTED,  # would normally raise, but mocks override
        package_manager=pm,
        scheduler=sch,
    )
    assert components.platform is Platform.UNSUPPORTED
    assert components.package_manager is pm
    assert components.scheduler is sch


def test_factory_unsupported_platform_raises() -> None:
    with pytest.raises(UnsupportedPlatformError, match="not supported"):
        make_platform_components(platform=Platform.UNSUPPORTED)


def test_factory_macos_returns_brew_and_launchd() -> None:
    from cerebro.runtime.pkg_managers import BrewPackageManager
    from cerebro.runtime.schedulers import LaunchdScheduler

    components = make_platform_components(platform=Platform.MACOS)
    assert isinstance(components.package_manager, BrewPackageManager)
    assert isinstance(components.scheduler, LaunchdScheduler)


def test_factory_linux_apt_returns_apt_and_systemd() -> None:
    from cerebro.runtime.pkg_managers import AptPackageManager
    from cerebro.runtime.schedulers import SystemdUserScheduler

    components = make_platform_components(platform=Platform.LINUX_APT)
    assert isinstance(components.package_manager, AptPackageManager)
    assert isinstance(components.scheduler, SystemdUserScheduler)


def test_factory_linux_pacman_returns_pacman_and_systemd() -> None:
    from cerebro.runtime.pkg_managers import PacmanPackageManager
    from cerebro.runtime.schedulers import SystemdUserScheduler

    components = make_platform_components(platform=Platform.LINUX_PACMAN)
    assert isinstance(components.package_manager, PacmanPackageManager)
    assert isinstance(components.scheduler, SystemdUserScheduler)
