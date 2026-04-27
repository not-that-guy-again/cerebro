"""Drift detection and repair for installed plugins.

The doctor compares two views of the system:

- *Expected*: the persisted ``InstallManifest`` for each installed plugin.
- *Actual*: what is currently on disk and in the platform's package /
  scheduler stores.

For each operation kind we know how to verify, ``check_plugin`` produces
an ``OperationCheck`` describing whether the resource is intact, has
drifted, or has gone missing. ``run_doctor`` walks every installed
plugin and returns one ``DriftReport`` per plugin.

Two repair paths are exposed:

- ``repair_plugin``: re-runs the plugin's ``install`` hook through the
  engine's transactional path. Failures roll back; the existing manifest
  is only replaced once the new install commits.
- ``accept_plugin``: rewrites the install manifest to describe the
  current disk state. Drifted operations are updated in place; missing
  operations are dropped. After accept, a follow-up ``run_doctor`` will
  report the plugin clean.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cerebro.models import InstalledPlugin, InstallManifest, Operation
from cerebro.runtime.blocks import find_block, get_format
from cerebro.runtime.platform import PlatformComponents, make_platform_components
from cerebro.state import (
    load_install_manifest,
    load_state,
    save_install_manifest,
    state_dir,
)

_STATE_FILENAME = "state.yaml"
_MANIFESTS_DIRNAME = "manifests"
_INSTALL_MANIFEST_FILENAME = "install.yaml"


class OperationStatus(enum.Enum):
    OK = "ok"
    DRIFTED = "drifted"
    MISSING = "missing"


@dataclass(frozen=True)
class OperationCheck:
    """The result of verifying a single recorded operation against disk."""

    kind: str
    target: str
    status: OperationStatus
    detail: str = ""


@dataclass(frozen=True)
class DriftReport:
    """Per-plugin summary of drift checks."""

    plugin_name: str
    version: str
    checks: list[OperationCheck] = field(default_factory=list)
    manifest_missing: bool = False

    @property
    def is_drifted(self) -> bool:
        if self.manifest_missing:
            return True
        return any(c.status is not OperationStatus.OK for c in self.checks)

    def drifted_checks(self) -> list[OperationCheck]:
        return [c for c in self.checks if c.status is not OperationStatus.OK]


class DoctorError(RuntimeError):
    """Raised when a doctor action cannot proceed (e.g. plugin not installed)."""


def _manifest_path_for(home: Path, plugin_name: str) -> Path:
    return home / _MANIFESTS_DIRNAME / plugin_name / _INSTALL_MANIFEST_FILENAME


def _resolve_home(home: Path | None) -> Path:
    return home if home is not None else state_dir()


def _check_write_file(op: Operation) -> OperationCheck:
    raw_path = op.parameters["path"]
    expected_content = op.parameters["content"]
    path = Path(raw_path)
    if not path.exists():
        return OperationCheck(
            kind="write_file",
            target=raw_path,
            status=OperationStatus.MISSING,
            detail="file does not exist",
        )
    actual = path.read_text(encoding="utf-8")
    if actual != expected_content:
        return OperationCheck(
            kind="write_file",
            target=raw_path,
            status=OperationStatus.DRIFTED,
            detail="file content differs from manifest",
        )
    return OperationCheck(kind="write_file", target=raw_path, status=OperationStatus.OK)


def _check_add_block(op: Operation) -> OperationCheck:
    raw_path = op.parameters["path"]
    plugin_name = op.parameters["plugin_name"]
    fmt = op.parameters.get("format", "markdown")
    expected_content = op.parameters["content"]
    path = Path(raw_path)
    target = f"{raw_path}#{plugin_name}"
    if not path.exists():
        return OperationCheck(
            kind="add_block",
            target=target,
            status=OperationStatus.MISSING,
            detail="host file does not exist",
        )
    syntax = get_format(fmt)
    block = find_block(path.read_text(encoding="utf-8"), plugin_name, syntax)
    if block is None:
        return OperationCheck(
            kind="add_block",
            target=target,
            status=OperationStatus.MISSING,
            detail="plugin block is absent",
        )
    if block != expected_content:
        return OperationCheck(
            kind="add_block",
            target=target,
            status=OperationStatus.DRIFTED,
            detail="block content differs from manifest",
        )
    return OperationCheck(kind="add_block", target=target, status=OperationStatus.OK)


def _check_run_pkg(op: Operation, components: PlatformComponents) -> OperationCheck:
    package = op.parameters["package"]
    is_cask = bool(op.parameters.get("cask", False))
    pm = components.package_manager
    if is_cask:
        checker = getattr(pm, "is_cask_installed", None)
        installed = bool(checker(package)) if checker is not None else False
    else:
        installed = pm.is_installed(package)
    if installed:
        return OperationCheck(kind="run_pkg", target=package, status=OperationStatus.OK)
    return OperationCheck(
        kind="run_pkg",
        target=package,
        status=OperationStatus.MISSING,
        detail="cask is not installed" if is_cask else "package is not installed",
    )


def _check_register_task(op: Operation, components: PlatformComponents) -> OperationCheck:
    name = op.parameters["name"]
    if components.scheduler.is_registered(name):
        return OperationCheck(kind="register_task", target=name, status=OperationStatus.OK)
    return OperationCheck(
        kind="register_task",
        target=name,
        status=OperationStatus.MISSING,
        detail="scheduled task is not registered",
    )


def _check_operation(op: Operation, components: PlatformComponents) -> OperationCheck | None:
    if op.kind == "write_file":
        return _check_write_file(op)
    if op.kind == "add_block":
        return _check_add_block(op)
    if op.kind == "run_pkg":
        return _check_run_pkg(op, components)
    if op.kind == "register_task":
        return _check_register_task(op, components)
    return None  # pragma: no cover - guarded by Operation kind validation


def check_plugin(
    plugin: InstalledPlugin,
    *,
    home: Path | None = None,
    components: PlatformComponents | None = None,
) -> DriftReport:
    """Build a :class:`DriftReport` for ``plugin`` against disk.

    A plugin whose install manifest is missing is reported with
    ``manifest_missing=True`` and an empty ``checks`` list; the caller
    decides whether to treat that as repair-by-reinstall or fail.
    """
    base = _resolve_home(home)
    components = components if components is not None else make_platform_components()
    manifest_path = _manifest_path_for(base, plugin.name)
    if not manifest_path.exists():
        return DriftReport(
            plugin_name=plugin.name,
            version=plugin.version,
            checks=[],
            manifest_missing=True,
        )
    install_manifest = load_install_manifest(manifest_path)
    checks: list[OperationCheck] = []
    for op in install_manifest.operations:
        result = _check_operation(op, components)
        if result is not None:
            checks.append(result)
    return DriftReport(
        plugin_name=plugin.name,
        version=install_manifest.version,
        checks=checks,
    )


def run_doctor(
    *,
    home: Path | None = None,
    components: PlatformComponents | None = None,
) -> list[DriftReport]:
    """Walk ``state.yaml`` and return one :class:`DriftReport` per installed plugin."""
    base = _resolve_home(home)
    state_path = base / _STATE_FILENAME
    if not state_path.exists():
        return []
    state = load_state(state_path)
    components = components if components is not None else make_platform_components()
    reports: list[DriftReport] = []
    for record in sorted(state.installed_plugins, key=lambda p: p.name):
        reports.append(check_plugin(record, home=base, components=components))
    return reports


def _accept_operation(op: Operation, components: PlatformComponents) -> Operation | None:
    """Return the operation as it should appear after accepting current disk.

    ``None`` means the resource is gone; the caller drops the operation
    from the rewritten manifest.
    """
    if op.kind == "write_file":
        path = Path(op.parameters["path"])
        if not path.exists():
            return None
        new_content = path.read_text(encoding="utf-8")
        if new_content == op.parameters["content"]:
            return op
        new_params = dict(op.parameters)
        new_params["content"] = new_content
        return Operation(
            kind=op.kind,
            parameters=new_params,
            inverse=dict(op.inverse),
            pre_existing=op.pre_existing,
        )
    if op.kind == "add_block":
        path = Path(op.parameters["path"])
        if not path.exists():
            return None
        plugin_name = op.parameters["plugin_name"]
        fmt = op.parameters.get("format", "markdown")
        block = find_block(path.read_text(encoding="utf-8"), plugin_name, get_format(fmt))
        if block is None:
            return None
        if block == op.parameters["content"]:
            return op
        new_params = dict(op.parameters)
        new_params["content"] = block
        return Operation(
            kind=op.kind,
            parameters=new_params,
            inverse=dict(op.inverse),
            pre_existing=op.pre_existing,
        )
    if op.kind == "run_pkg":
        is_cask = bool(op.parameters.get("cask", False))
        pm = components.package_manager
        if is_cask:
            checker = getattr(pm, "is_cask_installed", None)
            installed = bool(checker(op.parameters["package"])) if checker is not None else False
        else:
            installed = pm.is_installed(op.parameters["package"])
        if installed:
            return op
        return None
    if op.kind == "register_task":
        if components.scheduler.is_registered(op.parameters["name"]):
            return op
        return None
    if op.kind == "run_command":
        # Generic commands have no portable presence check — keep the
        # recorded op as-is and rely on the plugin's verify hook for
        # deeper checks.
        return op
    return op  # pragma: no cover - guarded by Operation kind validation


def accept_plugin(
    plugin_name: str,
    *,
    home: Path | None = None,
    components: PlatformComponents | None = None,
) -> DriftReport:
    """Rewrite ``plugin_name``'s install manifest to match the current disk.

    Drifted operations have their parameters updated in place; operations
    whose resource has gone missing are dropped. Returns a fresh
    :class:`DriftReport` for the plugin, which should be clean.
    """
    base = _resolve_home(home)
    components = components if components is not None else make_platform_components()

    state_path = base / _STATE_FILENAME
    if not state_path.exists():
        raise DoctorError(f"plugin {plugin_name!r} is not installed")
    state = load_state(state_path)
    record = next((p for p in state.installed_plugins if p.name == plugin_name), None)
    if record is None:
        raise DoctorError(f"plugin {plugin_name!r} is not installed")

    manifest_path = _manifest_path_for(base, plugin_name)
    if not manifest_path.exists():
        raise DoctorError(
            f"install manifest for {plugin_name!r} is missing at {manifest_path}; "
            "cannot accept without a manifest to rewrite"
        )
    install_manifest = load_install_manifest(manifest_path)

    new_ops: list[Operation] = []
    for op in install_manifest.operations:
        accepted = _accept_operation(op, components)
        if accepted is not None:
            new_ops.append(accepted)
    new_manifest = InstallManifest(
        plugin_name=install_manifest.plugin_name,
        version=install_manifest.version,
        operations=new_ops,
        installed_at=install_manifest.installed_at,
    )
    save_install_manifest(new_manifest, manifest_path)

    return check_plugin(record, home=base, components=components)


def repair_plugin(
    plugin_name: str,
    *,
    home: Path | None = None,
    in_tree_root: Path | None = None,
    taps_root: Path | None = None,
    components: PlatformComponents | None = None,
    auth=None,
    now: datetime | None = None,
) -> None:
    """Re-run ``plugin_name``'s install hook through the engine.

    Delegates to :func:`cerebro.runtime.engine.reinstall_plugin` so the
    re-run is transactional: a failing repair rolls back the partial
    work and leaves the existing install manifest untouched.
    """
    # Imported lazily to avoid an engine -> doctor import cycle if the
    # engine ever needs to call into the doctor module.
    from cerebro.runtime.engine import reinstall_plugin

    reinstall_plugin(
        plugin_name,
        home=home,
        in_tree_root=in_tree_root,
        taps_root=taps_root,
        components=components,
        auth=auth,
        now=now if now is not None else datetime.now(tz=UTC),
    )


def _summarize_counts(checks: Iterable[OperationCheck]) -> dict[str, int]:
    counts = {"ok": 0, "drifted": 0, "missing": 0}
    for c in checks:
        counts[c.status.value] += 1
    return counts


def report_to_dict(report: DriftReport) -> dict:
    """Serialize ``report`` for ``--json`` output."""
    return {
        "plugin": report.plugin_name,
        "version": report.version,
        "drifted": report.is_drifted,
        "manifest_missing": report.manifest_missing,
        "summary": _summarize_counts(report.checks),
        "checks": [
            {
                "kind": c.kind,
                "target": c.target,
                "status": c.status.value,
                "detail": c.detail,
            }
            for c in report.checks
        ],
    }


__all__ = [
    "DoctorError",
    "DriftReport",
    "OperationCheck",
    "OperationStatus",
    "accept_plugin",
    "check_plugin",
    "repair_plugin",
    "report_to_dict",
    "run_doctor",
]
