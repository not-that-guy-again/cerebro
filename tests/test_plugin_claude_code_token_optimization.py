"""Tests for the in-tree claude-code-token-optimization behavior plugin.

Covers the unit-level hook contracts (install / configure / verify with
the caveman install mechanism mocked through ``subprocess.run``), drift
detection on a deliberately-corrupted block, and an end-to-end install
+ uninstall through the engine. The ``claude``, ``code``, npm, and brew
commands are all faked; no real system mutation happens.

The unit tests redirect ``~/.claude`` under ``tmp_path`` via a patched
``Path.expanduser`` because the in-tree claude-code plugin's
``rules_file_path`` resolves it to the user's real home otherwise.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from cerebro.models import CerebroState, InstalledPlugin, PluginManifest
from cerebro.plugins.loader import (
    IN_TREE_SOURCE,
    DiscoveredPlugin,
    load_plugin_module,
)
from cerebro.runtime.context import HookContext, build_context
from cerebro.runtime.doctor import OperationStatus, check_plugin
from cerebro.runtime.engine import install as engine_install
from cerebro.runtime.engine import uninstall as engine_uninstall
from cerebro.runtime.platform import Platform, PlatformComponents
from cerebro.state import load_plugin_manifest
from tests.test_plugin_claude_code import (
    FakeBrewPackageManager,
    FakeRunner,
    RecordingAuthHandoff,
)
from tests.test_recorder import FakeScheduler

_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "cerebro" / "plugins"
_TOKEN_DIR = _PLUGINS_DIR / "claude-code-token-optimization"
_CLAUDE_DIR = _PLUGINS_DIR / "claude-code"
_VSCODE_DIR = _PLUGINS_DIR / "vscode"


# ---------------------------------------------------------------------------
# Fakes — extend the shared FakeRunner with caveman command awareness
# ---------------------------------------------------------------------------


class FakeRunnerWithCaveman(FakeRunner):
    """Adds ``claude plugin {list,install,uninstall} caveman`` to FakeRunner."""

    def __init__(
        self,
        *,
        caveman_installed: bool = False,
        installed_extensions: set[str] | None = None,
    ) -> None:
        super().__init__(installed_extensions=installed_extensions)
        self.caveman_installed = caveman_installed

    def __call__(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        capture_output: bool = False,
        text: bool = False,
    ) -> SimpleNamespace:
        argv_list = list(argv)
        if argv_list == ["claude", "plugin", "list"]:
            self.calls.append(argv_list)
            stdout = "caveman\n" if self.caveman_installed else ""
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        if argv_list == ["claude", "plugin", "install", "caveman"]:
            self.calls.append(argv_list)
            self.caveman_installed = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv_list == ["claude", "plugin", "uninstall", "caveman"]:
            self.calls.append(argv_list)
            self.caveman_installed = False
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return super().__call__(
            argv,
            check=check,
            capture_output=capture_output,
            text=text,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _token_module() -> ModuleType:
    discovered = DiscoveredPlugin(
        manifest=load_plugin_manifest(_TOKEN_DIR),
        module_path=_TOKEN_DIR,
        source=IN_TREE_SOURCE,
    )
    return load_plugin_module(discovered)


def _token_manifest() -> PluginManifest:
    return load_plugin_manifest(_TOKEN_DIR)


def _claude_manifest() -> PluginManifest:
    return load_plugin_manifest(_CLAUDE_DIR)


def _state_with_claude(vault: Path) -> CerebroState:
    return CerebroState(
        core_version="0.1.0",
        vault_path=vault,
        installed_plugins=[
            InstalledPlugin(
                name="vscode",
                source=IN_TREE_SOURCE,
                version="1.0.0",
                installed_at=datetime(2026, 4, 27, 11, 0, 0, tzinfo=UTC),
                enabled=True,
            ),
            InstalledPlugin(
                name="claude-code",
                source=IN_TREE_SOURCE,
                version="1.0.0",
                installed_at=datetime(2026, 4, 27, 11, 30, 0, tzinfo=UTC),
                enabled=True,
            ),
        ],
    )


def _make_ctx(
    *,
    state: CerebroState,
    fake_runner: FakeRunnerWithCaveman,
) -> HookContext:
    plugin = _token_manifest()
    claude_manifest = _claude_manifest()
    vscode_manifest = load_plugin_manifest(_VSCODE_DIR)

    def manifest_lookup(name: str) -> PluginManifest | None:
        if name == "claude-code":
            return claude_manifest
        if name == "vscode":
            return vscode_manifest
        if name == "claude-code-token-optimization":
            return plugin
        return None

    return build_context(
        plugin=plugin,
        state=state,
        package_manager=FakeBrewPackageManager(),
        scheduler=FakeScheduler(),
        when=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        manifest_lookup=manifest_lookup,
        command_runner=fake_runner,
    )


def _redirect_claude_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Make ``~/.claude/...`` resolve under ``tmp_path``.

    The token-optimization plugin re-imports the in-tree claude-code
    plugin via ``discover_plugins`` to call its real ``rules_file_path``
    helper, which expands ``~/.claude``. Redirecting ``Path.expanduser``
    is the only sound way to keep that off the user's real home.
    """
    real_expanduser = Path.expanduser

    def fake_expanduser(self: Path) -> Path:
        s = str(self)
        if s.startswith("~/.claude"):
            return tmp_path / "claude-home" / s[len("~/.claude/") :]
        return real_expanduser(self)

    monkeypatch.setattr(Path, "expanduser", fake_expanduser)
    return tmp_path / "claude-home" / "CLAUDE.md"


