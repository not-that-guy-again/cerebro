"""Tests for the in-tree VSCode plugin.

Covers the unit-level hook contracts (install routes through the
platform package manager, verify exercises ``code --version``,
companion-extension helpers record reversible operations) plus
integration through the install/uninstall engine. Brew, apt, pacman,
and the ``code`` CLI are all faked; no real system mutation occurs.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from cerebro.models import CerebroState, PluginManifest
from cerebro.plugins.loader import (
    IN_TREE_SOURCE,
    DiscoveredPlugin,
    load_plugin_module,
)
from cerebro.runtime.context import HookContext, build_context
from cerebro.runtime.doctor import OperationStatus, check_plugin
from cerebro.runtime.engine import install as engine_install
from cerebro.runtime.engine import uninstall as engine_uninstall
from cerebro.runtime.platform import InstallResult, Platform, PlatformComponents
from cerebro.state import load_plugin_manifest, load_state
from tests.test_recorder import FakeScheduler

_VSCODE_DIR = Path(__file__).resolve().parent.parent / "cerebro" / "plugins" / "vscode"
_CODE_BIN = "/fake/bin/code"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeBrewPackageManager:
    """Stand-in for ``BrewPackageManager`` with cask support."""

    def __init__(
        self,
        *,
        already_installed: set[str] | None = None,
        already_cask_installed: set[str] | None = None,
    ) -> None:
        self.installed: set[str] = set(already_installed or set())
        self.cask_installed: set[str] = set(already_cask_installed or set())
        self.calls: list[tuple[str, str]] = []

    def is_installed(self, package: str) -> bool:
        return package in self.installed

    def install(self, package: str) -> InstallResult:
        already = package in self.installed
        self.installed.add(package)
        self.calls.append(("install", package))
        return InstallResult(already_installed=already)

    def uninstall(self, package: str) -> None:
        self.installed.discard(package)
        self.calls.append(("uninstall", package))

    def is_cask_installed(self, package: str) -> bool:
        return package in self.cask_installed

    def install_cask(self, package: str) -> InstallResult:
        already = package in self.cask_installed
        self.cask_installed.add(package)
        self.calls.append(("install_cask", package))
        return InstallResult(already_installed=already)

    def uninstall_cask(self, package: str) -> None:
        self.cask_installed.discard(package)
        self.calls.append(("uninstall_cask", package))


class FakeAptPackageManager:
    """Stand-in for ``AptPackageManager``; tracks plain installs."""

    def __init__(self, *, already_installed: set[str] | None = None) -> None:
        self.installed: set[str] = set(already_installed or set())
        self.calls: list[tuple[str, str]] = []

    def is_installed(self, package: str) -> bool:
        return package in self.installed

    def install(self, package: str) -> InstallResult:
        already = package in self.installed
        self.installed.add(package)
        self.calls.append(("install", package))
        return InstallResult(already_installed=already)

    def uninstall(self, package: str) -> None:
        self.installed.discard(package)
        self.calls.append(("uninstall", package))


class FakeCodeCli:
    """Records ``code`` CLI invocations and answers list/install queries."""

    def __init__(self, *, installed_extensions: set[str] | None = None) -> None:
        self.installed_extensions: set[str] = set(installed_extensions or set())
        self.calls: list[list[str]] = []
        self.version_returncode = 0
        self.version_stdout = "1.85.0\n"

    def __call__(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        capture_output: bool = False,
        text: bool = False,
    ) -> SimpleNamespace:
        del capture_output, text  # signature compatibility only
        argv_list = list(argv)
        self.calls.append(argv_list)
        if argv_list[1:] == ["--version"]:
            return SimpleNamespace(
                returncode=self.version_returncode,
                stdout=self.version_stdout,
                stderr="",
            )
        if argv_list[1:] == ["--list-extensions"]:
            stdout = "\n".join(sorted(self.installed_extensions)) + "\n"
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        if len(argv_list) == 3 and argv_list[1] == "--install-extension":
            self.installed_extensions.add(argv_list[2])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if len(argv_list) == 3 and argv_list[1] == "--uninstall-extension":
            self.installed_extensions.discard(argv_list[2])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if check:
            raise AssertionError(f"unexpected code invocation: {argv_list}")
        return SimpleNamespace(returncode=1, stdout="", stderr="unknown")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vscode_module() -> ModuleType:
    manifest = load_plugin_manifest(_VSCODE_DIR)
    discovered = DiscoveredPlugin(
        manifest=manifest,
        module_path=_VSCODE_DIR,
        source=IN_TREE_SOURCE,
    )
    return load_plugin_module(discovered)


def _vscode_manifest() -> PluginManifest:
    return load_plugin_manifest(_VSCODE_DIR)


def _make_ctx(
    *,
    package_manager: Any,
    fake_code: FakeCodeCli | None = None,
    when: datetime | None = None,
) -> HookContext:
    plugin = _vscode_manifest()
    state = CerebroState(core_version="0.1.0", vault_path=Path("/tmp/vault"))
    return build_context(
        plugin=plugin,
        state=state,
        package_manager=package_manager,
        scheduler=FakeScheduler(),
        when=when or datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
        command_runner=fake_code,
    )


def _patch_code_cli(
    monkeypatch: pytest.MonkeyPatch,
    fake_code: FakeCodeCli | None,
) -> None:
    """Make ``shutil.which("code")`` resolve and route subprocess to the fake.

    Patches the underlying stdlib modules so any freshly-imported copy of
    the plugin (the engine reloads it on every install/uninstall) picks
    the patches up.
    """
    import shutil
    import subprocess

    monkeypatch.setattr(
        shutil, "which", lambda name: _CODE_BIN if name == "code" else None
    )
    if fake_code is not None:
        monkeypatch.setattr(subprocess, "run", fake_code)


def _patch_no_code_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _name: None)


def _patch_platform(monkeypatch: pytest.MonkeyPatch, plat: Platform) -> None:
    """Override ``current_platform()`` for the plugin's lookup.

    The plugin imports ``cerebro.runtime.platform`` as a module and calls
    ``platform.current_platform()``, so patching the attribute on the
    module is visible to every freshly-loaded copy of the plugin.
    """
    import cerebro.runtime.platform as platform_mod

    monkeypatch.setattr(platform_mod, "current_platform", lambda: plat)


# ---------------------------------------------------------------------------
# install hook
# ---------------------------------------------------------------------------


def test_install_macos_routes_through_brew_cask(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _vscode_module()
    _patch_platform(monkeypatch, Platform.MACOS)
    fake_code = FakeCodeCli()
    _patch_code_cli(monkeypatch, fake_code)
    pm = FakeBrewPackageManager()

    ctx = _make_ctx(package_manager=pm, fake_code=fake_code)
    module.install(ctx)

    assert pm.calls == [("install_cask", "visual-studio-code")]
    ops = ctx.manifest.operations
    assert len(ops) == 1
    assert ops[0].kind == "run_pkg"
    assert ops[0].parameters == {
        "action": "install",
        "package": "visual-studio-code",
        "cask": True,
    }
    assert ops[0].pre_existing is False


def test_install_macos_records_pre_existing_when_cask_already_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _vscode_module()
    _patch_platform(monkeypatch, Platform.MACOS)
    fake_code = FakeCodeCli()
    _patch_code_cli(monkeypatch, fake_code)
    pm = FakeBrewPackageManager(already_cask_installed={"visual-studio-code"})

    ctx = _make_ctx(package_manager=pm, fake_code=fake_code)
    module.install(ctx)

    op = ctx.manifest.operations[0]
    assert op.pre_existing is True


def test_install_linux_apt_uses_plain_install(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _vscode_module()
    _patch_platform(monkeypatch, Platform.LINUX_APT)
    fake_code = FakeCodeCli()
    _patch_code_cli(monkeypatch, fake_code)
    pm = FakeAptPackageManager()

    ctx = _make_ctx(package_manager=pm, fake_code=fake_code)
    module.install(ctx)

    assert pm.calls == [("install", "code")]
    op = ctx.manifest.operations[0]
    assert op.parameters == {"action": "install", "package": "code"}
    assert "cask" not in op.parameters


def test_install_linux_pacman_uses_plain_install(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _vscode_module()
    _patch_platform(monkeypatch, Platform.LINUX_PACMAN)
    fake_code = FakeCodeCli()
    _patch_code_cli(monkeypatch, fake_code)
    pm = FakeAptPackageManager()  # is_installed/install/uninstall protocol is enough

    ctx = _make_ctx(package_manager=pm, fake_code=fake_code)
    module.install(ctx)

    assert pm.calls == [("install", "code")]


def test_install_unsupported_platform_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _vscode_module()
    _patch_platform(monkeypatch, Platform.UNSUPPORTED)
    pm = FakeBrewPackageManager()
    ctx = _make_ctx(package_manager=pm)

    with pytest.raises(RuntimeError, match="does not support platform"):
        module.install(ctx)


def test_install_raises_when_code_cli_missing_after_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _vscode_module()
    _patch_platform(monkeypatch, Platform.MACOS)
    _patch_no_code_cli(monkeypatch)
    pm = FakeBrewPackageManager()
    ctx = _make_ctx(package_manager=pm)

    with pytest.raises(RuntimeError, match="Shell Command"):
        module.install(ctx)


# ---------------------------------------------------------------------------
# verify hook
# ---------------------------------------------------------------------------


def test_verify_succeeds_when_code_version_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _vscode_module()
    fake_code = FakeCodeCli()
    _patch_code_cli(monkeypatch, fake_code)
    ctx = _make_ctx(package_manager=FakeBrewPackageManager(), fake_code=fake_code)

    module.verify(ctx)

    assert [_CODE_BIN, "--version"] in fake_code.calls


def test_verify_raises_when_code_cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _vscode_module()
    _patch_no_code_cli(monkeypatch)
    ctx = _make_ctx(package_manager=FakeBrewPackageManager())

    with pytest.raises(RuntimeError, match="not on PATH"):
        module.verify(ctx)


def test_verify_raises_when_code_version_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _vscode_module()
    fake_code = FakeCodeCli()
    fake_code.version_returncode = 2
    fake_code.version_stdout = "broken\n"
    _patch_code_cli(monkeypatch, fake_code)
    ctx = _make_ctx(package_manager=FakeBrewPackageManager(), fake_code=fake_code)

    with pytest.raises(RuntimeError, match="code --version"):
        module.verify(ctx)


# ---------------------------------------------------------------------------
# install_companion_extension / uninstall_companion_extension
# ---------------------------------------------------------------------------


def test_install_companion_extension_installs_new_and_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _vscode_module()
    fake_code = FakeCodeCli(installed_extensions=set())
    _patch_code_cli(monkeypatch, fake_code)
    ctx = _make_ctx(package_manager=FakeBrewPackageManager(), fake_code=fake_code)

    was_no_op = module.install_companion_extension(ctx, "anthropic.claude-code")

    assert was_no_op is False
    assert "anthropic.claude-code" in fake_code.installed_extensions
    assert [_CODE_BIN, "--install-extension", "anthropic.claude-code"] in fake_code.calls

    op = ctx.manifest.operations[-1]
    assert op.kind == "run_command"
    assert op.parameters["argv"] == [
        _CODE_BIN,
        "--install-extension",
        "anthropic.claude-code",
    ]
    assert op.inverse["argv"] == [
        _CODE_BIN,
        "--uninstall-extension",
        "anthropic.claude-code",
    ]
    assert op.pre_existing is False


def test_install_companion_extension_records_pre_existing_when_already_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _vscode_module()
    fake_code = FakeCodeCli(installed_extensions={"anthropic.claude-code"})
    _patch_code_cli(monkeypatch, fake_code)
    ctx = _make_ctx(package_manager=FakeBrewPackageManager(), fake_code=fake_code)

    was_no_op = module.install_companion_extension(ctx, "anthropic.claude-code")

    assert was_no_op is True
    op = ctx.manifest.operations[-1]
    assert op.pre_existing is True
    # The recorder must NOT have shelled out to install something already present.
    assert [_CODE_BIN, "--install-extension", "anthropic.claude-code"] not in fake_code.calls


def test_install_companion_extension_raises_when_code_cli_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _vscode_module()
    _patch_no_code_cli(monkeypatch)
    ctx = _make_ctx(package_manager=FakeBrewPackageManager())

    with pytest.raises(RuntimeError, match="install or repair the vscode plugin"):
        module.install_companion_extension(ctx, "anthropic.claude-code")


def test_uninstall_companion_extension_removes_present_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _vscode_module()
    fake_code = FakeCodeCli(installed_extensions={"anthropic.claude-code"})
    _patch_code_cli(monkeypatch, fake_code)
    ctx = _make_ctx(package_manager=FakeBrewPackageManager(), fake_code=fake_code)

    removed = module.uninstall_companion_extension(ctx, "anthropic.claude-code")

    assert removed is True
    assert "anthropic.claude-code" not in fake_code.installed_extensions
    op = ctx.manifest.operations[-1]
    assert op.kind == "run_command"
    assert op.parameters["argv"][1] == "--uninstall-extension"
    assert op.inverse["argv"][1] == "--install-extension"


def test_uninstall_companion_extension_no_op_when_extension_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _vscode_module()
    fake_code = FakeCodeCli(installed_extensions=set())
    _patch_code_cli(monkeypatch, fake_code)
    ctx = _make_ctx(package_manager=FakeBrewPackageManager(), fake_code=fake_code)

    removed = module.uninstall_companion_extension(ctx, "anthropic.claude-code")

    assert removed is False
    assert ctx.manifest.operations == []


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------


def _vscode_only_root(tmp_path: Path) -> Path:
    """Materialize ``cerebro/plugins/vscode/`` into a clean in-tree root."""
    root = tmp_path / "in-tree"
    target = root / "vscode"
    target.mkdir(parents=True)
    for child in _VSCODE_DIR.iterdir():
        if child.is_file():
            target.joinpath(child.name).write_bytes(child.read_bytes())
    return root


def _patch_engine_subprocess(
    monkeypatch: pytest.MonkeyPatch, fake_code: FakeCodeCli
) -> None:
    """Route the engine's subprocess.run (used during run_command inversion) through the fake."""
    import cerebro.runtime.engine as engine_mod

    monkeypatch.setattr(engine_mod.subprocess, "run", fake_code)


