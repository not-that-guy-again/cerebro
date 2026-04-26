from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from cerebro.models import CerebroState
from cerebro.plugins.loader import (
    DiscoveredPlugin,
    PluginConflictError,
    PluginLoadError,
    discover_plugins,
    load_plugin_module,
)


def _state(tmp_path: Path) -> CerebroState:
    return CerebroState(core_version="0.1.0", vault_path=tmp_path / "vault")


def _write_plugin(
    root: Path,
    name: str,
    *,
    version: str = "0.1.0",
    plugin_type: str = "behavior",
    dependencies: dict[str, str] | None = None,
    hooks_declared: list[str] | None = None,
    plugin_py: str | None = None,
) -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True)
    manifest_data = {
        "name": name,
        "version": version,
        "type": plugin_type,
        "description": f"test fixture for {name}",
        "dependencies": dependencies or {},
        "min_core_version": "0.1.0",
        "supported_platforms": ["macos", "linux"],
        "hooks_declared": hooks_declared or [],
    }
    with (plugin_dir / "plugin.yaml").open("w", encoding="utf-8") as fh:
        yaml.safe_dump(manifest_data, fh, sort_keys=False)
    if plugin_py is not None:
        (plugin_dir / "plugin.py").write_text(plugin_py)
    return plugin_dir


def test_discover_finds_in_tree_plugins(tmp_path: Path) -> None:
    in_tree = tmp_path / "in_tree"
    in_tree.mkdir()
    _write_plugin(in_tree, "alpha")
    _write_plugin(in_tree, "beta")

    found = discover_plugins(_state(tmp_path), in_tree_root=in_tree, taps_root=tmp_path / "taps")

    assert set(found) == {"alpha", "beta"}
    assert all(isinstance(d, DiscoveredPlugin) for d in found.values())
    assert found["alpha"].source == "in-tree"
    assert found["alpha"].module_path == in_tree / "alpha"


def test_discover_finds_tap_plugins(tmp_path: Path) -> None:
    in_tree = tmp_path / "in_tree"
    in_tree.mkdir()
    taps_root = tmp_path / "taps"
    tap_one_plugins = taps_root / "tap-one" / "plugins"
    _write_plugin(tap_one_plugins, "from-tap")

    found = discover_plugins(_state(tmp_path), in_tree_root=in_tree, taps_root=taps_root)

    assert set(found) == {"from-tap"}
    assert found["from-tap"].source == "tap-one"


def test_discover_walks_in_tree_and_taps(tmp_path: Path) -> None:
    in_tree = tmp_path / "in_tree"
    _write_plugin(in_tree, "core-plugin")
    taps_root = tmp_path / "taps"
    _write_plugin(taps_root / "user-tap" / "plugins", "extra-plugin")

    found = discover_plugins(_state(tmp_path), in_tree_root=in_tree, taps_root=taps_root)

    assert set(found) == {"core-plugin", "extra-plugin"}
    assert found["core-plugin"].source == "in-tree"
    assert found["extra-plugin"].source == "user-tap"


def test_discover_raises_on_name_conflict_with_qualification_hint(tmp_path: Path) -> None:
    in_tree = tmp_path / "in_tree"
    _write_plugin(in_tree, "dup")
    taps_root = tmp_path / "taps"
    _write_plugin(taps_root / "tap-x" / "plugins", "dup")
    _write_plugin(taps_root / "tap-y" / "plugins", "dup")

    with pytest.raises(PluginConflictError) as excinfo:
        discover_plugins(_state(tmp_path), in_tree_root=in_tree, taps_root=taps_root)

    msg = str(excinfo.value)
    assert "dup" in msg
    assert "in-tree/dup" in msg
    assert "tap-x/dup" in msg
    assert "tap-y/dup" in msg


def test_discover_skips_non_plugin_directories(tmp_path: Path) -> None:
    in_tree = tmp_path / "in_tree"
    in_tree.mkdir()
    (in_tree / "__pycache__").mkdir()
    (in_tree / "no-manifest").mkdir()
    (in_tree / "loader.py").write_text("# pretend support module\n")
    _write_plugin(in_tree, "real")

    found = discover_plugins(_state(tmp_path), in_tree_root=in_tree, taps_root=tmp_path / "taps")

    assert set(found) == {"real"}


def test_discover_with_missing_roots_returns_empty(tmp_path: Path) -> None:
    found = discover_plugins(
        _state(tmp_path),
        in_tree_root=tmp_path / "does-not-exist-in-tree",
        taps_root=tmp_path / "does-not-exist-taps",
    )
    assert found == {}


def test_load_plugin_module_imports_and_validates_hooks(tmp_path: Path) -> None:
    in_tree = tmp_path / "in_tree"
    _write_plugin(
        in_tree,
        "withhooks",
        hooks_declared=["install", "uninstall"],
        plugin_py=dedent(
            """\
            def install(ctx):
                return "installed"

            def uninstall(ctx):
                return "uninstalled"
            """
        ),
    )
    found = discover_plugins(_state(tmp_path), in_tree_root=in_tree, taps_root=tmp_path / "taps")

    module = load_plugin_module(found["withhooks"])

    assert callable(module.install)
    assert module.install(None) == "installed"
    assert module.uninstall(None) == "uninstalled"


def test_load_plugin_module_raises_when_declared_hook_missing(tmp_path: Path) -> None:
    in_tree = tmp_path / "in_tree"
    _write_plugin(
        in_tree,
        "broken",
        hooks_declared=["install", "configure"],
        plugin_py="def install(ctx):\n    pass\n",
    )
    found = discover_plugins(_state(tmp_path), in_tree_root=in_tree, taps_root=tmp_path / "taps")

    with pytest.raises(PluginLoadError, match="configure"):
        load_plugin_module(found["broken"])


def test_load_plugin_module_raises_when_declared_hook_not_callable(tmp_path: Path) -> None:
    in_tree = tmp_path / "in_tree"
    _write_plugin(
        in_tree,
        "notcallable",
        hooks_declared=["install"],
        plugin_py="install = 'not a function'\n",
    )
    found = discover_plugins(_state(tmp_path), in_tree_root=in_tree, taps_root=tmp_path / "taps")

    with pytest.raises(PluginLoadError, match="install"):
        load_plugin_module(found["notcallable"])


def test_load_plugin_module_raises_when_no_module_file(tmp_path: Path) -> None:
    in_tree = tmp_path / "in_tree"
    _write_plugin(in_tree, "noplugin")
    found = discover_plugins(_state(tmp_path), in_tree_root=in_tree, taps_root=tmp_path / "taps")

    with pytest.raises(PluginLoadError, match="plugin.py"):
        load_plugin_module(found["noplugin"])


def test_load_plugin_module_supports_package_form(tmp_path: Path) -> None:
    in_tree = tmp_path / "in_tree"
    plugin_dir = _write_plugin(in_tree, "pkgform", hooks_declared=["install"])
    (plugin_dir / "__init__.py").write_text("def install(ctx):\n    return 42\n")
    found = discover_plugins(_state(tmp_path), in_tree_root=in_tree, taps_root=tmp_path / "taps")

    module = load_plugin_module(found["pkgform"])

    assert module.install(None) == 42
