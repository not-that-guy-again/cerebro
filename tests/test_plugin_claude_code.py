"""Tests for the in-tree Claude Code agent plugin.

Covers the unit-level hook contracts — the IDE precondition, the
sequenced install (Obsidian helper, npm install, auth handoff, managed
block, companion extensions), reconfigure-on-new-IDE, and verify — plus
end-to-end install + uninstall through the engine. npm, the ``claude``
CLI, the ``code`` CLI, and brew/apt/pacman are all faked; no real
system mutation happens.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from cerebro.models import CerebroState, InstalledPlugin, PluginManifest
from cerebro.plugins.loader import (
    IN_TREE_SOURCE,
    DiscoveredPlugin,
    load_plugin_module,
)
from cerebro.runtime.context import HookContext, build_context
from cerebro.runtime.engine import install as engine_install
from cerebro.runtime.engine import uninstall as engine_uninstall
from cerebro.runtime.platform import InstallResult, Platform, PlatformComponents
from cerebro.state import load_plugin_manifest, load_state
from tests.test_recorder import FakeScheduler

_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "cerebro" / "plugins"
_CLAUDE_DIR = _PLUGINS_DIR / "claude-code"
_VSCODE_DIR = _PLUGINS_DIR / "vscode"
_CLAUDE_BIN = "/fake/bin/claude"
_CODE_BIN = "/fake/bin/code"
_VSCODE_EXTENSION_ID = "anthropic.claude-code"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeBrewPackageManager:
    def __init__(
        self,
        *,
        cask_installed: set[str] | None = None,
        installed: set[str] | None = None,
    ) -> None:
        self.cask_installed: set[str] = set(cask_installed or set())
        self.installed: set[str] = set(installed or set())
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


class FakeRunner:
    """Fake subprocess.run-style callable for npm + code + claude commands."""

    def __init__(
        self,
        *,
        installed_extensions: set[str] | None = None,
        version_returncode: int = 0,
        version_stdout: str = "1.0.0\n",
    ) -> None:
        self.installed_extensions: set[str] = set(installed_extensions or set())
        self.calls: list[list[str]] = []
        self.version_returncode = version_returncode
        self.version_stdout = version_stdout

    def __call__(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        capture_output: bool = False,
        text: bool = False,
    ) -> SimpleNamespace:
        del capture_output, text
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
        if argv_list[:3] == ["npm", "install", "-g"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv_list[:3] == ["npm", "uninstall", "-g"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv_list[:1] == ["brew"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv_list[:1] == ["sudo"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if check:
            raise AssertionError(f"unexpected invocation: {argv_list}")
        return SimpleNamespace(returncode=1, stdout="", stderr="unknown")


class RecordingAuthHandoff:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def request(self, *, message: str) -> None:
        self.messages.append(message)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _claude_module() -> ModuleType:
    discovered = DiscoveredPlugin(
        manifest=load_plugin_manifest(_CLAUDE_DIR),
        module_path=_CLAUDE_DIR,
        source=IN_TREE_SOURCE,
    )
    return load_plugin_module(discovered)


def _claude_manifest() -> PluginManifest:
    return load_plugin_manifest(_CLAUDE_DIR)


def _vscode_manifest() -> PluginManifest:
    return load_plugin_manifest(_VSCODE_DIR)


def _make_ctx(
    *,
    package_manager: Any,
    fake_runner: FakeRunner | None = None,
    auth: RecordingAuthHandoff | None = None,
    state: CerebroState | None = None,
    when: datetime | None = None,
) -> HookContext:
    plugin = _claude_manifest()
    vscode_manifest = _vscode_manifest()
    if state is None:
        state = CerebroState(
            core_version="0.1.0",
            vault_path=Path("/tmp/vault-fake"),
            installed_plugins=[
                InstalledPlugin(
                    name="vscode",
                    source=IN_TREE_SOURCE,
                    version="1.0.0",
                    installed_at=datetime(2026, 4, 27, 11, 0, 0, tzinfo=UTC),
                    enabled=True,
                ),
            ],
        )

    def manifest_lookup(name: str) -> PluginManifest | None:
        if name == "vscode":
            return vscode_manifest
        if name == "claude-code":
            return plugin
        return None

    return build_context(
        plugin=plugin,
        state=state,
        package_manager=package_manager,
        scheduler=FakeScheduler(),
        when=when or datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        auth=auth,
        manifest_lookup=manifest_lookup,
        command_runner=fake_runner,
    )


def _patch_platform(monkeypatch: pytest.MonkeyPatch, plat: Platform) -> None:
    import cerebro.runtime.platform as platform_mod

    monkeypatch.setattr(platform_mod, "current_platform", lambda: plat)


def _patch_which(
    monkeypatch: pytest.MonkeyPatch,
    *,
    claude: bool,
    code: bool = True,
) -> None:
    """Patch shutil.which so claude/code resolve as instructed."""
    import shutil

    def fake_which(name: str) -> str | None:
        if name == "claude":
            return _CLAUDE_BIN if claude else None
        if name == "code":
            return _CODE_BIN if code else None
        return None

    monkeypatch.setattr(shutil, "which", fake_which)


def _patch_subprocess_run(
    monkeypatch: pytest.MonkeyPatch, fake_runner: FakeRunner
) -> None:
    """Route the global subprocess.run through ``fake_runner``.

    Both the claude-code plugin (verify) and the freshly-loaded vscode
    plugin module (companion-extension hooks) call subprocess.run from
    their own module-level imports of stdlib subprocess; patching the
    stdlib module covers all of them.
    """
    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_runner)


def _patch_obsidian_runner(
    monkeypatch: pytest.MonkeyPatch, fake_runner: FakeRunner
) -> None:
    """Make obsidian.ensure_obsidian_installed use our runner.

    The helper takes an injectable ``runner``, but the plugin calls it
    without one. Patch the module-level default to keep the plugin's
    call signature realistic.
    """
    import cerebro.runtime.obsidian as obsidian_mod

    monkeypatch.setattr(obsidian_mod, "_default_runner", fake_runner)


# ---------------------------------------------------------------------------
# rules_file_path
# ---------------------------------------------------------------------------


def test_rules_file_path_returns_user_claude_md() -> None:
    module = _claude_module()
    pm = FakeBrewPackageManager()
    ctx = _make_ctx(package_manager=pm)

    result = module.rules_file_path(ctx)

    assert result == Path("~/.claude/CLAUDE.md").expanduser()


# ---------------------------------------------------------------------------
# install hook (unit)
# ---------------------------------------------------------------------------


def test_install_raises_when_no_ide_plugin_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _claude_module()
    _patch_platform(monkeypatch, Platform.MACOS)
    pm = FakeBrewPackageManager()
    state = CerebroState(
        core_version="0.1.0",
        vault_path=Path("/tmp/vault-fake"),
        installed_plugins=[],
    )
    ctx = _make_ctx(package_manager=pm, state=state)

    with pytest.raises(RuntimeError, match="at least one IDE plugin"):
        module.install(ctx)
    # No side effects recorded when we bail early.
    assert ctx.manifest.operations == []


def test_install_runs_full_sequence_on_macos(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _claude_module()
    _patch_platform(monkeypatch, Platform.MACOS)
    fake_runner = FakeRunner()
    _patch_obsidian_runner(monkeypatch, fake_runner)
    _patch_subprocess_run(monkeypatch, fake_runner)
    _patch_which(monkeypatch, claude=False, code=True)

    pm = FakeBrewPackageManager()
    auth = RecordingAuthHandoff()
    vault = tmp_path / "ObsidianVault"
    state = CerebroState(
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
        ],
    )
    ctx = _make_ctx(
        package_manager=pm,
        fake_runner=fake_runner,
        auth=auth,
        state=state,
    )

    rules_path = tmp_path / "claude" / "CLAUDE.md"
    monkeypatch.setattr(module, "rules_file_path", lambda _ctx: rules_path)

    module.install(ctx)

    # Obsidian was installed via brew --cask (recorder bypassed).
    assert ["brew", "install", "--cask", "obsidian"] in fake_runner.calls
    # Vault scaffold present.
    assert vault.is_dir()
    for sub in ("repos", "briefings", "decisions-mirror", "templates"):
        assert (vault / sub).is_dir()
    assert (vault / "README.md").exists()
    # Auth handoff was requested with explicit `claude login` instructions.
    assert any("claude login" in m for m in auth.messages)
    # Managed block written to the rules file.
    assert rules_path.exists()
    body = rules_path.read_text(encoding="utf-8")
    assert ">>> cerebro:plugin:claude-code >>>" in body
    assert str(vault) in body
    # Companion extension installed in vscode.
    assert _VSCODE_EXTENSION_ID in fake_runner.installed_extensions

    # Recorded operations: npm install, add_block, run_command (extension).
    kinds = [op.kind for op in ctx.manifest.operations]
    assert kinds == ["run_command", "add_block", "run_command"]
    npm_op = ctx.manifest.operations[0]
    assert npm_op.parameters["argv"] == [
        "npm", "install", "-g", "@anthropic-ai/claude-code",
    ]
    assert npm_op.inverse["argv"] == [
        "npm", "uninstall", "-g", "@anthropic-ai/claude-code",
    ]
    assert npm_op.pre_existing is False
    block_op = ctx.manifest.operations[1]
    assert block_op.parameters["plugin_name"] == "claude-code"
    ext_op = ctx.manifest.operations[2]
    assert ext_op.parameters["argv"][1] == "--install-extension"
    assert ext_op.parameters["argv"][2] == _VSCODE_EXTENSION_ID


def test_install_records_pre_existing_when_claude_already_on_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _claude_module()
    _patch_platform(monkeypatch, Platform.MACOS)
    fake_runner = FakeRunner()
    _patch_obsidian_runner(monkeypatch, fake_runner)
    _patch_subprocess_run(monkeypatch, fake_runner)
    _patch_which(monkeypatch, claude=True, code=True)

    pm = FakeBrewPackageManager(cask_installed={"obsidian"})
    auth = RecordingAuthHandoff()
    vault = tmp_path / "ObsidianVault"
    state = CerebroState(
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
        ],
    )
    ctx = _make_ctx(
        package_manager=pm, fake_runner=fake_runner, auth=auth, state=state
    )
    rules_path = tmp_path / "claude" / "CLAUDE.md"
    monkeypatch.setattr(module, "rules_file_path", lambda _ctx: rules_path)

    module.install(ctx)

    npm_op = ctx.manifest.operations[0]
    assert npm_op.kind == "run_command"
    assert npm_op.pre_existing is True
    # Pre-existing means the install command was NOT shelled out.
    assert ["npm", "install", "-g", "@anthropic-ai/claude-code"] not in fake_runner.calls
    # And brew was NOT shelled out either (Obsidian already present).
    assert ["brew", "install", "--cask", "obsidian"] not in fake_runner.calls


def test_install_skips_unknown_ide_plugin_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An installed IDE plugin we have no extension ID for is logged-and-skipped, not fatal."""
    module = _claude_module()
    _patch_platform(monkeypatch, Platform.MACOS)
    fake_runner = FakeRunner()
    _patch_obsidian_runner(monkeypatch, fake_runner)
    _patch_subprocess_run(monkeypatch, fake_runner)
    _patch_which(monkeypatch, claude=False, code=True)

    pm = FakeBrewPackageManager()
    auth = RecordingAuthHandoff()
    vault = tmp_path / "ObsidianVault"
    state = CerebroState(
        core_version="0.1.0",
        vault_path=vault,
        installed_plugins=[
            InstalledPlugin(
                name="jetbrains",  # hypothetical, no entry in _COMPANION_EXTENSION_IDS
                source=IN_TREE_SOURCE,
                version="1.0.0",
                installed_at=datetime(2026, 4, 27, 11, 0, 0, tzinfo=UTC),
                enabled=True,
            ),
        ],
    )

    plugin = _claude_manifest()
    jetbrains_manifest = PluginManifest(
        name="jetbrains",
        version="1.0.0",
        type="ide",
        description="hypothetical",
        min_core_version="0.1.0",
        supported_platforms=["macos"],
    )

    def manifest_lookup(name: str) -> PluginManifest | None:
        if name == "jetbrains":
            return jetbrains_manifest
        if name == "claude-code":
            return plugin
        return None

    ctx = build_context(
        plugin=plugin,
        state=state,
        package_manager=pm,
        scheduler=FakeScheduler(),
        when=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        auth=auth,
        manifest_lookup=manifest_lookup,
        command_runner=fake_runner,
    )
    rules_path = tmp_path / "claude" / "CLAUDE.md"
    monkeypatch.setattr(module, "rules_file_path", lambda _ctx: rules_path)

    module.install(ctx)

    # No companion extension recorded — the unknown IDE was skipped.
    kinds = [op.kind for op in ctx.manifest.operations]
    assert kinds == ["run_command", "add_block"]


