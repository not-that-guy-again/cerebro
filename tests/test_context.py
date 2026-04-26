from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cerebro.models import CerebroState, InstalledPlugin, PluginManifest
from cerebro.runtime.context import build_context
from tests.test_recorder import FakePackageManager, FakeScheduler


def _manifest(name: str, plugin_type: str = "agent") -> PluginManifest:
    return PluginManifest(
        name=name,
        version="1.0.0",
        type=plugin_type,  # type: ignore[arg-type]
        description="",
        min_core_version="0.0.1",
        supported_platforms=["macos", "linux"],
    )


def _state(*plugins: str) -> CerebroState:
    return CerebroState(
        core_version="0.1.0",
        vault_path=Path("/tmp/vault"),
        installed_plugins=[
            InstalledPlugin(
                name=name,
                source="in-tree",
                version="1.0.0",
                installed_at=datetime(2026, 1, 1, tzinfo=UTC),
                enabled=True,
            )
            for name in plugins
        ],
    )


def test_build_context_wires_helpers() -> None:
    plugin = _manifest("demo")
    ctx = build_context(
        plugin=plugin,
        state=_state(),
        package_manager=FakePackageManager(),
        scheduler=FakeScheduler(),
        when=datetime(2026, 4, 26, tzinfo=UTC),
    )
    assert ctx.fs is not None
    assert ctx.blocks is not None
    assert ctx.pkg is not None
    assert ctx.tasks is not None
    assert ctx.auth is not None
    assert ctx.manifest is not None
    assert ctx.log.name == "cerebro.plugin.demo"


def test_installed_plugins_returns_state_list() -> None:
    ctx = build_context(
        plugin=_manifest("demo"),
        state=_state("a", "b"),
        package_manager=FakePackageManager(),
        scheduler=FakeScheduler(),
        when=datetime(2026, 4, 26, tzinfo=UTC),
    )
    assert [p.name for p in ctx.installed_plugins()] == ["a", "b"]


def test_plugins_of_type_filters_via_lookup() -> None:
    types = {"a": _manifest("a", "ide"), "b": _manifest("b", "agent")}

    ctx = build_context(
        plugin=_manifest("demo"),
        state=_state("a", "b"),
        package_manager=FakePackageManager(),
        scheduler=FakeScheduler(),
        when=datetime(2026, 4, 26, tzinfo=UTC),
        manifest_lookup=types.get,
    )
    assert [p.name for p in ctx.plugins_of_type("ide")] == ["a"]
    assert [p.name for p in ctx.plugins_of_type("agent")] == ["b"]
    assert ctx.plugins_of_type("workflow") == []


def test_plugins_of_type_default_lookup_returns_empty() -> None:
    ctx = build_context(
        plugin=_manifest("demo"),
        state=_state("a"),
        package_manager=FakePackageManager(),
        scheduler=FakeScheduler(),
        when=datetime(2026, 4, 26, tzinfo=UTC),
    )
    # Without a manifest_lookup we cannot determine the type; return [].
    assert ctx.plugins_of_type("ide") == []


def test_auth_helper_delegates_to_handoff() -> None:
    received: list[str] = []

    class SpyAuth:
        def request(self, *, message: str) -> None:
            received.append(message)

    ctx = build_context(
        plugin=_manifest("demo"),
        state=_state(),
        package_manager=FakePackageManager(),
        scheduler=FakeScheduler(),
        when=datetime(2026, 4, 26, tzinfo=UTC),
        auth=SpyAuth(),
    )
    ctx.auth.request("please log in")
    assert received == ["please log in"]
