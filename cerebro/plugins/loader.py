"""Plugin discovery and module loading.

Walks the in-tree plugin directory and every registered tap looking for
plugin subdirectories that contain a ``plugin.yaml`` manifest. Returns a
flat ``name -> DiscoveredPlugin`` map. Conflicts (the same plugin name
declared by two sources) raise ``PluginConflictError`` with a message that
suggests the ``<source>/<name>`` qualification described in ADR-0009.

This module does not run any plugin hooks; it only loads metadata and
imports the plugin's Python module so the hook functions are addressable.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from cerebro.models import CerebroState, PluginManifest
from cerebro.state import load_plugin_manifest, state_dir

IN_TREE_SOURCE = "in-tree"
_PLUGIN_MANIFEST_FILENAME = "plugin.yaml"
_PLUGIN_MODULE_FILENAME = "plugin.py"
_PACKAGE_INIT_FILENAME = "__init__.py"
_TAPS_PLUGINS_SUBDIR = "plugins"


class PluginConflictError(RuntimeError):
    """Two or more sources declare a plugin with the same name."""


class PluginLoadError(RuntimeError):
    """A plugin could not be loaded (missing module, missing hook, etc.)."""


@dataclass(frozen=True)
class DiscoveredPlugin:
    manifest: PluginManifest
    module_path: Path
    source: str


def default_in_tree_root() -> Path:
    """Where in-tree plugins live by default: this package's own directory."""
    return Path(__file__).resolve().parent


def default_taps_root() -> Path:
    """Where registered taps live by default: ``<state_dir>/taps``."""
    return state_dir() / "taps"


def discover_plugins(
    state: CerebroState,
    *,
    in_tree_root: Path | None = None,
    taps_root: Path | None = None,
) -> dict[str, DiscoveredPlugin]:
    """Discover plugins from the in-tree root and every tap, keyed by name.

    ``state`` is currently advisory; it is part of the signature so callers
    can pass the engine's resolved state for future filtering (e.g., honoring
    a tap allowlist) without changing the call sites.
    """
    del state  # reserved for future filtering; see docstring
    in_tree = in_tree_root if in_tree_root is not None else default_in_tree_root()
    taps = taps_root if taps_root is not None else default_taps_root()

    by_name: dict[str, list[DiscoveredPlugin]] = {}
    for found in _scan_plugin_dir(in_tree, IN_TREE_SOURCE):
        by_name.setdefault(found.manifest.name, []).append(found)

    if taps.is_dir():
        for tap_dir in sorted(p for p in taps.iterdir() if p.is_dir()):
            tap_plugins = tap_dir / _TAPS_PLUGINS_SUBDIR
            for found in _scan_plugin_dir(tap_plugins, tap_dir.name):
                by_name.setdefault(found.manifest.name, []).append(found)

    conflicts = {name: ds for name, ds in by_name.items() if len(ds) > 1}
    if conflicts:
        raise PluginConflictError(_format_conflict_message(conflicts))

    return {name: ds[0] for name, ds in by_name.items()}


def load_plugin_module(discovered: DiscoveredPlugin) -> ModuleType:
    """Import the plugin's Python module and validate its declared hooks.

    Plugins ship as either a single ``plugin.py`` file or a package (a
    directory with ``__init__.py``). The package form is supported because
    ADR-0003 explicitly allows plugins to grow into packages.
    """
    plugin_dir = discovered.module_path
    module_file = plugin_dir / _PLUGIN_MODULE_FILENAME
    package_init = plugin_dir / _PACKAGE_INIT_FILENAME

    if module_file.is_file():
        spec_path = module_file
        search_locations: list[str] | None = None
    elif package_init.is_file():
        spec_path = package_init
        search_locations = [str(plugin_dir)]
    else:
        raise PluginLoadError(
            f"plugin {discovered.manifest.name!r} at {plugin_dir} has no "
            f"{_PLUGIN_MODULE_FILENAME} or {_PACKAGE_INIT_FILENAME}"
        )

    module_name = _module_name_for(discovered)
    spec = importlib.util.spec_from_file_location(
        module_name,
        spec_path,
        submodule_search_locations=search_locations,
    )
    if spec is None or spec.loader is None:
        raise PluginLoadError(
            f"could not build import spec for plugin {discovered.manifest.name!r} at {spec_path}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise

    missing = [
        hook
        for hook in sorted(discovered.manifest.hooks_declared)
        if not callable(getattr(module, hook, None))
    ]
    if missing:
        raise PluginLoadError(
            f"plugin {discovered.manifest.name!r} declares hooks "
            f"{missing} but they are not callable functions in {spec_path}"
        )
    return module


def _scan_plugin_dir(root: Path, source: str) -> list[DiscoveredPlugin]:
    if not root.is_dir():
        return []
    out: list[DiscoveredPlugin] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / _PLUGIN_MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        manifest = load_plugin_manifest(entry)
        out.append(
            DiscoveredPlugin(
                manifest=manifest,
                module_path=entry,
                source=source,
            )
        )
    return out


def _format_conflict_message(conflicts: dict[str, list[DiscoveredPlugin]]) -> str:
    lines = []
    for name in sorted(conflicts):
        sources = ", ".join(d.source for d in conflicts[name])
        qualifications = ", ".join(f"{d.source}/{name}" for d in conflicts[name])
        lines.append(f"{name!r} declared by {sources}; qualify as one of {qualifications}")
    return "plugin name conflicts: " + "; ".join(lines)


def _module_name_for(discovered: DiscoveredPlugin) -> str:
    safe_source = discovered.source.replace("-", "_").replace("/", "_")
    safe_name = discovered.manifest.name.replace("-", "_")
    return f"cerebro_plugin_{safe_source}__{safe_name}"


__all__ = [
    "DiscoveredPlugin",
    "IN_TREE_SOURCE",
    "PluginConflictError",
    "PluginLoadError",
    "default_in_tree_root",
    "default_taps_root",
    "discover_plugins",
    "load_plugin_module",
]
