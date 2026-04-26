"""The ``ctx`` object passed to every plugin hook.

A ``HookContext`` exposes the read-only state, queries over installed
plugins, the helper objects that record side effects, a logger, and the
recorder itself (as ``ctx.manifest``). The helpers are documented on
their own classes in ``cerebro.runtime.recorder``.

``build_context`` is the canonical constructor. The install engine
(SPEC-06) and tests use it; plugins do not.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from cerebro.models import CerebroState, InstalledPlugin, PluginManifest, PluginType
from cerebro.runtime.auth import AuthHandoff, NullAuthHandoff
from cerebro.runtime.platform import PackageManager
from cerebro.runtime.recorder import (
    AuthHandoffHelper,
    BlocksHelper,
    FilesystemHelper,
    OperationRecorder,
    PackageManagerHelper,
    ScheduledTaskHelper,
)
from cerebro.runtime.scheduling import Scheduler

ManifestLookup = Callable[[str], PluginManifest | None]


def _no_manifest(_: str) -> PluginManifest | None:
    return None


@dataclass
class HookContext:
    state: CerebroState
    pkg: PackageManagerHelper
    fs: FilesystemHelper
    blocks: BlocksHelper
    tasks: ScheduledTaskHelper
    auth: AuthHandoffHelper
    log: logging.Logger
    manifest: OperationRecorder
    _manifest_lookup: ManifestLookup = field(default=_no_manifest)

    def installed_plugins(self) -> list[InstalledPlugin]:
        return list(self.state.installed_plugins)

    def plugins_of_type(self, plugin_type: PluginType) -> list[InstalledPlugin]:
        out: list[InstalledPlugin] = []
        for record in self.state.installed_plugins:
            manifest = self._manifest_lookup(record.name)
            if manifest is not None and manifest.type == plugin_type:
                out.append(record)
        return out


def build_context(
    *,
    plugin: PluginManifest,
    state: CerebroState,
    package_manager: PackageManager,
    scheduler: Scheduler,
    when: datetime,
    auth: AuthHandoff | None = None,
    manifest_lookup: ManifestLookup | None = None,
    logger: logging.Logger | None = None,
) -> HookContext:
    recorder = OperationRecorder(
        plugin_name=plugin.name,
        plugin_version=plugin.version,
        when=when,
        package_manager=package_manager,
        scheduler=scheduler,
    )
    return HookContext(
        state=state,
        pkg=PackageManagerHelper(recorder, package_manager),
        fs=FilesystemHelper(recorder),
        blocks=BlocksHelper(recorder),
        tasks=ScheduledTaskHelper(recorder, scheduler),
        auth=AuthHandoffHelper(auth or NullAuthHandoff()),
        log=logger or logging.getLogger(f"cerebro.plugin.{plugin.name}"),
        manifest=recorder,
        _manifest_lookup=manifest_lookup or _no_manifest,
    )


__all__ = ["HookContext", "ManifestLookup", "build_context"]
