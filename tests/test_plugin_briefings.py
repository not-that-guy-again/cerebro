"""Tests for the in-tree briefings workflow plugin.

Covers:
- The unit-level install / configure / verify / uninstall hook contracts
  against a synthetic agent module (no claude-code dependency in unit
  tests so the surface contract stays explicit).
- Integration with the engine: install, reconfigure on new agent,
  uninstall via manifest replay — exercised through the engine using
  both the briefings plugin and a synthetic agent.
- The published Claude Code surface (``register_slash_command`` /
  ``unregister_slash_command`` / ``slash_command_path``) end-to-end.
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
from cerebro.runtime.engine import (
    install as engine_install,
)
from cerebro.runtime.engine import (
    uninstall as engine_uninstall,
)
from cerebro.runtime.platform import InstallResult, Platform, PlatformComponents
from cerebro.state import load_plugin_manifest, load_state, save_state
from tests.test_recorder import FakeScheduler

_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "cerebro" / "plugins"
_BRIEFINGS_DIR = _PLUGINS_DIR / "briefings"

_SLASH_COMMAND_NAMES = (
    "daily-briefing",
    "meeting-prep",
    "daily-summary",
    "weekly-summary",
    "monthly-summary",
    "quarterly-summary",
    "annual-summary",
)


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class FakePackageManager:
    def __init__(self) -> None:
        self.installed: set[str] = set()
        self.cask_installed: set[str] = set()

    def is_installed(self, package: str) -> bool:
        return package in self.installed

    def install(self, package: str) -> InstallResult:
        already = package in self.installed
        self.installed.add(package)
        return InstallResult(already_installed=already)

    def uninstall(self, package: str) -> None:
        self.installed.discard(package)

    def is_cask_installed(self, package: str) -> bool:
        return package in self.cask_installed

    def install_cask(self, package: str) -> InstallResult:
        already = package in self.cask_installed
        self.cask_installed.add(package)
        return InstallResult(already_installed=already)

    def uninstall_cask(self, package: str) -> None:
        self.cask_installed.discard(package)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        capture_output: bool = False,
        text: bool = False,
    ) -> SimpleNamespace:
        del check, capture_output, text
        self.calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")


# A synthetic agent plugin that exposes the published surface SPEC-13
# adds (register_slash_command / unregister_slash_command /
# slash_command_path). The plugin lives in a tmp in-tree root so the
# loader can find it.
def _materialize_agent_plugin(
    in_tree: Path,
    *,
    name: str,
    commands_dir: Path,
) -> Path:
    plugin_dir = in_tree / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(
        f"name: {name}\n"
        "version: 1.0.0\n"
        "type: agent\n"
        f"description: synthetic agent for briefings tests\n"
        "min_core_version: 0.1.0\n"
        "supported_platforms: [macos, linux_apt, linux_pacman]\n"
        "hooks_declared: [install]\n",
        encoding="utf-8",
    )
    (plugin_dir / "plugin.py").write_text(
        "from __future__ import annotations\n"
        "from pathlib import Path\n"
        f"_COMMANDS_DIR = Path({str(commands_dir)!r})\n"
        "\n"
        "def install(ctx):\n"
        "    pass\n"
        "\n"
        "def slash_command_path(ctx, name):\n"
        "    del ctx\n"
        "    return _COMMANDS_DIR / f'{name}.md'\n"
        "\n"
        "def register_slash_command(ctx, name, script_path):\n"
        "    destination = slash_command_path(ctx, name)\n"
        "    content = Path(script_path).read_text(encoding='utf-8')\n"
        "    ctx.fs.write_file(destination, content)\n"
        "    return destination\n"
        "\n"
        "def unregister_slash_command(ctx, name):\n"
        "    destination = slash_command_path(ctx, name)\n"
        "    if not destination.exists():\n"
        "        return False\n"
        "    destination.unlink()\n"
        "    return True\n",
        encoding="utf-8",
    )
    return plugin_dir


def _briefings_module() -> ModuleType:
    discovered = DiscoveredPlugin(
        manifest=load_plugin_manifest(_BRIEFINGS_DIR),
        module_path=_BRIEFINGS_DIR,
        source=IN_TREE_SOURCE,
    )
    return load_plugin_module(discovered)


def _briefings_manifest() -> PluginManifest:
    return load_plugin_manifest(_BRIEFINGS_DIR)


def _make_ctx(
    *,
    state: CerebroState,
    manifest_lookup_extra: dict[str, PluginManifest] | None = None,
    when: datetime | None = None,
    scheduler: FakeScheduler | None = None,
    package_manager: Any = None,
) -> HookContext:
    plugin = _briefings_manifest()
    extras = dict(manifest_lookup_extra or {})

    def manifest_lookup(name: str) -> PluginManifest | None:
        if name == plugin.name:
            return plugin
        return extras.get(name)

    return build_context(
        plugin=plugin,
        state=state,
        package_manager=package_manager or FakePackageManager(),
        scheduler=scheduler or FakeScheduler(),
        when=when or datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        manifest_lookup=manifest_lookup,
    )


def _patch_in_tree_root(monkeypatch: pytest.MonkeyPatch, in_tree: Path) -> None:
    import cerebro.plugins.loader as loader_mod

    monkeypatch.setattr(loader_mod, "default_in_tree_root", lambda: in_tree)


# ---------------------------------------------------------------------------
# install hook (unit)
# ---------------------------------------------------------------------------


def test_install_raises_when_no_agent_plugin_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_tree = tmp_path / "in-tree"
    # Briefings present, no agent.
    (in_tree / "briefings").mkdir(parents=True)
    for child in _BRIEFINGS_DIR.rglob("*"):
        if child.is_file():
            relative = child.relative_to(_BRIEFINGS_DIR)
            target = in_tree / "briefings" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(child.read_bytes())
    _patch_in_tree_root(monkeypatch, in_tree)

    state = CerebroState(
        core_version="0.1.0",
        vault_path=tmp_path / "vault",
        installed_plugins=[],
    )
    ctx = _make_ctx(state=state)

    module = _briefings_module()
    with pytest.raises(RuntimeError, match="at least one agent plugin"):
        module.install(ctx)


def test_install_registers_seven_commands_and_three_tasks_per_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_tree = tmp_path / "in-tree"
    in_tree.mkdir()
    # Materialize the briefings plugin into the tmp in-tree root so the
    # plugin's internal ``discover_plugins(ctx.state)`` call can find it
    # alongside our synthetic agent. Without this the plugin self-import
    # collides with the in-tree copy used by the engine.
    target_briefings = in_tree / "briefings"
    target_briefings.mkdir()
    for child in _BRIEFINGS_DIR.rglob("*"):
        if child.is_file():
            relative = child.relative_to(_BRIEFINGS_DIR)
            dest = target_briefings / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(child.read_bytes())

    agent_commands_dir = tmp_path / "agent-cmds"
    agent_commands_dir.mkdir()
    _materialize_agent_plugin(
        in_tree, name="fake-agent", commands_dir=agent_commands_dir
    )
    _patch_in_tree_root(monkeypatch, in_tree)

    state = CerebroState(
        core_version="0.1.0",
        vault_path=tmp_path / "vault",
        installed_plugins=[
            InstalledPlugin(
                name="fake-agent",
                source=IN_TREE_SOURCE,
                version="1.0.0",
                installed_at=datetime(2026, 4, 27, 11, 0, 0, tzinfo=UTC),
                enabled=True,
            ),
        ],
    )
    fake_agent_manifest = load_plugin_manifest(in_tree / "fake-agent")
    scheduler = FakeScheduler()
    ctx = _make_ctx(
        state=state,
        manifest_lookup_extra={"fake-agent": fake_agent_manifest},
        scheduler=scheduler,
    )

    # We need the briefings module loaded from the SAME in-tree root the
    # engine + plugin use, otherwise discover_plugins inside the install
    # hook produces a different DiscoveredPlugin than the manifest under
    # test. Loading from the tmp root keeps everything consistent.
    discovered = DiscoveredPlugin(
        manifest=load_plugin_manifest(target_briefings),
        module_path=target_briefings,
        source=IN_TREE_SOURCE,
    )
    module = load_plugin_module(discovered)
    module.install(ctx)

    # Seven slash commands, each as a write_file op into the agent's commands dir.
    write_ops = [op for op in ctx.manifest.operations if op.kind == "write_file"]
    assert len(write_ops) == 7
    written_paths = {Path(op.parameters["path"]).name for op in write_ops}
    assert written_paths == {f"{n}.md" for n in _SLASH_COMMAND_NAMES}
    # All slash command files actually exist on disk.
    for name in _SLASH_COMMAND_NAMES:
        assert (agent_commands_dir / f"{name}.md").is_file()

    # Three scheduled tasks registered.
    register_ops = [op for op in ctx.manifest.operations if op.kind == "register_task"]
    assert {op.parameters["name"] for op in register_ops} == {
        "cerebro-briefings-daily-summary",
        "cerebro-briefings-weekly-summary",
        "cerebro-briefings-monthly-summary",
    }
    # Cadences come from the spec verbatim.
    by_name = {op.parameters["name"]: op.parameters for op in register_ops}
    assert by_name["cerebro-briefings-daily-summary"]["schedule"] == "daily@18:00"
    assert by_name["cerebro-briefings-weekly-summary"]["schedule"] == "weekly:fri@17:00"
    assert (
        by_name["cerebro-briefings-monthly-summary"]["schedule"]
        == "monthly:last-business-day@17:00"
    )
    # Each command shells `cerebro internal briefings-write <period>`.
    for op in register_ops:
        cmd = op.parameters["command"]
        assert "internal briefings-write" in cmd
    # And every task name is now registered with the (fake) scheduler.
    assert {
        "cerebro-briefings-daily-summary",
        "cerebro-briefings-weekly-summary",
        "cerebro-briefings-monthly-summary",
    } <= scheduler.registered


def test_install_skips_agent_without_register_slash_command_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_tree = tmp_path / "in-tree"
    in_tree.mkdir()
    target_briefings = in_tree / "briefings"
    target_briefings.mkdir()
    for child in _BRIEFINGS_DIR.rglob("*"):
        if child.is_file():
            relative = child.relative_to(_BRIEFINGS_DIR)
            dest = target_briefings / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(child.read_bytes())

    # Synthetic agent with no register_slash_command hook.
    plugin_dir = in_tree / "weird-agent"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        "name: weird-agent\n"
        "version: 1.0.0\n"
        "type: agent\n"
        "description: synthetic agent without slash command surface\n"
        "min_core_version: 0.1.0\n"
        "supported_platforms: [macos]\n"
        "hooks_declared: [install]\n",
        encoding="utf-8",
    )
    (plugin_dir / "plugin.py").write_text(
        "def install(ctx):\n    pass\n",
        encoding="utf-8",
    )
    _patch_in_tree_root(monkeypatch, in_tree)

    state = CerebroState(
        core_version="0.1.0",
        vault_path=tmp_path / "vault",
        installed_plugins=[
            InstalledPlugin(
                name="weird-agent",
                source=IN_TREE_SOURCE,
                version="1.0.0",
                installed_at=datetime(2026, 4, 27, 11, 0, 0, tzinfo=UTC),
                enabled=True,
            ),
        ],
    )
    weird_manifest = load_plugin_manifest(plugin_dir)
    ctx = _make_ctx(
        state=state, manifest_lookup_extra={"weird-agent": weird_manifest}
    )

    discovered = DiscoveredPlugin(
        manifest=load_plugin_manifest(target_briefings),
        module_path=target_briefings,
        source=IN_TREE_SOURCE,
    )
    module = load_plugin_module(discovered)
    module.install(ctx)

    # No write_file ops (the agent had no slash command surface), but
    # the scheduled tasks are still registered because they're agent-
    # agnostic.
    write_ops = [op for op in ctx.manifest.operations if op.kind == "write_file"]
    assert write_ops == []
    register_ops = [op for op in ctx.manifest.operations if op.kind == "register_task"]
    assert len(register_ops) == 3


# ---------------------------------------------------------------------------
# Engine integration: install, configure, uninstall
# ---------------------------------------------------------------------------


def _briefings_in_tree(tmp_path: Path) -> Path:
    """Materialize the in-tree briefings plugin into ``tmp_path`` for engine tests."""
    root = tmp_path / "in-tree"
    target = root / "briefings"
    target.mkdir(parents=True)
    for child in _BRIEFINGS_DIR.rglob("*"):
        if child.is_file():
            relative = child.relative_to(_BRIEFINGS_DIR)
            dest = target / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(child.read_bytes())
    return root


def test_engine_install_then_uninstall_full_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))

    in_tree = _briefings_in_tree(tmp_path)
    agent_commands = tmp_path / "agent-cmds"
    agent_commands.mkdir()
    _materialize_agent_plugin(in_tree, name="fake-agent", commands_dir=agent_commands)
    # Briefings calls ``discover_plugins(ctx.state)`` without an
    # in_tree_root override; redirect the default so its internal
    # discovery sees the synthetic agent.
    _patch_in_tree_root(monkeypatch, in_tree)

    pm = FakePackageManager()
    scheduler = FakeScheduler()
    components = PlatformComponents(
        platform=Platform.MACOS,
        package_manager=pm,
        scheduler=scheduler,
    )

    save_state(
        CerebroState(core_version="0.1.0", vault_path=tmp_path / "vault"),
        home / "state.yaml",
    )

    engine_install(
        "fake-agent",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
    )
    engine_install(
        "briefings",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
    )

    # All seven slash command files written into the synthetic agent's
    # convention-defined directory.
    for name in _SLASH_COMMAND_NAMES:
        assert (agent_commands / f"{name}.md").is_file()
    # Three scheduled tasks registered.
    assert {
        "cerebro-briefings-daily-summary",
        "cerebro-briefings-weekly-summary",
        "cerebro-briefings-monthly-summary",
    } <= scheduler.registered

    state = load_state(home / "state.yaml")
    assert {p.name for p in state.installed_plugins} == {"fake-agent", "briefings"}

    engine_uninstall(
        "briefings",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
    )

    # Files gone.
    for name in _SLASH_COMMAND_NAMES:
        assert not (agent_commands / f"{name}.md").exists()
    # Tasks gone.
    assert not (
        scheduler.registered
        & {
            "cerebro-briefings-daily-summary",
            "cerebro-briefings-weekly-summary",
            "cerebro-briefings-monthly-summary",
        }
    )
    state = load_state(home / "state.yaml")
    assert "briefings" not in {p.name for p in state.installed_plugins}


def test_configure_extends_to_newly_installed_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding a second agent after briefings is installed triggers configure."""
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))

    in_tree = _briefings_in_tree(tmp_path)
    agent1_cmds = tmp_path / "agent1-cmds"
    agent1_cmds.mkdir()
    agent2_cmds = tmp_path / "agent2-cmds"
    agent2_cmds.mkdir()
    _materialize_agent_plugin(in_tree, name="agent-one", commands_dir=agent1_cmds)
    _patch_in_tree_root(monkeypatch, in_tree)

    pm = FakePackageManager()
    scheduler = FakeScheduler()
    components = PlatformComponents(
        platform=Platform.MACOS,
        package_manager=pm,
        scheduler=scheduler,
    )

    save_state(
        CerebroState(core_version="0.1.0", vault_path=tmp_path / "vault"),
        home / "state.yaml",
    )

    engine_install(
        "agent-one",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
    )
    engine_install(
        "briefings",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
    )

    # agent-one has all seven; agent-two doesn't exist yet.
    for name in _SLASH_COMMAND_NAMES:
        assert (agent1_cmds / f"{name}.md").is_file()
    assert not list(agent2_cmds.glob("*.md"))

    # Add a second agent. The engine reconfigure pass should call
    # briefings.configure(), which extends every slash command to the
    # new agent.
    _materialize_agent_plugin(in_tree, name="agent-two", commands_dir=agent2_cmds)
    engine_install(
        "agent-two",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
    )

    for name in _SLASH_COMMAND_NAMES:
        assert (agent2_cmds / f"{name}.md").is_file()


