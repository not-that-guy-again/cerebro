from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cerebro.models import InstalledPlugin, PluginManifest
from cerebro.plugins.loader import DiscoveredPlugin
from cerebro.plugins.resolver import (
    DependencyResolutionError,
    find_dependents,
    find_targeting,
    resolve_install_order,
)


def _manifest(
    name: str,
    *,
    version: str = "0.1.0",
    plugin_type: str = "behavior",
    dependencies: dict[str, str] | None = None,
    targets: list[str] | None = None,
) -> PluginManifest:
    return PluginManifest(
        name=name,
        version=version,
        type=plugin_type,
        description=f"fixture {name}",
        dependencies=dependencies or {},
        min_core_version="0.1.0",
        supported_platforms=["macos", "linux"],
        targets=targets,
    )


def _discovered(manifest: PluginManifest, source: str = "in-tree") -> DiscoveredPlugin:
    return DiscoveredPlugin(manifest=manifest, module_path=Path("/dev/null"), source=source)


def _installed(name: str, *, version: str = "0.1.0", source: str = "in-tree") -> InstalledPlugin:
    return InstalledPlugin(
        name=name,
        source=source,
        version=version,
        installed_at=datetime(2026, 1, 1, tzinfo=UTC),
        enabled=True,
    )


def test_resolve_install_order_topologically_sorts(tmp_path: Path) -> None:
    available = {
        "a": _discovered(_manifest("a", dependencies={"b": ">=0.1.0", "c": ">=0.1.0"})),
        "b": _discovered(_manifest("b", dependencies={"c": ">=0.1.0"})),
        "c": _discovered(_manifest("c")),
    }

    order = resolve_install_order("a", available, installed=[])

    names = [m.name for m in order]
    assert names.index("c") < names.index("b") < names.index("a")
    assert names == ["c", "b", "a"]


def test_resolve_install_order_skips_already_installed(tmp_path: Path) -> None:
    available = {
        "a": _discovered(_manifest("a", dependencies={"b": ">=0.1.0"})),
        "b": _discovered(_manifest("b", dependencies={"c": ">=0.1.0"})),
        "c": _discovered(_manifest("c")),
    }

    order = resolve_install_order("a", available, installed=[_installed("c")])

    assert [m.name for m in order] == ["b", "a"]


def test_resolve_install_order_only_target_when_all_deps_installed(tmp_path: Path) -> None:
    available = {
        "a": _discovered(_manifest("a", dependencies={"b": ">=0.1.0"})),
        "b": _discovered(_manifest("b")),
    }

    order = resolve_install_order("a", available, installed=[_installed("b")])

    assert [m.name for m in order] == ["a"]


def test_resolve_install_order_raises_when_target_missing(tmp_path: Path) -> None:
    with pytest.raises(DependencyResolutionError, match="not available"):
        resolve_install_order("nope", {}, installed=[])


def test_resolve_install_order_raises_when_dependency_missing(tmp_path: Path) -> None:
    available = {
        "a": _discovered(_manifest("a", dependencies={"missing": ">=0.1.0"})),
    }
    with pytest.raises(DependencyResolutionError, match="missing"):
        resolve_install_order("a", available, installed=[])


def test_resolve_install_order_raises_on_version_mismatch(tmp_path: Path) -> None:
    available = {
        "a": _discovered(_manifest("a", dependencies={"b": ">=2.0.0"})),
        "b": _discovered(_manifest("b", version="1.0.0")),
    }
    with pytest.raises(DependencyResolutionError, match=">=2.0.0"):
        resolve_install_order("a", available, installed=[])


def test_resolve_install_order_raises_on_installed_version_mismatch(tmp_path: Path) -> None:
    available = {
        "a": _discovered(_manifest("a", dependencies={"b": ">=2.0.0"})),
        "b": _discovered(_manifest("b", version="2.0.0")),
    }
    with pytest.raises(DependencyResolutionError, match="installed"):
        resolve_install_order("a", available, installed=[_installed("b", version="1.0.0")])


def test_resolve_install_order_detects_two_node_cycle(tmp_path: Path) -> None:
    available = {
        "a": _discovered(_manifest("a", dependencies={"b": ">=0.1.0"})),
        "b": _discovered(_manifest("b", dependencies={"a": ">=0.1.0"})),
    }
    with pytest.raises(DependencyResolutionError) as excinfo:
        resolve_install_order("a", available, installed=[])

    msg = str(excinfo.value)
    assert "cycle" in msg
    assert "a" in msg and "b" in msg
    assert "->" in msg


def test_resolve_install_order_detects_three_node_cycle(tmp_path: Path) -> None:
    available = {
        "a": _discovered(_manifest("a", dependencies={"b": ">=0.1.0"})),
        "b": _discovered(_manifest("b", dependencies={"c": ">=0.1.0"})),
        "c": _discovered(_manifest("c", dependencies={"a": ">=0.1.0"})),
    }
    with pytest.raises(DependencyResolutionError) as excinfo:
        resolve_install_order("a", available, installed=[])

    msg = str(excinfo.value)
    assert "cycle" in msg
    for name in ("a", "b", "c"):
        assert name in msg


