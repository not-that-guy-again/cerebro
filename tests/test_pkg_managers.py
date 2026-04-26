from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pytest

from cerebro.runtime.pkg_managers import (
    AptPackageManager,
    BrewPackageManager,
    PacmanPackageManager,
)
from cerebro.runtime.platform import InstallResult


@dataclass
class _FakeResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class _FakeRunner:
    """Records every call. Each call consumes one scripted response."""

    def __init__(self, responses: Sequence[_FakeResult] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[list[str], bool]] = []

    def __call__(self, argv: Sequence[str], *, check: bool) -> _FakeResult:
        self.calls.append((list(argv), check))
        if not self.responses:
            return _FakeResult()
        return self.responses.pop(0)


# ---------------------------------------------------------------------------
# Brew
# ---------------------------------------------------------------------------


def test_brew_is_installed_true() -> None:
    runner = _FakeRunner([_FakeResult(returncode=0, stdout="ripgrep 14.1.0\n")])
    pm = BrewPackageManager(runner=runner)
    assert pm.is_installed("ripgrep") is True
    assert runner.calls[0][0] == ["brew", "list", "--versions", "ripgrep"]
    assert runner.calls[0][1] is False


def test_brew_is_installed_false_on_nonzero() -> None:
    runner = _FakeRunner([_FakeResult(returncode=1, stdout="")])
    pm = BrewPackageManager(runner=runner)
    assert pm.is_installed("ripgrep") is False


def test_brew_is_installed_false_on_blank_stdout() -> None:
    # `brew list --versions` returns 0 with empty stdout for a missing package
    # under some versions; treat that as not installed.
    runner = _FakeRunner([_FakeResult(returncode=0, stdout="")])
    pm = BrewPackageManager(runner=runner)
    assert pm.is_installed("ripgrep") is False


def test_brew_install_skipped_when_already_present() -> None:
    runner = _FakeRunner([_FakeResult(returncode=0, stdout="ripgrep 14.1.0\n")])
    pm = BrewPackageManager(runner=runner)
    result = pm.install("ripgrep")
    assert result == InstallResult(already_installed=True)
    # Only the is_installed probe should have been issued.
    assert len(runner.calls) == 1


def test_brew_install_runs_when_missing() -> None:
    runner = _FakeRunner(
        [
            _FakeResult(returncode=1, stdout=""),  # is_installed -> False
            _FakeResult(returncode=0),  # install
        ]
    )
    pm = BrewPackageManager(runner=runner)
    result = pm.install("ripgrep")
    assert result == InstallResult(already_installed=False)
    assert runner.calls[1][0] == ["brew", "install", "ripgrep"]
    assert runner.calls[1][1] is True


def test_brew_uninstall_calls_brew_uninstall() -> None:
    runner = _FakeRunner([_FakeResult(returncode=0)])
    pm = BrewPackageManager(runner=runner)
    pm.uninstall("ripgrep")
    assert runner.calls[0][0] == ["brew", "uninstall", "ripgrep"]
    assert runner.calls[0][1] is True


def test_brew_install_cask_uses_cask_flag() -> None:
    runner = _FakeRunner(
        [
            _FakeResult(returncode=1),  # is_cask_installed
            _FakeResult(returncode=0),  # install
        ]
    )
    pm = BrewPackageManager(runner=runner)
    pm.install_cask("rectangle")
    assert runner.calls[0][0] == ["brew", "list", "--cask", "--versions", "rectangle"]
    assert runner.calls[1][0] == ["brew", "install", "--cask", "rectangle"]


# ---------------------------------------------------------------------------
# Apt
# ---------------------------------------------------------------------------


def test_apt_is_installed_uses_dpkg_query() -> None:
    runner = _FakeRunner(
        [_FakeResult(returncode=0, stdout="install ok installed")]
    )
    pm = AptPackageManager(runner=runner)
    assert pm.is_installed("ripgrep") is True
    assert runner.calls[0][0] == ["dpkg-query", "-W", "-f=${Status}", "ripgrep"]


def test_apt_is_installed_false_on_other_status() -> None:
    runner = _FakeRunner(
        [_FakeResult(returncode=0, stdout="deinstall ok config-files")]
    )
    pm = AptPackageManager(runner=runner)
    assert pm.is_installed("ripgrep") is False


def test_apt_install_uses_sudo_apt_get_install() -> None:
    runner = _FakeRunner(
        [
            _FakeResult(returncode=1),  # not installed
            _FakeResult(returncode=0),  # install
        ]
    )
    pm = AptPackageManager(runner=runner)
    result = pm.install("ripgrep")
    assert result == InstallResult(already_installed=False)
    assert runner.calls[1][0] == ["sudo", "apt-get", "install", "-y", "ripgrep"]


def test_apt_uninstall_uses_sudo_apt_get_remove() -> None:
    runner = _FakeRunner([_FakeResult(returncode=0)])
    pm = AptPackageManager(runner=runner)
    pm.uninstall("ripgrep")
    assert runner.calls[0][0] == ["sudo", "apt-get", "remove", "-y", "ripgrep"]


# ---------------------------------------------------------------------------
# Pacman
# ---------------------------------------------------------------------------


def test_pacman_is_installed_uses_pacman_q() -> None:
    runner = _FakeRunner([_FakeResult(returncode=0)])
    pm = PacmanPackageManager(runner=runner)
    assert pm.is_installed("ripgrep") is True
    assert runner.calls[0][0] == ["pacman", "-Q", "ripgrep"]


def test_pacman_install_uses_sudo_pacman_s_noconfirm() -> None:
    runner = _FakeRunner(
        [
            _FakeResult(returncode=1),  # not installed
            _FakeResult(returncode=0),  # install
        ]
    )
    pm = PacmanPackageManager(runner=runner)
    pm.install("ripgrep")
    assert runner.calls[1][0] == [
        "sudo",
        "pacman",
        "-S",
        "--noconfirm",
        "ripgrep",
    ]


def test_pacman_uninstall_uses_r_noconfirm() -> None:
    runner = _FakeRunner([_FakeResult(returncode=0)])
    pm = PacmanPackageManager(runner=runner)
    pm.uninstall("ripgrep")
    assert runner.calls[0][0] == [
        "sudo",
        "pacman",
        "-R",
        "--noconfirm",
        "ripgrep",
    ]


# ---------------------------------------------------------------------------
# Failure surfaces (check=True propagates subprocess errors)
# ---------------------------------------------------------------------------


def test_install_propagates_runner_errors() -> None:
    """Sanity: when the package manager binary fails, callers see the error."""

    def failing_runner(argv: Sequence[str], *, check: bool) -> _FakeResult:
        if check:
            raise RuntimeError("network down")
        return _FakeResult(returncode=1)

    pm = BrewPackageManager(runner=failing_runner)
    with pytest.raises(RuntimeError, match="network down"):
        pm.install("ripgrep")