# ---------------------------------------------------------------------------
# verify hook
# ---------------------------------------------------------------------------


def test_verify_succeeds_when_files_present_and_tasks_registered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_tree = tmp_path / "in-tree"
    in_tree.mkdir()
    target_briefings = in_tree / "briefings"
    target_briefings.mkdir()
    for child in _BRIEFINGS_DIR.rglob("*"):
        if child.is_file():
            relative = child.relative_to(_BRIEFINGS_DIR)
            dest = target_briefings / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(child.read_bytes())
    agent_cmds = tmp_path / "agent-cmds"
    agent_cmds.mkdir()
    _materialize_agent_plugin(in_tree, name="fake-agent", commands_dir=agent_cmds)
    _patch_in_tree_root(monkeypatch, in_tree)

    # Pre-populate slash command files and registered tasks.
    for name in _SLASH_COMMAND_NAMES:
        (agent_cmds / f"{name}.md").write_text("ok\n", encoding="utf-8")
    scheduler = FakeScheduler(
        already_registered={
            "cerebro-briefings-daily-summary",
            "cerebro-briefings-weekly-summary",
            "cerebro-briefings-monthly-summary",
        }
    )

    fake_agent_manifest = load_plugin_manifest(in_tree / "fake-agent")
    state = CerebroState(
        core_version="0.1.0",
        vault_path=tmp_path / "vault",
        installed_plugins=[
            InstalledPlugin(
                name="fake-agent",
                source=IN_TREE_SOURCE,
                version="1.0.0",
                installed_at=datetime(2026, 4, 27, 11, 0, 0, tzinfo=UTC),
                enabled=True,
            ),
        ],
    )
    ctx = _make_ctx(
        state=state,
        manifest_lookup_extra={"fake-agent": fake_agent_manifest},
        scheduler=scheduler,
    )

    discovered = DiscoveredPlugin(
        manifest=load_plugin_manifest(target_briefings),
        module_path=target_briefings,
        source=IN_TREE_SOURCE,
    )
    module = load_plugin_module(discovered)
    module.verify(ctx)  # should not raise