# ---------------------------------------------------------------------------
# configure hook
# ---------------------------------------------------------------------------


def test_configure_installs_companion_for_each_ide(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _claude_module()
    _patch_platform(monkeypatch, Platform.MACOS)
    fake_runner = FakeRunner()
    _patch_subprocess_run(monkeypatch, fake_runner)
    _patch_which(monkeypatch, claude=True, code=True)

    pm = FakeBrewPackageManager()
    ctx = _make_ctx(package_manager=pm, fake_runner=fake_runner)

    module.configure(ctx)

    # Companion extension installed; one run_command op recorded.
    assert _VSCODE_EXTENSION_ID in fake_runner.installed_extensions
    assert [op.kind for op in ctx.manifest.operations] == ["run_command"]


def test_configure_records_pre_existing_when_extension_already_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _claude_module()
    _patch_platform(monkeypatch, Platform.MACOS)
    fake_runner = FakeRunner(installed_extensions={_VSCODE_EXTENSION_ID})
    _patch_subprocess_run(monkeypatch, fake_runner)
    _patch_which(monkeypatch, claude=True, code=True)

    pm = FakeBrewPackageManager()
    ctx = _make_ctx(package_manager=pm, fake_runner=fake_runner)

    module.configure(ctx)

    op = ctx.manifest.operations[-1]
    assert op.pre_existing is True
    # Idempotent: no shelling out happened to install something that was there.
    assert (
        [_CODE_BIN, "--install-extension", _VSCODE_EXTENSION_ID]
        not in fake_runner.calls
    )


def test_configure_with_no_ides_is_no_op() -> None:
    module = _claude_module()
    pm = FakeBrewPackageManager()
    state = CerebroState(
        core_version="0.1.0",
        vault_path=Path("/tmp/vault-fake"),
        installed_plugins=[],
    )
    ctx = _make_ctx(package_manager=pm, state=state)

    module.configure(ctx)

    assert ctx.manifest.operations == []


# ---------------------------------------------------------------------------
# verify hook
# ---------------------------------------------------------------------------


def test_verify_succeeds_when_block_present_and_cli_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _claude_module()
    fake_runner = FakeRunner()
    _patch_which(monkeypatch, claude=True)
    _patch_subprocess_run(monkeypatch, fake_runner)

    pm = FakeBrewPackageManager()
    vault = tmp_path / "ObsidianVault"
    state = CerebroState(
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
        ],
    )
    ctx = _make_ctx(package_manager=pm, fake_runner=fake_runner, state=state)

    rules_path = tmp_path / "claude" / "CLAUDE.md"
    monkeypatch.setattr(module, "rules_file_path", lambda _ctx: rules_path)
    rules_path.parent.mkdir(parents=True)
    expected = module._block_content(vault)
    rules_path.write_text(
        f"<!-- >>> cerebro:plugin:claude-code >>> -->\n{expected}\n"
        "<!-- <<< cerebro:plugin:claude-code <<< -->\n",
        encoding="utf-8",
    )

    module.verify(ctx)

    assert [_CLAUDE_BIN, "--version"] in fake_runner.calls