def _patch_subprocess_run(
    monkeypatch: pytest.MonkeyPatch, fake_runner: FakeRunnerWithCaveman
) -> None:
    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_runner)


# ---------------------------------------------------------------------------
# install hook (unit)
# ---------------------------------------------------------------------------


def test_install_records_caveman_install_and_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    rules_path = _redirect_claude_home(monkeypatch, tmp_path)
    fake_runner = FakeRunnerWithCaveman(caveman_installed=False)
    _patch_subprocess_run(monkeypatch, fake_runner)

    module = _token_module()
    state = _state_with_claude(tmp_path / "vault")
    ctx = _make_ctx(state=state, fake_runner=fake_runner)

    module.install(ctx)

    # Caveman was installed via the CLI mechanism the plugin assumes.
    assert ["claude", "plugin", "install", "caveman"] in fake_runner.calls
    assert fake_runner.caveman_installed is True

    # Managed block landed in the rules file with both required items.
    assert rules_path.exists()
    body = rules_path.read_text(encoding="utf-8")
    assert ">>> cerebro:plugin:claude-code-token-optimization >>>" in body
    assert "Avoid platitudes" in body
    assert '"great question"' in body
    assert "10th response" in body

    # Two operations recorded: caveman command + block.
    kinds = [op.kind for op in ctx.manifest.operations]
    assert kinds == ["run_command", "add_block"]
    cmd_op = ctx.manifest.operations[0]
    assert cmd_op.parameters["argv"] == ["claude", "plugin", "install", "caveman"]
    assert cmd_op.inverse["argv"] == ["claude", "plugin", "uninstall", "caveman"]
    assert cmd_op.pre_existing is False
    block_op = ctx.manifest.operations[1]
    assert block_op.parameters["plugin_name"] == "claude-code-token-optimization"
    assert block_op.parameters["path"] == str(rules_path)