def test_verify_raises_when_slash_command_file_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_tree = tmp_path / "in-tree"
    in_tree.mkdir()
    target_briefings = in_tree / "briefings"
    target_briefings.mkdir()
    for child in _BRIEFINGS_DIR.rglob("*"):
        if child.is_file():
            relative = child.relative_to(_BRIEFINGS_DIR)
            dest = target_briefings / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(child.read_bytes())
    agent_cmds = tmp_path / "agent-cmds"
    agent_cmds.mkdir()
    _materialize_agent_plugin(in_tree, name="fake-agent", commands_dir=agent_cmds)
    _patch_in_tree_root(monkeypatch, in_tree)

    fake_agent_manifest = load_plugin_manifest(in_tree / "fake-agent")
    state = CerebroState(
        core_version="0.1.0",
        vault_path=tmp_path / "vault",
        installed_plugins=[
            InstalledPlugin(
                name="fake-agent",
                source=IN_TREE_SOURCE,
                version="1.0.0",
                installed_at=datetime(2026, 4, 27, 11, 0, 0, tzinfo=UTC),
                enabled=True,
            ),
        ],
    )
    ctx = _make_ctx(
        state=state,
        manifest_lookup_extra={"fake-agent": fake_agent_manifest},
    )

    discovered = DiscoveredPlugin(
        manifest=load_plugin_manifest(target_briefings),
        module_path=target_briefings,
        source=IN_TREE_SOURCE,
    )
    module = load_plugin_module(discovered)
    with pytest.raises(RuntimeError, match="slash command file missing"):
        module.verify(ctx)