def test_verify_raises_when_cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _claude_module()
    _patch_which(monkeypatch, claude=False)
    pm = FakeBrewPackageManager()
    ctx = _make_ctx(package_manager=pm)

    with pytest.raises(RuntimeError, match="not on PATH"):
        module.verify(ctx)


def test_verify_raises_when_cli_version_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _claude_module()
    fake_runner = FakeRunner(version_returncode=2, version_stdout="bad")
    _patch_which(monkeypatch, claude=True)
    _patch_subprocess_run(monkeypatch, fake_runner)
    pm = FakeBrewPackageManager()
    ctx = _make_ctx(package_manager=pm, fake_runner=fake_runner)

    with pytest.raises(RuntimeError, match="claude --version"):
        module.verify(ctx)


def test_verify_raises_when_rules_file_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _claude_module()
    fake_runner = FakeRunner()
    _patch_which(monkeypatch, claude=True)
    _patch_subprocess_run(monkeypatch, fake_runner)
    pm = FakeBrewPackageManager()
    ctx = _make_ctx(package_manager=pm, fake_runner=fake_runner)

    rules_path = tmp_path / "missing" / "CLAUDE.md"
    monkeypatch.setattr(module, "rules_file_path", lambda _ctx: rules_path)

    with pytest.raises(RuntimeError, match="rules file is missing"):
        module.verify(ctx)


