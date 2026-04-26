"""Filesystem I/O for Cerebro state and plugin manifests.

All persistence is YAML. The state directory defaults to ``~/.config/cerebro/``
(per ADR-0002) and is overridable with the ``CEREBRO_HOME`` environment
variable for tests and non-default installations.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from cerebro.models import CerebroState, InstallManifest, PluginManifest

CEREBRO_HOME_ENV = "CEREBRO_HOME"
_DEFAULT_STATE_DIR = "~/.config/cerebro"
_PLUGIN_MANIFEST_FILENAME = "plugin.yaml"


def state_dir() -> Path:
    """Return the Cerebro state directory, honoring the ``CEREBRO_HOME`` env var."""
    override = os.environ.get(CEREBRO_HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path(_DEFAULT_STATE_DIR).expanduser()


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(
            f"expected a mapping at the top level of {path}, got {type(data).__name__}"
        )
    return data


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)


def load_state(path: Path) -> CerebroState:
    return CerebroState.model_validate(_read_yaml(path))


def save_state(state: CerebroState, path: Path) -> None:
    _write_yaml(path, state.model_dump(mode="json"))


def load_plugin_manifest(plugin_dir: Path) -> PluginManifest:
    return PluginManifest.model_validate(_read_yaml(plugin_dir / _PLUGIN_MANIFEST_FILENAME))


def load_install_manifest(path: Path) -> InstallManifest:
    return InstallManifest.model_validate(_read_yaml(path))


def save_install_manifest(manifest: InstallManifest, path: Path) -> None:
    _write_yaml(path, manifest.model_dump(mode="json"))


__all__ = [
    "CEREBRO_HOME_ENV",
    "load_install_manifest",
    "load_plugin_manifest",
    "load_state",
    "save_install_manifest",
    "save_state",
    "state_dir",
]