def test_verify_raises_when_scheduled_task_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_tree = tmp_path / "in-tree"
    in_tree.mkdir()
    target_briefings = in_tree / "briefings"
    target_briefings.mkdir()
    for child in _BRIEFINGS_DIR.rglob("*"):
        if child.is_file():
            relative = child.relative_to(_BRIEFINGS_DIR)
            dest = target_briefings / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(child.read_bytes())
    agent_cmds = tmp_path / "agent-cmds"
    agent_cmds.mkdir()
    _materialize_agent_plugin(in_tree, name="fake-agent", commands_dir=agent_cmds)
    _patch_in_tree_root(monkeypatch, in_tree)

    # All slash command files present, but no scheduler entries.
    for name in _SLASH_COMMAND_NAMES:
        (agent_cmds / f"{name}.md").write_text("ok\n", encoding="utf-8")

    fake_agent_manifest = load_plugin_manifest(in_tree / "fake-agent")
    state = CerebroState(
        core_version="0.1.0",
        vault_path=tmp_path / "vault",
        installed_plugins=[
            InstalledPlugin(
                name="fake-agent",
                source=IN_TREE_SOURCE,
                version="1.0.0",
                installed_at=datetime(2026, 4, 27, 11, 0, 0, tzinfo=UTC),
                enabled=True,
            ),
        ],
    )
    ctx = _make_ctx(
        state=state,
        manifest_lookup_extra={"fake-agent": fake_agent_manifest},
        scheduler=FakeScheduler(),  # nothing registered
    )

    discovered = DiscoveredPlugin(
        manifest=load_plugin_manifest(target_briefings),
        module_path=target_briefings,
        source=IN_TREE_SOURCE,
    )
    module = load_plugin_module(discovered)
    with pytest.raises(RuntimeError, match="not registered"):
        module.verify(ctx)