def test_verify_raises_when_block_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _claude_module()
    fake_runner = FakeRunner()
    _patch_which(monkeypatch, claude=True)
    _patch_subprocess_run(monkeypatch, fake_runner)
    pm = FakeBrewPackageManager()
    ctx = _make_ctx(package_manager=pm, fake_runner=fake_runner)

    rules_path = tmp_path / "claude" / "CLAUDE.md"
    rules_path.parent.mkdir()
    rules_path.write_text("# user notes\n", encoding="utf-8")
    monkeypatch.setattr(module, "rules_file_path", lambda _ctx: rules_path)

    with pytest.raises(RuntimeError, match="managed claude-code block missing"):
        module.verify(ctx)


def test_verify_raises_when_block_modified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _claude_module()
    fake_runner = FakeRunner()
    _patch_which(monkeypatch, claude=True)
    _patch_subprocess_run(monkeypatch, fake_runner)
    pm = FakeBrewPackageManager()
    vault = tmp_path / "vault"
    state = CerebroState(
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
        ],
    )
    ctx = _make_ctx(package_manager=pm, fake_runner=fake_runner, state=state)

    rules_path = tmp_path / "claude" / "CLAUDE.md"
    monkeypatch.setattr(module, "rules_file_path", lambda _ctx: rules_path)
    rules_path.parent.mkdir(parents=True)
    rules_path.write_text(
        "<!-- >>> cerebro:plugin:claude-code >>> -->\n"
        "user-edited content; not what Cerebro wrote\n"
        "<!-- <<< cerebro:plugin:claude-code <<< -->\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="modified outside Cerebro"):
        module.verify(ctx)


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------


def _vscode_and_claude_in_tree(tmp_path: Path) -> Path:
    """Materialize the in-tree vscode and claude-code plugins into ``tmp_path``."""
    root = tmp_path / "in-tree"
    for src in (_VSCODE_DIR, _CLAUDE_DIR):
        target = root / src.name
        target.mkdir(parents=True)
        for child in src.iterdir():
            if child.is_file():
                target.joinpath(child.name).write_bytes(child.read_bytes())
    return root


def _patch_engine_subprocess(
    monkeypatch: pytest.MonkeyPatch, fake_runner: FakeRunner
) -> None:
    """Route the engine's subprocess.run (used during run_command inversion) through the fake."""
    import cerebro.runtime.engine as engine_mod

    monkeypatch.setattr(engine_mod.subprocess, "run", fake_runner)


def _patch_default_in_tree(
    monkeypatch: pytest.MonkeyPatch, in_tree: Path
) -> None:
    """Make ``discover_plugins()`` (called from claude-code's install hook
    with no in_tree_root override) look at ``in_tree``."""
    import cerebro.plugins.loader as loader_mod

    monkeypatch.setattr(loader_mod, "default_in_tree_root", lambda: in_tree)


def test_engine_install_then_uninstall_full_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))

    _patch_platform(monkeypatch, Platform.MACOS)
    fake_runner = FakeRunner()
    _patch_subprocess_run(monkeypatch, fake_runner)
    _patch_engine_subprocess(monkeypatch, fake_runner)
    _patch_obsidian_runner(monkeypatch, fake_runner)
    _patch_which(monkeypatch, claude=False, code=True)

    in_tree = _vscode_and_claude_in_tree(tmp_path)
    _patch_default_in_tree(monkeypatch, in_tree)

    # Vault path through state; pre-set so the engine's empty-state fallback
    # doesn't try to use ~/.cerebro/vault.
    vault = tmp_path / "ObsidianVault"
    rules_path = tmp_path / ".claude" / "CLAUDE.md"

    pm = FakeBrewPackageManager()
    components = PlatformComponents(
        platform=Platform.MACOS,
        package_manager=pm,
        scheduler=FakeScheduler(),
    )
    auth = RecordingAuthHandoff()

    # Pre-create state.yaml so vault_path is the test's path.
    from cerebro.state import save_state
    save_state(
        CerebroState(core_version="0.1.0", vault_path=vault),
        home / "state.yaml",
    )

    # Patch the loaded claude-code module's rules_file_path so the test
    # writes to a temp path instead of ~/.claude/CLAUDE.md. The engine
    # reloads modules each time, so we monkeypatch the module dict via
    # importlib's loaded module after install starts. Easiest: monkey-
    # patch Path.expanduser for the duration so ~/.claude resolves under
    # tmp_path.
    real_expanduser = Path.expanduser

    def fake_expanduser(self: Path) -> Path:
        s = str(self)
        if s.startswith("~/.claude"):
            return tmp_path / s[2:]  # strip the leading "~/"
        return real_expanduser(self)

    monkeypatch.setattr(Path, "expanduser", fake_expanduser)

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

    state = load_state(home / "state.yaml")
    assert sorted(p.name for p in state.installed_plugins) == ["claude-code", "vscode"]
    # End-to-end side effects:
    assert "visual-studio-code" in pm.cask_installed
    assert vault.is_dir()
    assert (vault / "repos").is_dir()
    assert _VSCODE_EXTENSION_ID in fake_runner.installed_extensions
    assert rules_path.exists()
    assert ">>> cerebro:plugin:claude-code >>>" in rules_path.read_text(encoding="utf-8")
    # Auth handoff was requested.
    assert any("claude login" in m for m in auth.messages)

    engine_uninstall(
        "claude-code",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
    )

    # Uninstall reverses the recorded operations:
    assert _VSCODE_EXTENSION_ID not in fake_runner.installed_extensions
    assert ["npm", "uninstall", "-g", "@anthropic-ai/claude-code"] in fake_runner.calls
    if rules_path.exists():
        # If the file still exists, the managed block must be gone.
        assert ">>> cerebro:plugin:claude-code >>>" not in rules_path.read_text(
            encoding="utf-8"
        )
    # Obsidian and the vault remain — they are user data per ADR-0013/SPEC-11.
    # The recorder was bypassed for these, so the engine never even attempts
    # to invert them; assert no `brew uninstall --cask obsidian` was issued
    # and the vault is intact on disk.
    assert ["brew", "uninstall", "--cask", "obsidian"] not in fake_runner.calls
    assert vault.is_dir()
    assert (vault / "repos").is_dir()


