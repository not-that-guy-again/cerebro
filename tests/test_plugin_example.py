"""Tests for the in-tree example plugin.

These tests are the CI guardrail referenced by ``docs/authoring-plugins.md``:
if the worked example diverges from the runtime contract, the engine
install/uninstall test below fails and the docs are corrected alongside
the code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

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
from cerebro.runtime.platform import Platform, PlatformComponents
from cerebro.state import load_plugin_manifest, save_state
from tests.test_recorder import FakePackageManager, FakeScheduler

_PLUGIN_DIR = (
    Path(__file__).resolve().parent.parent / "cerebro" / "plugins" / "example"
)


def _example_module() -> ModuleType:
    discovered = DiscoveredPlugin(
        manifest=load_plugin_manifest(_PLUGIN_DIR),
        module_path=_PLUGIN_DIR,
        source=IN_TREE_SOURCE,
    )
    return load_plugin_module(discovered)


def _example_manifest() -> PluginManifest:
    return load_plugin_manifest(_PLUGIN_DIR)


def _make_ctx(state: CerebroState) -> HookContext:
    plugin = _example_manifest()

    def manifest_lookup(name: str) -> PluginManifest | None:
        if name == _PLUGIN_NAME:
            return plugin
        return None

    return build_context(
        plugin=plugin,
        state=state,
        package_manager=FakePackageManager(),
        scheduler=FakeScheduler(),
        when=datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC),
        manifest_lookup=manifest_lookup,
    )


_PLUGIN_NAME = "example"


def test_manifest_round_trips_through_pydantic() -> None:
    """The shipped manifest validates as a PluginManifest."""
    manifest = _example_manifest()
    assert manifest.name == _PLUGIN_NAME
    assert manifest.type == "behavior"
    assert manifest.targets == ["all agent plugins"]
    assert manifest.hooks_declared == {"install", "uninstall", "configure", "verify"}


def test_install_with_no_agents_writes_only_greeting_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    module = _example_module()
    state = CerebroState(
        core_version="0.1.0",
        vault_path=tmp_path / "vault",
        installed_plugins=[],
    )
    ctx = _make_ctx(state)

    module.install(ctx)

    greeting = tmp_path / "home" / "plugins" / _PLUGIN_NAME / "greeting.txt"
    assert greeting.exists()
    assert "example plugin" in greeting.read_text(encoding="utf-8")
    kinds = [op.kind for op in ctx.manifest.operations]
    assert kinds == ["write_file"]


def test_install_skips_agent_without_rules_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed agent that publishes no rules_file_path is skipped, not fatal."""
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))

    in_tree = tmp_path / "in-tree"
    fake_dir = in_tree / "fake-agent"
    fake_dir.mkdir(parents=True)
    (fake_dir / "plugin.yaml").write_text(
        "name: fake-agent\n"
        "version: 1.0.0\n"
        "type: agent\n"
        "description: synthetic agent without a rules_file_path\n"
        "min_core_version: 0.1.0\n"
        "supported_platforms: [macos]\n"
        "hooks_declared: [install]\n",
        encoding="utf-8",
    )
    (fake_dir / "plugin.py").write_text(
        "def install(ctx):\n    pass\n",
        encoding="utf-8",
    )

    import cerebro.plugins.loader as loader_mod

    monkeypatch.setattr(loader_mod, "default_in_tree_root", lambda: in_tree)

    module = _example_module()
    state = CerebroState(
        core_version="0.1.0",
        vault_path=tmp_path / "vault",
        installed_plugins=[
            InstalledPlugin(
                name="fake-agent",
                source=IN_TREE_SOURCE,
                version="1.0.0",
                installed_at=datetime(2026, 5, 2, 11, 0, 0, tzinfo=UTC),
                enabled=True,
            ),
        ],
    )
    plugin = _example_manifest()
    fake_manifest = PluginManifest(
        name="fake-agent",
        version="1.0.0",
        type="agent",
        description="synthetic agent without a rules_file_path",
        min_core_version="0.1.0",
        supported_platforms=["macos"],
    )

    def manifest_lookup(name: str) -> PluginManifest | None:
        if name == "fake-agent":
            return fake_manifest
        if name == _PLUGIN_NAME:
            return plugin
        return None

    ctx = build_context(
        plugin=plugin,
        state=state,
        package_manager=FakePackageManager(),
        scheduler=FakeScheduler(),
        when=datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC),
        manifest_lookup=manifest_lookup,
    )

    module.install(ctx)

    kinds = [op.kind for op in ctx.manifest.operations]
    assert kinds == ["write_file"]


def test_verify_succeeds_after_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    module = _example_module()
    state = CerebroState(
        core_version="0.1.0",
        vault_path=tmp_path / "vault",
        installed_plugins=[],
    )
    ctx = _make_ctx(state)

    module.install(ctx)
    module.verify(ctx)


def test_verify_raises_when_greeting_file_modified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    module = _example_module()
    state = CerebroState(
        core_version="0.1.0",
        vault_path=tmp_path / "vault",
        installed_plugins=[],
    )
    ctx = _make_ctx(state)

    module.install(ctx)

    greeting = tmp_path / "home" / "plugins" / _PLUGIN_NAME / "greeting.txt"
    greeting.write_text("tampered", encoding="utf-8")
    with pytest.raises(RuntimeError, match="modified outside Cerebro"):
        module.verify(ctx)


def test_verify_raises_when_greeting_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    module = _example_module()
    state = CerebroState(
        core_version="0.1.0",
        vault_path=tmp_path / "vault",
        installed_plugins=[],
    )
    ctx = _make_ctx(state)

    with pytest.raises(RuntimeError, match="greeting file is missing"):
        module.verify(ctx)


# ---------------------------------------------------------------------------
# Engine integration: this is the documentation-drift guard called out in
# docs/authoring-plugins.md. It runs the full install + uninstall through
# the real engine, exercising every layer the worked example references.
# ---------------------------------------------------------------------------


def _materialize_example(tmp_path: Path) -> Path:
    root = tmp_path / "in-tree"
    target = root / "example"
    target.mkdir(parents=True)
    for entry in _PLUGIN_DIR.rglob("*"):
        if entry.is_file() and "__pycache__" not in entry.parts:
            rel = entry.relative_to(_PLUGIN_DIR)
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(entry.read_bytes())
    return root


def test_engine_install_then_uninstall_full_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI guard: install + uninstall the example plugin through the engine.

    Referenced by docs/authoring-plugins.md as the mechanism that keeps
    the worked example in lockstep with the runtime contract.
    """
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))

    import cerebro.runtime.platform as platform_mod

    monkeypatch.setattr(platform_mod, "current_platform", lambda: Platform.MACOS)

    in_tree = _materialize_example(tmp_path)

    import cerebro.plugins.loader as loader_mod

    monkeypatch.setattr(loader_mod, "default_in_tree_root", lambda: in_tree)

    components = PlatformComponents(
        platform=Platform.MACOS,
        package_manager=FakePackageManager(),
        scheduler=FakeScheduler(),
    )

    save_state(
        CerebroState(
            core_version="0.1.0",
            vault_path=tmp_path / "ObsidianVault",
        ),
        home / "state.yaml",
    )

    engine_install(
        _PLUGIN_NAME,
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
    )

    greeting = home / "plugins" / _PLUGIN_NAME / "greeting.txt"
    assert greeting.exists()
    install_manifest = home / "manifests" / _PLUGIN_NAME / "install.yaml"
    assert install_manifest.exists()

    engine_uninstall(
        _PLUGIN_NAME,
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
    )

    assert not greeting.exists()
    assert not install_manifest.exists()