def test_engine_install_then_uninstall_macos_removes_newly_installed_vscode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))

    _vscode_module()
    _patch_platform(monkeypatch, Platform.MACOS)
    fake_code = FakeCodeCli()
    _patch_code_cli(monkeypatch, fake_code)

    pm = FakeBrewPackageManager()
    components = PlatformComponents(
        platform=Platform.MACOS,
        package_manager=pm,
        scheduler=FakeScheduler(),
    )
    in_tree = _vscode_only_root(tmp_path)

    engine_install(
        "vscode",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
    )
    state = load_state(home / "state.yaml")
    assert [p.name for p in state.installed_plugins] == ["vscode"]
    assert "visual-studio-code" in pm.cask_installed

    engine_uninstall(
        "vscode",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
    )
    state = load_state(home / "state.yaml")
    assert state.installed_plugins == []
    # The newly-installed cask was removed by the engine's manifest replay.
    assert ("uninstall_cask", "visual-studio-code") in pm.calls
    assert "visual-studio-code" not in pm.cask_installed


def test_engine_uninstall_macos_keeps_pre_existing_vscode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))

    _vscode_module()
    _patch_platform(monkeypatch, Platform.MACOS)
    fake_code = FakeCodeCli()
    _patch_code_cli(monkeypatch, fake_code)

    pm = FakeBrewPackageManager(already_cask_installed={"visual-studio-code"})
    components = PlatformComponents(
        platform=Platform.MACOS,
        package_manager=pm,
        scheduler=FakeScheduler(),
    )
    in_tree = _vscode_only_root(tmp_path)

    engine_install(
        "vscode",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
    )

    engine_uninstall(
        "vscode",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
    )

    # The cask was already there before Cerebro touched the system, so the
    # engine's replay must skip its uninstall.
    assert ("uninstall_cask", "visual-studio-code") not in pm.calls
    assert "visual-studio-code" in pm.cask_installed