def test_engine_reconfigure_when_new_ide_added(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that adding a hypothetical second IDE plugin triggers
    claude-code's configure hook, and configure is a no-op for IDEs we
    don't have an extension ID for."""
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))

    _patch_platform(monkeypatch, Platform.MACOS)
    fake_runner = FakeRunner()
    _patch_subprocess_run(monkeypatch, fake_runner)
    _patch_engine_subprocess(monkeypatch, fake_runner)
    _patch_obsidian_runner(monkeypatch, fake_runner)
    _patch_which(monkeypatch, claude=False, code=True)

    in_tree = _vscode_and_claude_in_tree(tmp_path)
    _patch_default_in_tree(monkeypatch, in_tree)

    vault = tmp_path / "ObsidianVault"
    pm = FakeBrewPackageManager()
    components = PlatformComponents(
        platform=Platform.MACOS,
        package_manager=pm,
        scheduler=FakeScheduler(),
    )
    auth = RecordingAuthHandoff()

    from cerebro.state import save_state
    save_state(
        CerebroState(core_version="0.1.0", vault_path=vault),
        home / "state.yaml",
    )

    real_expanduser = Path.expanduser

    def fake_expanduser(self: Path) -> Path:
        s = str(self)
        if s.startswith("~/.claude"):
            return tmp_path / s[2:]
        return real_expanduser(self)

    monkeypatch.setattr(Path, "expanduser", fake_expanduser)

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

    # Add a hypothetical second IDE plugin to the in-tree root.
    fake_ide_dir = in_tree / "fake-ide"
    fake_ide_dir.mkdir()
    (fake_ide_dir / "plugin.yaml").write_text(
        "name: fake-ide\n"
        "version: 1.0.0\n"
        "type: ide\n"
        "description: hypothetical second IDE\n"
        "min_core_version: 0.1.0\n"
        "supported_platforms: [macos]\n"
        "hooks_declared: [install]\n",
        encoding="utf-8",
    )
    (fake_ide_dir / "plugin.py").write_text(
        "def install(ctx):\n    pass\n",
        encoding="utf-8",
    )

    # Capture how many companion installs we did pre-reconfigure.
    extensions_before = set(fake_runner.installed_extensions)

    engine_install(
        "fake-ide",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
    )

    # Engine ran claude-code's configure (it targets ide). For a fake-ide
    # we have no extension ID, so configure logged-and-skipped — meaning
    # no NEW companion ops were shelled out, and the existing vscode
    # extension is still present.
    assert fake_runner.installed_extensions == extensions_before
    state = load_state(home / "state.yaml")
    assert "fake-ide" in {p.name for p in state.installed_plugins}