def test_install_records_pre_existing_when_caveman_already_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user-managed caveman install must not be uninstalled on cleanup."""
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    _redirect_claude_home(monkeypatch, tmp_path)
    fake_runner = FakeRunnerWithCaveman(caveman_installed=True)
    _patch_subprocess_run(monkeypatch, fake_runner)

    module = _token_module()
    state = _state_with_claude(tmp_path / "vault")
    ctx = _make_ctx(state=state, fake_runner=fake_runner)

    module.install(ctx)

    cmd_op = ctx.manifest.operations[0]
    assert cmd_op.kind == "run_command"
    assert cmd_op.pre_existing is True
    # Pre-existing means the install command was NOT shelled out.
    assert ["claude", "plugin", "install", "caveman"] not in fake_runner.calls


# ---------------------------------------------------------------------------
# configure hook (unit)
# ---------------------------------------------------------------------------


def test_configure_reapplies_block_and_caveman_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configure is idempotent: re-runs install steps after claude-code drift."""
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    rules_path = _redirect_claude_home(monkeypatch, tmp_path)
    fake_runner = FakeRunnerWithCaveman(caveman_installed=False)
    _patch_subprocess_run(monkeypatch, fake_runner)

    module = _token_module()
    state = _state_with_claude(tmp_path / "vault")
    ctx = _make_ctx(state=state, fake_runner=fake_runner)

    module.configure(ctx)

    assert ["claude", "plugin", "install", "caveman"] in fake_runner.calls
    assert rules_path.exists()
    body = rules_path.read_text(encoding="utf-8")
    assert ">>> cerebro:plugin:claude-code-token-optimization >>>" in body


def test_configure_records_pre_existing_when_caveman_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    _redirect_claude_home(monkeypatch, tmp_path)
    fake_runner = FakeRunnerWithCaveman(caveman_installed=True)
    _patch_subprocess_run(monkeypatch, fake_runner)

    module = _token_module()
    state = _state_with_claude(tmp_path / "vault")
    ctx = _make_ctx(state=state, fake_runner=fake_runner)

    module.configure(ctx)

    cmd_op = ctx.manifest.operations[0]
    assert cmd_op.kind == "run_command"
    assert cmd_op.pre_existing is True
    assert ["claude", "plugin", "install", "caveman"] not in fake_runner.calls


# ---------------------------------------------------------------------------
# verify hook (unit)
# ---------------------------------------------------------------------------


def test_verify_succeeds_when_block_and_caveman_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    _redirect_claude_home(monkeypatch, tmp_path)
    fake_runner = FakeRunnerWithCaveman(caveman_installed=False)
    _patch_subprocess_run(monkeypatch, fake_runner)

    module = _token_module()
    state = _state_with_claude(tmp_path / "vault")
    ctx = _make_ctx(state=state, fake_runner=fake_runner)

    module.install(ctx)
    module.verify(ctx)


def test_verify_raises_when_caveman_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    _redirect_claude_home(monkeypatch, tmp_path)
    fake_runner = FakeRunnerWithCaveman(caveman_installed=False)
    _patch_subprocess_run(monkeypatch, fake_runner)

    module = _token_module()
    state = _state_with_claude(tmp_path / "vault")
    ctx = _make_ctx(state=state, fake_runner=fake_runner)

    module.install(ctx)
    # Simulate caveman being removed out-of-band.
    fake_runner.caveman_installed = False

    with pytest.raises(RuntimeError, match="caveman plugin is not installed"):
        module.verify(ctx)


def test_verify_raises_when_rules_file_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    _redirect_claude_home(monkeypatch, tmp_path)
    # Caveman pretends to be installed but the rules file was never written.
    fake_runner = FakeRunnerWithCaveman(caveman_installed=True)
    _patch_subprocess_run(monkeypatch, fake_runner)

    module = _token_module()
    state = _state_with_claude(tmp_path / "vault")
    ctx = _make_ctx(state=state, fake_runner=fake_runner)

    with pytest.raises(RuntimeError, match="rules file is missing"):
        module.verify(ctx)