def test_doctor_reports_drift_when_cask_removed_externally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))

    _vscode_module()
    _patch_platform(monkeypatch, Platform.MACOS)
    fake_code = FakeCodeCli()
    _patch_code_cli(monkeypatch, fake_code)

    pm = FakeBrewPackageManager()
    components = PlatformComponents(
        platform=Platform.MACOS,
        package_manager=pm,
        scheduler=FakeScheduler(),
    )
    in_tree = _vscode_only_root(tmp_path)

    engine_install(
        "vscode",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
    )
    state = load_state(home / "state.yaml")
    record = state.installed_plugins[0]

    clean = check_plugin(record, home=home, components=components)
    assert clean.is_drifted is False

    # Simulate the user removing VSCode outside Cerebro.
    pm.cask_installed.discard("visual-studio-code")

    drifted = check_plugin(record, home=home, components=components)
    assert drifted.is_drifted is True
    drifted_op = drifted.drifted_checks()[0]
    assert drifted_op.status is OperationStatus.MISSING
    assert "cask" in drifted_op.detail


def test_engine_uninstall_replays_companion_extension_via_code_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a companion extension installed via the helper is removed
    by the engine's manifest replay shelling out to ``code --uninstall-extension``."""
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))

    _vscode_module()
    _patch_platform(monkeypatch, Platform.MACOS)
    fake_code = FakeCodeCli()
    _patch_code_cli(monkeypatch, fake_code)
    _patch_engine_subprocess(monkeypatch, fake_code)

    pm = FakeBrewPackageManager()
    components = PlatformComponents(
        platform=Platform.MACOS,
        package_manager=pm,
        scheduler=FakeScheduler(),
    )
    in_tree = _vscode_only_root(tmp_path)

    engine_install(
        "vscode",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
    )

    # Simulate a sibling agent plugin calling install_companion_extension by
    # extending the recorded install manifest with a run_command op.
    from cerebro.models import InstallManifest, Operation
    from cerebro.state import load_install_manifest, save_install_manifest

    manifest_path = home / "manifests" / "vscode" / "install.yaml"
    manifest = load_install_manifest(manifest_path)
    fake_code.installed_extensions.add("anthropic.claude-code")
    extra_op = Operation(
        kind="run_command",
        parameters={
            "argv": [_CODE_BIN, "--install-extension", "anthropic.claude-code"],
        },
        inverse={
            "argv": [_CODE_BIN, "--uninstall-extension", "anthropic.claude-code"],
        },
        pre_existing=False,
    )
    extended = InstallManifest(
        plugin_name=manifest.plugin_name,
        version=manifest.version,
        operations=list(manifest.operations) + [extra_op],
        installed_at=manifest.installed_at,
    )
    save_install_manifest(extended, manifest_path)

    engine_uninstall(
        "vscode",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
    )

    assert "anthropic.claude-code" not in fake_code.installed_extensions
    assert (
        [_CODE_BIN, "--uninstall-extension", "anthropic.claude-code"]
        in fake_code.calls
    )


# ---------------------------------------------------------------------------
# Optional macOS integration test
# ---------------------------------------------------------------------------


@pytest.mark.macos
@pytest.mark.skip(
    reason=(
        "Optional integration test; only run manually on a macOS host with brew. "
        "Actually installs and uninstalls VSCode through the production path."
    )
)
def test_real_install_and_uninstall_on_macos(tmp_path: Path) -> None:  # pragma: no cover
    """Real install + uninstall against brew on a macOS runner.

    Marked skipped by default to keep CI from mutating the runner. Drop the
    skip locally to exercise the real path: this should leave brew in the
    same state it started in (pre-existing handling) when VSCode was already
    present.
    """
