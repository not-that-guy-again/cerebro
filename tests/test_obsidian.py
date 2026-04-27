"""Tests for the in-core Obsidian app installer + vault scaffolder.

The helpers in :mod:`cerebro.runtime.obsidian` deliberately bypass the
operation recorder (ADR-0013: vault contents are user data and survive
uninstall). These tests pin that contract: ``ctx.manifest.operations``
stays empty across calls, package-manager calls go through the read-side
of ``ctx``, and writes go through an injectable ``runner``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cerebro.models import CerebroState, PluginManifest
from cerebro.runtime import obsidian
from cerebro.runtime.context import HookContext, build_context
from cerebro.runtime.platform import InstallResult, Platform
from tests.test_recorder import FakeScheduler


class FakeBrewPackageManager:
    def __init__(
        self,
        *,
        cask_installed: set[str] | None = None,
        installed: set[str] | None = None,
    ) -> None:
        self.cask_installed: set[str] = set(cask_installed or set())
        self.installed: set[str] = set(installed or set())

    def is_installed(self, package: str) -> bool:
        return package in self.installed

    def install(self, package: str) -> InstallResult:  # pragma: no cover - read-only in these tests
        already = package in self.installed
        self.installed.add(package)
        return InstallResult(already_installed=already)

    def uninstall(self, package: str) -> None:  # pragma: no cover
        self.installed.discard(package)

    def is_cask_installed(self, package: str) -> bool:
        return package in self.cask_installed

    def install_cask(self, package: str) -> InstallResult:  # pragma: no cover
        already = package in self.cask_installed
        self.cask_installed.add(package)
        return InstallResult(already_installed=already)

    def uninstall_cask(self, package: str) -> None:  # pragma: no cover
        self.cask_installed.discard(package)


class FakeAptPackageManager:
    def __init__(self, *, installed: set[str] | None = None) -> None:
        self.installed: set[str] = set(installed or set())

    def is_installed(self, package: str) -> bool:
        return package in self.installed

    def install(self, package: str) -> InstallResult:  # pragma: no cover
        already = package in self.installed
        self.installed.add(package)
        return InstallResult(already_installed=already)

    def uninstall(self, package: str) -> None:  # pragma: no cover
        self.installed.discard(package)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> None:
        self.calls.append(list(argv))


def _make_ctx(
    *,
    package_manager: object,
    vault_path: Path,
) -> HookContext:
    plugin = PluginManifest(
        name="claude-code",
        version="1.0.0",
        type="agent",
        description="test",
        min_core_version="0.1.0",
        supported_platforms=["macos", "linux_apt", "linux_pacman"],
    )
    state = CerebroState(core_version="0.1.0", vault_path=vault_path)
    return build_context(
        plugin=plugin,
        state=state,
        package_manager=package_manager,  # type: ignore[arg-type]
        scheduler=FakeScheduler(),
        when=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
    )


def _patch_platform(monkeypatch: pytest.MonkeyPatch, plat: Platform) -> None:
    import cerebro.runtime.platform as platform_mod

    monkeypatch.setattr(platform_mod, "current_platform", lambda: plat)


# ---------------------------------------------------------------------------
# ensure_obsidian_installed
# ---------------------------------------------------------------------------


def test_install_macos_invokes_brew_when_cask_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_platform(monkeypatch, Platform.MACOS)
    pm = FakeBrewPackageManager()
    runner = FakeRunner()
    ctx = _make_ctx(package_manager=pm, vault_path=tmp_path / "vault")

    obsidian.ensure_obsidian_installed(ctx, runner=runner)

    assert runner.calls == [["brew", "install", "--cask", "obsidian"]]
    # Recorder bypassed: vault & Obsidian are user data per ADR-0013/SPEC-11.
    assert ctx.manifest.operations == []


def test_install_macos_skips_when_cask_already_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_platform(monkeypatch, Platform.MACOS)
    pm = FakeBrewPackageManager(cask_installed={"obsidian"})
    runner = FakeRunner()
    ctx = _make_ctx(package_manager=pm, vault_path=tmp_path / "vault")

    obsidian.ensure_obsidian_installed(ctx, runner=runner)

    assert runner.calls == []
    assert ctx.manifest.operations == []


def test_install_linux_apt_uses_apt_get(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_platform(monkeypatch, Platform.LINUX_APT)
    pm = FakeAptPackageManager()
    runner = FakeRunner()
    ctx = _make_ctx(package_manager=pm, vault_path=tmp_path / "vault")

    obsidian.ensure_obsidian_installed(ctx, runner=runner)

    assert runner.calls == [["sudo", "apt-get", "install", "-y", "obsidian"]]
    assert ctx.manifest.operations == []


def test_install_linux_apt_skips_when_already_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_platform(monkeypatch, Platform.LINUX_APT)
    pm = FakeAptPackageManager(installed={"obsidian"})
    runner = FakeRunner()
    ctx = _make_ctx(package_manager=pm, vault_path=tmp_path / "vault")

    obsidian.ensure_obsidian_installed(ctx, runner=runner)

    assert runner.calls == []


def test_install_linux_pacman_uses_pacman(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_platform(monkeypatch, Platform.LINUX_PACMAN)
    pm = FakeAptPackageManager()  # is_installed/install protocol is enough
    runner = FakeRunner()
    ctx = _make_ctx(package_manager=pm, vault_path=tmp_path / "vault")

    obsidian.ensure_obsidian_installed(ctx, runner=runner)

    assert runner.calls == [["sudo", "pacman", "-S", "--noconfirm", "obsidian"]]


def test_install_unsupported_platform_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_platform(monkeypatch, Platform.UNSUPPORTED)
    pm = FakeBrewPackageManager()
    runner = FakeRunner()
    ctx = _make_ctx(package_manager=pm, vault_path=tmp_path / "vault")

    with pytest.raises(RuntimeError, match="does not support platform"):
        obsidian.ensure_obsidian_installed(ctx, runner=runner)
    assert runner.calls == []


# ---------------------------------------------------------------------------
# ensure_vault_scaffold
# ---------------------------------------------------------------------------


def test_vault_scaffold_creates_layout_and_readme(tmp_path: Path) -> None:
    vault = tmp_path / "ObsidianVault"
    pm = FakeBrewPackageManager()
    ctx = _make_ctx(package_manager=pm, vault_path=vault)

    obsidian.ensure_vault_scaffold(ctx, vault)

    assert vault.is_dir()
    for sub in ("repos", "briefings", "decisions-mirror", "templates"):
        assert (vault / sub).is_dir()
    readme = vault / "README.md"
    assert readme.is_file()
    body = readme.read_text(encoding="utf-8")
    assert "Cerebro Vault" in body
    assert "repos/" in body
    # Recorder bypassed: vault contents are user data per ADR-0013/SPEC-11.
    assert ctx.manifest.operations == []


def test_vault_scaffold_idempotent(tmp_path: Path) -> None:
    vault = tmp_path / "ObsidianVault"
    pm = FakeBrewPackageManager()
    ctx = _make_ctx(package_manager=pm, vault_path=vault)

    obsidian.ensure_vault_scaffold(ctx, vault)
    obsidian.ensure_vault_scaffold(ctx, vault)

    # No errors on the second call; layout still intact.
    for sub in ("repos", "briefings", "decisions-mirror", "templates"):
        assert (vault / sub).is_dir()


def test_vault_scaffold_does_not_overwrite_user_edited_readme(tmp_path: Path) -> None:
    vault = tmp_path / "ObsidianVault"
    vault.mkdir()
    custom = "# my notes\nuser content\n"
    (vault / "README.md").write_text(custom, encoding="utf-8")
    pm = FakeBrewPackageManager()
    ctx = _make_ctx(package_manager=pm, vault_path=vault)

    obsidian.ensure_vault_scaffold(ctx, vault)

    assert (vault / "README.md").read_text(encoding="utf-8") == custom
    # Subdirectories are still ensured, even if README pre-existed.
    assert (vault / "repos").is_dir()


def test_vault_scaffold_accepts_string_path(tmp_path: Path) -> None:
    vault = tmp_path / "ObsidianVault"
    pm = FakeBrewPackageManager()
    ctx = _make_ctx(package_manager=pm, vault_path=vault)

    obsidian.ensure_vault_scaffold(ctx, str(vault))

    assert (vault / "templates").is_dir()