def test_verify_raises_when_block_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    rules_path = _redirect_claude_home(monkeypatch, tmp_path)
    fake_runner = FakeRunnerWithCaveman(caveman_installed=False)
    _patch_subprocess_run(monkeypatch, fake_runner)

    module = _token_module()
    state = _state_with_claude(tmp_path / "vault")
    ctx = _make_ctx(state=state, fake_runner=fake_runner)

    module.install(ctx)
    # Strip the managed block while leaving the file in place.
    rules_path.write_text("# user notes only\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="block missing"):
        module.verify(ctx)


def test_verify_raises_when_block_modified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deliberate-corruption test: edits inside the block must be caught."""
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    rules_path = _redirect_claude_home(monkeypatch, tmp_path)
    fake_runner = FakeRunnerWithCaveman(caveman_installed=False)
    _patch_subprocess_run(monkeypatch, fake_runner)

    module = _token_module()
    state = _state_with_claude(tmp_path / "vault")
    ctx = _make_ctx(state=state, fake_runner=fake_runner)

    module.install(ctx)

    rules_path.write_text(
        "<!-- >>> cerebro:plugin:claude-code-token-optimization >>> -->\n"
        "user-edited content; not what Cerebro wrote\n"
        "<!-- <<< cerebro:plugin:claude-code-token-optimization <<< -->\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="modified outside Cerebro"):
        module.verify(ctx)


# ---------------------------------------------------------------------------
# Drift detection: doctor catches a deliberately corrupted managed block
# ---------------------------------------------------------------------------


def _materialize_in_tree(tmp_path: Path) -> Path:
    """Copy the in-tree plugins this test exercises into ``tmp_path``."""
    root = tmp_path / "in-tree"
    for src in (_VSCODE_DIR, _CLAUDE_DIR, _TOKEN_DIR):
        target = root / src.name
        target.mkdir(parents=True)
        for entry in src.rglob("*"):
            if entry.is_file():
                rel = entry.relative_to(src)
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(entry.read_bytes())
    return root


def _patch_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    import cerebro.runtime.platform as platform_mod

    monkeypatch.setattr(platform_mod, "current_platform", lambda: Platform.MACOS)


def _patch_engine_subprocess(
    monkeypatch: pytest.MonkeyPatch, fake_runner: FakeRunnerWithCaveman
) -> None:
    import cerebro.runtime.engine as engine_mod

    monkeypatch.setattr(engine_mod.subprocess, "run", fake_runner)


def _patch_obsidian_runner(
    monkeypatch: pytest.MonkeyPatch, fake_runner: FakeRunnerWithCaveman
) -> None:
    import cerebro.runtime.obsidian as obsidian_mod

    monkeypatch.setattr(obsidian_mod, "_default_runner", fake_runner)


def _patch_default_in_tree(monkeypatch: pytest.MonkeyPatch, in_tree: Path) -> None:
    import cerebro.plugins.loader as loader_mod

    monkeypatch.setattr(loader_mod, "default_in_tree_root", lambda: in_tree)


def _patch_which(
    monkeypatch: pytest.MonkeyPatch, *, claude: bool, code: bool = True
) -> None:
    def fake_which(name: str) -> str | None:
        if name == "claude":
            return "/fake/bin/claude" if claude else None
        if name == "code":
            return "/fake/bin/code" if code else None
        return None

    monkeypatch.setattr(shutil, "which", fake_which)


def _full_install_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path, FakeBrewPackageManager, FakeRunnerWithCaveman]:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))

    _patch_platform(monkeypatch)
    fake_runner = FakeRunnerWithCaveman(caveman_installed=False)
    _patch_subprocess_run(monkeypatch, fake_runner)
    _patch_engine_subprocess(monkeypatch, fake_runner)
    _patch_obsidian_runner(monkeypatch, fake_runner)
    _patch_which(monkeypatch, claude=False, code=True)

    in_tree = _materialize_in_tree(tmp_path)
    _patch_default_in_tree(monkeypatch, in_tree)
    rules_path = _redirect_claude_home(monkeypatch, tmp_path)

    pm = FakeBrewPackageManager()

    from cerebro.state import save_state

    vault = tmp_path / "ObsidianVault"
    save_state(
        CerebroState(core_version="0.1.0", vault_path=vault),
        home / "state.yaml",
    )

    return home, in_tree, rules_path, pm, fake_runner


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------


def test_engine_install_then_uninstall_full_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, in_tree, rules_path, pm, fake_runner = _full_install_setup(
        tmp_path, monkeypatch
    )
    components = PlatformComponents(
        platform=Platform.MACOS,
        package_manager=pm,
        scheduler=FakeScheduler(),
    )
    auth = RecordingAuthHandoff()

    engine_install(
        "vscode",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
    )
    engine_install(
        "claude-code",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
    )
    engine_install(
        "claude-code-token-optimization",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
    )

    body = rules_path.read_text(encoding="utf-8")
    # Both blocks coexist in the same CLAUDE.md.
    assert ">>> cerebro:plugin:claude-code >>>" in body
    assert ">>> cerebro:plugin:claude-code-token-optimization >>>" in body
    # Token-optimization content landed.
    assert "Avoid platitudes" in body
    assert "10th response" in body
    # Caveman was installed via the assumed CLI mechanism.
    assert ["claude", "plugin", "install", "caveman"] in fake_runner.calls
    assert fake_runner.caveman_installed is True

    engine_uninstall(
        "claude-code-token-optimization",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
    )

    body_after = rules_path.read_text(encoding="utf-8")
    # Our block is gone.
    assert ">>> cerebro:plugin:claude-code-token-optimization >>>" not in body_after
    # Sibling claude-code block still intact.
    assert ">>> cerebro:plugin:claude-code >>>" in body_after
    # Caveman was uninstalled (we installed it, so the inverse runs).
    assert ["claude", "plugin", "uninstall", "caveman"] in fake_runner.calls
    assert fake_runner.caveman_installed is False


def test_engine_install_is_rejected_when_already_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cerebro.runtime.engine import PluginAlreadyInstalledError

    home, in_tree, _rules_path, pm, _fake_runner = _full_install_setup(
        tmp_path, monkeypatch
    )
    components = PlatformComponents(
        platform=Platform.MACOS,
        package_manager=pm,
        scheduler=FakeScheduler(),
    )
    auth = RecordingAuthHandoff()

    engine_install(
        "vscode",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
    )
    engine_install(
        "claude-code",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
    )
    engine_install(
        "claude-code-token-optimization",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
    )

    with pytest.raises(PluginAlreadyInstalledError):
        engine_install(
            "claude-code-token-optimization",
            home=home,
            in_tree_root=in_tree,
            taps_root=tmp_path / "taps",
            components=components,
            auth=auth,
        )


def test_doctor_detects_drift_in_managed_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end drift detection: edit inside the block, doctor catches it."""
    from cerebro.state import load_state

    home, in_tree, rules_path, pm, _fake_runner = _full_install_setup(
        tmp_path, monkeypatch
    )
    components = PlatformComponents(
        platform=Platform.MACOS,
        package_manager=pm,
        scheduler=FakeScheduler(),
    )
    auth = RecordingAuthHandoff()

    engine_install(
        "vscode",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
    )
    engine_install(
        "claude-code",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
    )
    engine_install(
        "claude-code-token-optimization",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
    )

    # Sanity check: doctor reports clean before corruption.
    state = load_state(home / "state.yaml")
    record = next(
        p for p in state.installed_plugins if p.name == "claude-code-token-optimization"
    )
    clean = check_plugin(record, home=home, components=components)
    assert not clean.is_drifted

    # Hand-edit the managed block content.
    body = rules_path.read_text(encoding="utf-8")
    open_marker = "<!-- >>> cerebro:plugin:claude-code-token-optimization >>> -->"
    close_marker = "<!-- <<< cerebro:plugin:claude-code-token-optimization <<< -->"
    assert open_marker in body and close_marker in body
    corrupted = (
        body.split(open_marker)[0]
        + open_marker
        + "\nuser-edited content; not what Cerebro wrote\n"
        + close_marker
        + body.split(close_marker, 1)[1]
    )
    rules_path.write_text(corrupted, encoding="utf-8")

    drifted = check_plugin(record, home=home, components=components)
    assert drifted.is_drifted
    drifted_checks = drifted.drifted_checks()
    assert any(
        c.kind == "add_block"
        and c.status is OperationStatus.DRIFTED
        and "claude-code-token-optimization" in c.target
        for c in drifted_checks
    ), f"expected drifted add_block check, got {drifted_checks!r}"
