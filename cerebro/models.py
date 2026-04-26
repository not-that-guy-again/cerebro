"""Core data models for Cerebro.

Stack choice: Pydantic v2.

Why Pydantic v2 over attrs+cattrs:
- Field validators integrate cleanly with the per-field semver and platform
  checks the spec requires, without writing a parallel converter layer.
- ``model_dump(mode="json")`` produces a YAML-friendly dict (datetimes as ISO
  strings, sets as lists, paths as strings) without per-type hooks.
- Validation errors come pre-formatted with field paths, satisfying the
  "clear validation error" acceptance criterion out of the box.
- One dependency vs. two; nothing else in the project needs cattrs.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PluginType = Literal["ide", "agent", "behavior", "workflow"]
OperationKind = Literal["write_file", "add_block", "run_pkg", "register_task"]
Platform = Literal["macos", "linux"]

_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+(?P<build>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


def _validate_semver(value: str) -> str:
    if not isinstance(value, str) or not _SEMVER_RE.match(value):
        raise ValueError(f"not a valid semver string: {value!r}")
    return value


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PluginManifest(_Base):
    name: str
    version: str
    type: PluginType
    description: str
    dependencies: dict[str, str] = Field(default_factory=dict)
    min_core_version: str
    supported_platforms: list[Platform]
    schedule: dict[str, Any] | None = None
    targets: list[str] | None = None
    hooks_declared: set[str] = Field(default_factory=set)

    @field_validator("version", "min_core_version")
    @classmethod
    def _check_semver(cls, v: str) -> str:
        return _validate_semver(v)

    @field_validator("dependencies")
    @classmethod
    def _check_dependencies(cls, v: dict[str, str]) -> dict[str, str]:
        for name, range_spec in v.items():
            if not name:
                raise ValueError("dependency name must be non-empty")
            if not isinstance(range_spec, str) or not range_spec.strip():
                raise ValueError(f"invalid range for dependency {name!r}: {range_spec!r}")
        return v

    @field_validator("supported_platforms")
    @classmethod
    def _check_platforms_non_empty(cls, v: list[Platform]) -> list[Platform]:
        if not v:
            raise ValueError("supported_platforms must list at least one platform")
        return v


class InstalledPlugin(_Base):
    name: str
    # Either the literal "in-tree" or the name of the tap the plugin came from.
    source: str
    version: str
    installed_at: datetime
    enabled: bool

    @field_validator("version")
    @classmethod
    def _check_semver(cls, v: str) -> str:
        return _validate_semver(v)

    @field_validator("source")
    @classmethod
    def _check_source(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("source must be 'in-tree' or a non-empty tap name")
        return v


class CerebroState(_Base):
    core_version: str
    vault_path: Path
    installed_plugins: list[InstalledPlugin] = Field(default_factory=list)

    @field_validator("core_version")
    @classmethod
    def _check_semver(cls, v: str) -> str:
        return _validate_semver(v)


class Operation(_Base):
    kind: OperationKind
    parameters: dict[str, Any] = Field(default_factory=dict)
    inverse: dict[str, Any] = Field(default_factory=dict)


class InstallManifest(_Base):
    plugin_name: str
    version: str
    operations: list[Operation] = Field(default_factory=list)
    installed_at: datetime

    @field_validator("version")
    @classmethod
    def _check_semver(cls, v: str) -> str:
        return _validate_semver(v)


__all__ = [
    "CerebroState",
    "InstallManifest",
    "InstalledPlugin",
    "Operation",
    "PluginManifest",
    "PluginType",
    "OperationKind",
    "Platform",
]
