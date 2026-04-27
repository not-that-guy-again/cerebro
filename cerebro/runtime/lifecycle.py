"""Enable / disable a previously-installed plugin.

These flip the ``enabled`` flag in ``state.yaml`` (without removing the
plugin) and tear down or re-create the scheduled tasks the plugin
registered at install time. We replay the persisted ``InstallManifest``
to find out which tasks belong to the plugin: that file is the
authoritative record of side effects per ADR-0007 and ADR-0008.

Pre-existing tasks (those the user had before Cerebro touched the
system) are left alone, mirroring the rollback policy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cerebro.models import CerebroState, InstalledPlugin
from cerebro.runtime.platform import PlatformComponents, make_platform_components
from cerebro.state import (
    load_install_manifest,
    load_state,
    save_state,
    state_dir,
)

_STATE_FILENAME = "state.yaml"
_MANIFESTS_DIRNAME = "manifests"
_INSTALL_MANIFEST_FILENAME = "install.yaml"


class LifecycleError(RuntimeError):
    """Raised for enable/disable preconditions that fail (e.g., not installed)."""


def enable_plugin(
    plugin_name: str,
    *,
    home: Path | None = None,
    components: PlatformComponents | None = None,
    now: datetime | None = None,
) -> bool:
    """Mark ``plugin_name`` enabled and re-register its scheduled tasks.

    Returns ``True`` if the plugin's enabled flag changed, ``False`` if
    it was already enabled.
    """
    return _flip(plugin_name, target=True, home=home, components=components, now=now)


def disable_plugin(
    plugin_name: str,
    *,
    home: Path | None = None,
    components: PlatformComponents | None = None,
    now: datetime | None = None,
) -> bool:
    """Mark ``plugin_name`` disabled and unregister its scheduled tasks.

    Returns ``True`` if the plugin's enabled flag changed, ``False`` if
    it was already disabled.
    """
    return _flip(plugin_name, target=False, home=home, components=components, now=now)


def _flip(
    plugin_name: str,
    *,
    target: bool,
    home: Path | None,
    components: PlatformComponents | None,
    now: datetime | None,
) -> bool:
    base = home if home is not None else state_dir()
    state_path = base / _STATE_FILENAME
    if not state_path.exists():
        raise LifecycleError(f"plugin {plugin_name!r} is not installed")
    state = load_state(state_path)
    record = next((p for p in state.installed_plugins if p.name == plugin_name), None)
    if record is None:
        raise LifecycleError(f"plugin {plugin_name!r} is not installed")
    if record.enabled is target:
        return False

    components = components if components is not None else make_platform_components()
    when = now if now is not None else datetime.now(tz=UTC)

    manifest_path = base / _MANIFESTS_DIRNAME / plugin_name / _INSTALL_MANIFEST_FILENAME
    if manifest_path.exists():
        install_manifest = load_install_manifest(manifest_path)
        for op in install_manifest.operations:
            if op.kind != "register_task" or op.pre_existing:
                continue
            params = op.parameters
            name = params["name"]
            if target:
                if not components.scheduler.is_registered(name):
                    components.scheduler.register(name, params["schedule"], params["command"])
            else:
                if components.scheduler.is_registered(name):
                    components.scheduler.unregister(name)

    updated_record = InstalledPlugin(
        name=record.name,
        source=record.source,
        version=record.version,
        installed_at=record.installed_at,
        enabled=target,
    )
    plugins = [p for p in state.installed_plugins if p.name != plugin_name]
    plugins.append(updated_record)
    new_state = CerebroState(
        core_version=state.core_version,
        vault_path=state.vault_path,
        installed_plugins=plugins,
    )
    save_state(new_state, state_path)
    del when  # currently unused; reserved for future audit logging
    return True


__all__ = [
    "LifecycleError",
    "disable_plugin",
    "enable_plugin",
]