def test_resolve_install_order_does_not_revisit_completed_subtree(tmp_path: Path) -> None:
    available = {
        "root": _discovered(
            _manifest("root", dependencies={"x": ">=0.1.0", "y": ">=0.1.0"})
        ),
        "x": _discovered(_manifest("x", dependencies={"shared": ">=0.1.0"})),
        "y": _discovered(_manifest("y", dependencies={"shared": ">=0.1.0"})),
        "shared": _discovered(_manifest("shared")),
    }

    order = resolve_install_order("root", available, installed=[])

    names = [m.name for m in order]
    assert names.count("shared") == 1
    assert names.index("shared") < names.index("x")
    assert names.index("shared") < names.index("y")
    assert names[-1] == "root"


def test_find_dependents_returns_installed_plugins_that_depend_on_name(tmp_path: Path) -> None:
    available = {
        "ide-vscode": _discovered(_manifest("ide-vscode", plugin_type="ide")),
        "claude-code": _discovered(
            _manifest(
                "claude-code",
                plugin_type="agent",
                dependencies={"ide-vscode": ">=0.1.0"},
            )
        ),
        "briefings": _discovered(
            _manifest(
                "briefings",
                plugin_type="workflow",
                dependencies={"claude-code": ">=0.1.0"},
            )
        ),
    }
    installed = [
        _installed("ide-vscode"),
        _installed("claude-code"),
        _installed("briefings"),
    ]

    dependents = find_dependents("ide-vscode", installed, available)

    assert [p.name for p in dependents] == ["claude-code"]


def test_find_dependents_skips_self_and_unknown(tmp_path: Path) -> None:
    available = {
        "a": _discovered(_manifest("a", dependencies={"b": ">=0.1.0"})),
    }
    installed = [_installed("a"), _installed("orphan")]

    dependents = find_dependents("b", installed, available)

    assert [p.name for p in dependents] == ["a"]


def test_find_targeting_for_new_ide_returns_all_agent_plugins(tmp_path: Path) -> None:
    new_ide = _manifest("jetbrains", plugin_type="ide")
    available = {
        "claude-code": _discovered(_manifest("claude-code", plugin_type="agent")),
        "other-agent": _discovered(_manifest("other-agent", plugin_type="agent")),
        "briefings": _discovered(_manifest("briefings", plugin_type="workflow")),
        "coding-standards": _discovered(_manifest("coding-standards", plugin_type="behavior")),
    }
    installed = [
        _installed("claude-code"),
        _installed("other-agent"),
        _installed("briefings"),
        _installed("coding-standards"),
    ]

    targeted = find_targeting(new_ide, installed, available)

    assert sorted(p.name for p in targeted) == ["claude-code", "other-agent"]


def test_find_targeting_for_new_agent_returns_behavior_and_workflow_plugins(
    tmp_path: Path,
) -> None:
    new_agent = _manifest("new-agent", plugin_type="agent")
    available = {
        "vscode": _discovered(_manifest("vscode", plugin_type="ide")),
        "briefings": _discovered(_manifest("briefings", plugin_type="workflow")),
        "coding-standards": _discovered(_manifest("coding-standards", plugin_type="behavior")),
    }
    installed = [
        _installed("vscode"),
        _installed("briefings"),
        _installed("coding-standards"),
    ]

    targeted = find_targeting(new_agent, installed, available)

    assert sorted(p.name for p in targeted) == ["briefings", "coding-standards"]


def test_find_targeting_explicit_targets_specific_plugin(tmp_path: Path) -> None:
    new_agent = _manifest("claude-code", plugin_type="agent")
    available = {
        "specific-targeter": _discovered(
            _manifest("specific-targeter", plugin_type="behavior", targets=["claude-code"])
        ),
        "wrong-target": _discovered(
            _manifest("wrong-target", plugin_type="behavior", targets=["other-agent"])
        ),
    }
    installed = [_installed("specific-targeter"), _installed("wrong-target")]

    targeted = find_targeting(new_agent, installed, available)

    assert [p.name for p in targeted] == ["specific-targeter"]


def test_find_targeting_explicit_all_of_type(tmp_path: Path) -> None:
    new_agent = _manifest("claude-code", plugin_type="agent")
    available = {
        "broad": _discovered(
            _manifest("broad", plugin_type="behavior", targets=["all agent plugins"])
        ),
    }
    installed = [_installed("broad")]

    targeted = find_targeting(new_agent, installed, available)

    assert [p.name for p in targeted] == ["broad"]


def test_find_targeting_excludes_self(tmp_path: Path) -> None:
    new_agent = _manifest("claude-code", plugin_type="agent")
    available = {
        "claude-code": _discovered(_manifest("claude-code", plugin_type="agent")),
    }
    installed = [_installed("claude-code")]

    targeted = find_targeting(new_agent, installed, available)

    assert targeted == []
