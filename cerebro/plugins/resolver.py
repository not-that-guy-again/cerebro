"""Dependency resolution and cross-type targeting for plugins.

Version ranges in plugin manifests use the ``semantic_version`` library
(strict semver 2.0.0) rather than ``packaging`` (PEP 440). Reasons:

- ``PluginManifest.version`` is validated against the strict semver 2.0.0
  regex in ``cerebro.models``. Using ``semantic_version`` keeps version
  semantics consistent end-to-end.
- ``packaging`` would force PEP 440 normalization (e.g., ``1.0.0-beta.1``
  becomes ``1.0.0b1``), which is lossy for manifests that already validate
  as strict semver.
- ``semantic_version.SimpleSpec`` accepts the operators plugin authors
  expect from a semver world (``>=``, ``<``, ``^``, ``~``, ``*``, comma-
  separated clauses).
"""

from __future__ import annotations

from semantic_version import SimpleSpec, Version

from cerebro.models import InstalledPlugin, PluginManifest
from cerebro.plugins.loader import DiscoveredPlugin


class DependencyResolutionError(RuntimeError):
    """Raised for missing deps, version mismatches, and dependency cycles."""


_DEFAULT_TYPE_TARGETS: dict[str, str] = {
    "agent": "ide",
    "behavior": "agent",
    "workflow": "agent",
}


def resolve_install_order(
    target: str,
    available: dict[str, DiscoveredPlugin],
    installed: list[InstalledPlugin],
) -> list[PluginManifest]:
    """Topologically sort the plugins that must be installed to install ``target``.

    Already-installed plugins are skipped if their version satisfies the
    range required by their dependents. Cycles raise with a message that
    names every node on the cycle.
    """
    if target not in available:
        raise DependencyResolutionError(f"plugin {target!r} is not available")

    installed_by_name = {p.name: p for p in installed}
    visiting: list[str] = []
    visited: set[str] = set()
    order: list[PluginManifest] = []

    def visit(name: str, required_range: str | None) -> None:
        installed_record = installed_by_name.get(name)
        if installed_record is not None:
            if required_range is not None and not _satisfies(
                installed_record.version, required_range
            ):
                raise DependencyResolutionError(
                    f"installed plugin {name!r} version {installed_record.version} "
                    f"does not satisfy required range {required_range!r}"
                )
            return
        if name in visited:
            return
        if name in visiting:
            cycle = visiting[visiting.index(name) :] + [name]
            raise DependencyResolutionError(
                "dependency cycle detected: " + " -> ".join(cycle)
            )
        if name not in available:
            raise DependencyResolutionError(
                f"required dependency {name!r} is not available"
            )
        manifest = available[name].manifest
        if required_range is not None and not _satisfies(manifest.version, required_range):
            raise DependencyResolutionError(
                f"available plugin {name!r} version {manifest.version} "
                f"does not satisfy required range {required_range!r}"
            )
        visiting.append(name)
        for dep_name, dep_range in manifest.dependencies.items():
            visit(dep_name, dep_range)
        visiting.pop()
        visited.add(name)
        order.append(manifest)

    visit(target, required_range=None)
    return order


def find_dependents(
    plugin_name: str,
    installed: list[InstalledPlugin],
    available: dict[str, DiscoveredPlugin],
) -> list[InstalledPlugin]:
    """Installed plugins that declare a dependency on ``plugin_name``.

    ``available`` provides the manifests for installed plugins; an installed
    plugin whose manifest is not currently discoverable (e.g., its source was
    removed) is skipped because its dependencies cannot be inspected.
    """
    out: list[InstalledPlugin] = []
    for record in installed:
        if record.name == plugin_name:
            continue
        discovered = available.get(record.name)
        if discovered is None:
            continue
        if plugin_name in discovered.manifest.dependencies:
            out.append(record)
    return out


def find_targeting(
    new_plugin: PluginManifest,
    installed: list[InstalledPlugin],
    available: dict[str, DiscoveredPlugin],
) -> list[InstalledPlugin]:
    """Installed plugins whose ``configure`` hook should re-run for ``new_plugin``.

    Targeting follows ADR-0004: ``agent`` plugins target ``ide`` plugins, and
    ``behavior`` and ``workflow`` plugins target ``agent`` plugins. A plugin
    can override the default by setting ``targets`` on its manifest, listing
    either specific plugin names or the sentinel ``"all <type> plugins"``.
    """
    out: list[InstalledPlugin] = []
    for record in installed:
        if record.name == new_plugin.name:
            continue
        discovered = available.get(record.name)
        if discovered is None:
            continue
        if _targets(discovered.manifest, new_plugin):
            out.append(record)
    return out


def _satisfies(version: str, range_spec: str) -> bool:
    return Version(version) in SimpleSpec(range_spec)


def _targets(candidate: PluginManifest, target: PluginManifest) -> bool:
    if candidate.targets is not None:
        for entry in candidate.targets:
            if entry == target.name:
                return True
            if entry == f"all {target.type} plugins":
                return True
        return False
    return _DEFAULT_TYPE_TARGETS.get(candidate.type) == target.type


__all__ = [
    "DependencyResolutionError",
    "find_dependents",
    "find_targeting",
    "resolve_install_order",
]
