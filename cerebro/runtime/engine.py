"""Transactional install and uninstall engine.

The engine drives the full plugin lifecycle (ADR-0007, ADR-0008): it
resolves dependencies, runs ``install`` / ``uninstall`` / ``configure``
hooks against a ``HookContext``, persists per-plugin install manifests,
updates ``state.yaml`` only on success, and replays inverses in reverse
on failure.

Two transaction layers exist. The inner layer is per-plugin: every
operation a plugin performs through ``ctx`` is recorded, and that
plugin's own recorder rolls back if its hook raises. The outer layer is
the ``cerebro install`` invocation as a whole: if plugin Y fails partway
through a multi-plugin install order (deps first), prior plugins that
already finished are also rolled back, in reverse order, so the system
ends as it started.

Reconfigure-failure policy
==========================

After a successful install the engine re-runs the ``configure`` hooks of
plugins that target the newly installed plugin's type (ADR-0004). If one
of those reconfigures fails we DO NOT roll back the install. Rationale:
the install itself succeeded and is internally consistent; punishing the
just-installed plugin (and forcing the user to redo the work) because a
sibling plugin's configure hook is broken would be more disruptive than
leaving the failure surfaced for repair. The failed plugin's own
recorder still rolls back its partial reconfigure, so its on-disk state
is unchanged. The caller sees a ``ReconfigureError`` aggregating which
plugins failed; the user can fix them and re-run configure (e.g. via a
future ``cerebro doctor``).
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from cerebro.models import (
    CerebroState,
    InstalledPlugin,
    InstallManifest,
    PluginManifest,
)
from cerebro.plugins.loader import (
    DiscoveredPlugin,
    discover_plugins,
    load_plugin_module,
)
from cerebro.plugins.resolver import (
    find_dependents,
    find_targeting,
    resolve_install_order,
)
from cerebro.runtime.auth import AuthHandoff, NullAuthHandoff
from cerebro.runtime.blocks import get_format, remove_block
from cerebro.runtime.context import HookContext, build_context
from cerebro.runtime.platform import (
    PackageManager,
    PlatformComponents,
    make_platform_components,
)
from cerebro.runtime.scheduling import Scheduler
from cerebro.state import (
    load_install_manifest,
    load_state,
    save_install_manifest,
    save_state,
    state_dir,
)

_STATE_FILENAME = "state.yaml"
_MANIFESTS_DIRNAME = "manifests"
_INSTALL_MANIFEST_FILENAME = "install.yaml"
_LOGS_DIRNAME = "logs"


class EngineError(RuntimeError):
    """Base class for engine-raised errors."""


class PluginInstallError(EngineError):
    """A single plugin's install hook raised; that plugin's changes were rolled back."""

    def __init__(self, plugin_name: str, original: BaseException) -> None:
        super().__init__(f"install of plugin {plugin_name!r} failed: {original}")
        self.plugin_name = plugin_name
        self.original = original


class TransactionRollbackError(EngineError):
    """A multi-plugin install transaction was rolled back after a mid-transaction failure."""

    def __init__(self, failed_plugin: str, rolled_back: list[str], original: BaseException) -> None:
        rolled = ", ".join(rolled_back) if rolled_back else "none"
        super().__init__(
            f"install transaction failed at plugin {failed_plugin!r}: {original}; "
            f"rolled back prior plugins (reverse order): {rolled}"
        )
        self.failed_plugin = failed_plugin
        self.rolled_back = list(rolled_back)
        self.original = original


class DependencyInUseError(EngineError):
    """Cannot uninstall a plugin that other installed plugins depend on."""

    def __init__(self, plugin_name: str, dependents: list[str]) -> None:
        listed = ", ".join(sorted(dependents))
        super().__init__(
            f"cannot uninstall {plugin_name!r}: still required by {listed}"
        )
        self.plugin_name = plugin_name
        self.dependents = list(dependents)


class ReconfigureError(EngineError):
    """One or more targeting plugins failed during the post-install reconfigure pass."""

    def __init__(self, failures: dict[str, BaseException]) -> None:
        listed = ", ".join(f"{name}: {exc}" for name, exc in sorted(failures.items()))
        super().__init__(f"reconfigure failed for: {listed}")
        self.failures = dict(failures)


@dataclass
class _InstalledRecord:
    """Bookkeeping for a plugin that finished install in the current transaction."""

    name: str
    manifest: PluginManifest
    install_manifest: InstallManifest
    state_record: InstalledPlugin
    manifest_path: Path
    previous_manifest_yaml: bytes | None
    module: ModuleType


@dataclass
class _Paths:
    home: Path
    state_path: Path
    manifests_dir: Path
    logs_dir: Path

    @classmethod
    def resolve(cls, home: Path | None = None) -> _Paths:
        base = home if home is not None else state_dir()
        return cls(
            home=base,
            state_path=base / _STATE_FILENAME,
            manifests_dir=base / _MANIFESTS_DIRNAME,
            logs_dir=base / _LOGS_DIRNAME,
        )


@dataclass
class _EngineLogger:
    """Structured (JSON-lines) log for one engine invocation."""

    path: Path
    _fh: Any = None
    _python_logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("cerebro.engine")
    )

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def event(self, _event: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(tz=UTC).isoformat(),
            "event": _event,
            **fields,
        }
        line = json.dumps(record, default=str, sort_keys=True)
        if self._fh is not None:
            self._fh.write(line + "\n")
            self._fh.flush()
        self._python_logger.debug(line)

    def child_for_plugin(self, plugin_name: str) -> logging.Logger:
        return self._python_logger.getChild(plugin_name)


def _open_log(paths: _Paths, command: str) -> _EngineLogger:
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    log_path = paths.logs_dir / f"{stamp}-{command}.log"
    log = _EngineLogger(path=log_path)
    log.open()
    return log


def _empty_state(core_version: str = "0.0.0") -> CerebroState:
    return CerebroState(core_version=core_version, vault_path=Path("~/.cerebro/vault").expanduser())


def _read_state(paths: _Paths) -> CerebroState:
    if paths.state_path.exists():
        return load_state(paths.state_path)
    return _empty_state()


def _manifest_path_for(paths: _Paths, plugin_name: str) -> Path:
    return paths.manifests_dir / plugin_name / _INSTALL_MANIFEST_FILENAME


def _read_manifest_bytes(path: Path) -> bytes | None:
    if not path.exists():
        return None
    return path.read_bytes()


def _restore_manifest(path: Path, previous: bytes | None) -> None:
    if previous is None:
        if path.exists():
            path.unlink()
        # remove the now-empty plugin manifest dir if we created it
        try:
            path.parent.rmdir()
        except OSError:
            pass
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(previous)


def _state_with(state: CerebroState, record: InstalledPlugin) -> CerebroState:
    plugins = [p for p in state.installed_plugins if p.name != record.name]
    plugins.append(record)
    return CerebroState(
        core_version=state.core_version,
        vault_path=state.vault_path,
        installed_plugins=plugins,
    )


def _state_without(state: CerebroState, plugin_name: str) -> CerebroState:
    plugins = [p for p in state.installed_plugins if p.name != plugin_name]
    return CerebroState(
        core_version=state.core_version,
        vault_path=state.vault_path,
        installed_plugins=plugins,
    )


def _manifest_lookup_factory(
    available: dict[str, DiscoveredPlugin],
):
    def lookup(name: str) -> PluginManifest | None:
        d = available.get(name)
        return d.manifest if d is not None else None

    return lookup


def _replay_install_manifest(
    install_manifest: InstallManifest,
    *,
    package_manager: PackageManager,
    scheduler: Scheduler,
    log: _EngineLogger,
) -> None:
    """Replay the inverses of a persisted install manifest in reverse order.

    Used by ``uninstall`` and by the cross-plugin rollback path.
    """
    for op in reversed(install_manifest.operations):
        if op.pre_existing:
            log.event(
                "uninstall.skip_pre_existing",
                plugin=install_manifest.plugin_name,
                kind=op.kind,
            )
            continue
        try:
            _invert_persisted_op(op, package_manager=package_manager, scheduler=scheduler)
            log.event(
                "uninstall.invert",
                plugin=install_manifest.plugin_name,
                kind=op.kind,
            )
        except Exception as exc:  # noqa: BLE001
            log.event(
                "uninstall.invert_failed",
                plugin=install_manifest.plugin_name,
                kind=op.kind,
                error=str(exc),
            )


def _invert_persisted_op(
    op: Any,
    *,
    package_manager: PackageManager,
    scheduler: Scheduler,
) -> None:
    if op.kind == "write_file":
        path = Path(op.inverse["path"])
        previous = op.inverse.get("previous_content")
        if previous is None:
            if path.exists():
                path.unlink()
        else:
            path.write_text(previous, encoding="utf-8")
        for raw in reversed(list(op.inverse.get("created_dirs", []))):
            try:
                Path(raw).rmdir()
            except OSError:
                pass
    elif op.kind == "add_block":
        # Always strip just our plugin's block — sibling plugins may have
        # added their own blocks to the same file since install. If the
        # file is now empty AND we were the ones who created it
        # (previous_content is None), remove the file and any directories
        # we created on the way.
        path = Path(op.inverse["path"])
        previous_content = op.inverse.get("previous_content")
        if not path.exists():
            return
        params = op.parameters
        plugin_name = params["plugin_name"]
        fmt = params.get("format", "markdown")
        text = path.read_text(encoding="utf-8")
        new_text = remove_block(text, plugin_name, get_format(fmt))
        if previous_content is None and not new_text.strip():
            path.unlink()
            for raw in reversed(list(op.inverse.get("created_dirs", []))):
                try:
                    Path(raw).rmdir()
                except OSError:
                    pass
        else:
            path.write_text(new_text, encoding="utf-8")
    elif op.kind == "run_pkg":
        if op.inverse.get("cask"):
            uninstall_cask = getattr(package_manager, "uninstall_cask", None)
            if uninstall_cask is None:
                raise RuntimeError(
                    f"cannot invert cask install of {op.inverse['package']!r}: "
                    "package manager has no uninstall_cask"
                )
            uninstall_cask(op.inverse["package"])
        else:
            package_manager.uninstall(op.inverse["package"])
    elif op.kind == "register_task":
        scheduler.unregister(op.inverse["name"])
    elif op.kind == "run_command":
        subprocess.run(list(op.inverse["argv"]), check=True)
    else:  # pragma: no cover - guarded by Operation kind validation
        raise ValueError(f"unknown operation kind: {op.kind!r}")


def _build_ctx(
    *,
    plugin: PluginManifest,
    state: CerebroState,
    components: PlatformComponents,
    available: dict[str, DiscoveredPlugin],
    auth: AuthHandoff,
    when: datetime,
    logger: logging.Logger,
) -> HookContext:
    return build_context(
        plugin=plugin,
        state=state,
        package_manager=components.package_manager,
        scheduler=components.scheduler,
        when=when,
        auth=auth,
        manifest_lookup=_manifest_lookup_factory(available),
        logger=logger,
    )


def install(
    plugin_name: str,
    *,
    home: Path | None = None,
    in_tree_root: Path | None = None,
    taps_root: Path | None = None,
    components: PlatformComponents | None = None,
    auth: AuthHandoff | None = None,
    now: datetime | None = None,
) -> None:
    """Install ``plugin_name`` and any missing dependencies.

    Resolves install order, runs ``install`` hooks one at a time, and on
    any failure rolls back the failing plugin and every plugin already
    installed earlier in this transaction. After all installs commit, the
    targeting reconfigure pass runs (see module docstring).
    """
    paths = _Paths.resolve(home)
    log = _open_log(paths, "install")
    try:
        components = components if components is not None else make_platform_components()
        auth_handoff: AuthHandoff = auth if auth is not None else NullAuthHandoff()
        when = now if now is not None else datetime.now(tz=UTC)

        state = _read_state(paths)
        available = discover_plugins(state, in_tree_root=in_tree_root, taps_root=taps_root)
        log.event("install.begin", plugin=plugin_name)
        order = resolve_install_order(plugin_name, available, state.installed_plugins)
        log.event("install.order", order=[m.name for m in order])

        committed: list[_InstalledRecord] = []
        try:
            for manifest in order:
                committed.append(
                    _install_one(
                        manifest=manifest,
                        available=available,
                        state=state,
                        components=components,
                        auth=auth_handoff,
                        paths=paths,
                        when=when,
                        log=log,
                    )
                )
                # Subsequent plugins should see the just-installed plugin
                # in state and be able to look its manifest up.
                state = _state_with(state, committed[-1].state_record)
        except BaseException as exc:  # noqa: BLE001
            failed = order[len(committed)].name if len(committed) < len(order) else plugin_name
            rolled = _rollback_committed(committed, components, paths, log)
            log.event("install.transaction_failed", failed=failed, rolled_back=rolled)
            if isinstance(exc, PluginInstallError):
                raise TransactionRollbackError(exc.plugin_name, rolled, exc.original) from exc
            raise TransactionRollbackError(failed, rolled, exc) from exc

        # All installs committed: targeting reconfigure pass.
        last = committed[-1]
        try:
            reconfigure_targeting(
                last.manifest,
                home=paths.home,
                in_tree_root=in_tree_root,
                taps_root=taps_root,
                components=components,
                auth=auth_handoff,
                now=when,
                log=log,
            )
        except ReconfigureError as exc:
            log.event(
                "install.reconfigure_failed",
                plugin=last.name,
                failures=sorted(exc.failures),
            )
            # Per module docstring: do NOT roll back the just-completed
            # install on reconfigure failure. Surface the failure to the
            # caller so the user can repair the broken targeting plugin.
            raise

        log.event("install.complete", plugin=plugin_name)
    finally:
        log.close()


def _install_one(
    *,
    manifest: PluginManifest,
    available: dict[str, DiscoveredPlugin],
    state: CerebroState,
    components: PlatformComponents,
    auth: AuthHandoff,
    paths: _Paths,
    when: datetime,
    log: _EngineLogger,
) -> _InstalledRecord:
    discovered = available[manifest.name]
    module = load_plugin_module(discovered)
    install_hook = getattr(module, "install", None)
    if install_hook is None or not callable(install_hook):
        raise EngineError(
            f"plugin {manifest.name!r} does not define a callable install() hook"
        )

    plugin_logger = log.child_for_plugin(manifest.name)
    ctx = _build_ctx(
        plugin=manifest,
        state=state,
        components=components,
        available=available,
        auth=auth,
        when=when,
        logger=plugin_logger,
    )
    log.event("plugin.install.begin", plugin=manifest.name)
    try:
        install_hook(ctx)
    except BaseException as exc:  # noqa: BLE001
        log.event("plugin.install.failed", plugin=manifest.name, error=str(exc))
        ctx.manifest.rollback()
        raise PluginInstallError(manifest.name, exc) from exc

    install_manifest = ctx.manifest.commit()
    manifest_path = _manifest_path_for(paths, manifest.name)
    previous_manifest_yaml = _read_manifest_bytes(manifest_path)
    save_install_manifest(install_manifest, manifest_path)

    state_record = InstalledPlugin(
        name=manifest.name,
        source=discovered.source,
        version=manifest.version,
        installed_at=when,
        enabled=True,
    )
    new_state = _state_with(state, state_record)
    save_state(new_state, paths.state_path)

    log.event("plugin.install.commit", plugin=manifest.name, version=manifest.version)
    return _InstalledRecord(
        name=manifest.name,
        manifest=manifest,
        install_manifest=install_manifest,
        state_record=state_record,
        manifest_path=manifest_path,
        previous_manifest_yaml=previous_manifest_yaml,
        module=module,
    )


def _rollback_committed(
    committed: list[_InstalledRecord],
    components: PlatformComponents,
    paths: _Paths,
    log: _EngineLogger,
) -> list[str]:
    """Replay committed plugins' inverses in reverse order. Return rolled-back names."""
    rolled: list[str] = []
    for record in reversed(committed):
        log.event("transaction.rollback.plugin", plugin=record.name)
        _replay_install_manifest(
            record.install_manifest,
            package_manager=components.package_manager,
            scheduler=components.scheduler,
            log=log,
        )
        _restore_manifest(record.manifest_path, record.previous_manifest_yaml)
        rolled.append(record.name)

    if committed:
        # Reset state.yaml to the value before the first committed plugin.
        # Easier and safer than diffing: re-read whatever is on disk and
        # remove every name we just committed.
        state = _read_state(paths)
        for record in committed:
            state = _state_without(state, record.name)
        save_state(state, paths.state_path)
    return rolled


def uninstall(
    plugin_name: str,
    *,
    home: Path | None = None,
    in_tree_root: Path | None = None,
    taps_root: Path | None = None,
    components: PlatformComponents | None = None,
    auth: AuthHandoff | None = None,
    now: datetime | None = None,
) -> None:
    """Uninstall ``plugin_name``.

    Refuses if any installed plugin depends on this one. Otherwise
    replays the persisted install manifest's inverses, removes the
    plugin from ``state.yaml``, and re-runs targeting plugins'
    ``configure`` hooks so they no longer reference the removed plugin.
    """
    paths = _Paths.resolve(home)
    log = _open_log(paths, "uninstall")
    try:
        components = components if components is not None else make_platform_components()
        auth_handoff: AuthHandoff = auth if auth is not None else NullAuthHandoff()
        when = now if now is not None else datetime.now(tz=UTC)

        state = _read_state(paths)
        installed_record = next(
            (p for p in state.installed_plugins if p.name == plugin_name), None
        )
        if installed_record is None:
            raise EngineError(f"plugin {plugin_name!r} is not installed")

        available = discover_plugins(state, in_tree_root=in_tree_root, taps_root=taps_root)
        dependents = find_dependents(plugin_name, state.installed_plugins, available)
        if dependents:
            raise DependencyInUseError(plugin_name, [d.name for d in dependents])

        # Capture the manifest of the plugin being removed before we
        # touch anything; we use it for the post-uninstall reconfigure.
        removed_manifest = available[plugin_name].manifest if plugin_name in available else None

        manifest_path = _manifest_path_for(paths, plugin_name)
        if not manifest_path.exists():
            raise EngineError(
                f"install manifest for {plugin_name!r} is missing at {manifest_path}; "
                "cannot uninstall without it"
            )
        install_manifest = load_install_manifest(manifest_path)

        log.event("uninstall.begin", plugin=plugin_name)
        _replay_install_manifest(
            install_manifest,
            package_manager=components.package_manager,
            scheduler=components.scheduler,
            log=log,
        )

        manifest_path.unlink()
        try:
            manifest_path.parent.rmdir()
        except OSError:
            pass

        new_state = _state_without(state, plugin_name)
        save_state(new_state, paths.state_path)
        log.event("uninstall.state_updated", plugin=plugin_name)

        if removed_manifest is not None:
            try:
                reconfigure_targeting(
                    removed_manifest,
                    home=paths.home,
                    in_tree_root=in_tree_root,
                    taps_root=taps_root,
                    components=components,
                    auth=auth_handoff,
                    now=when,
                    log=log,
                )
            except ReconfigureError as exc:
                log.event(
                    "uninstall.reconfigure_failed",
                    plugin=plugin_name,
                    failures=sorted(exc.failures),
                )
                raise

        log.event("uninstall.complete", plugin=plugin_name)
    finally:
        log.close()


def reconfigure_targeting(
    plugin: PluginManifest,
    *,
    home: Path | None = None,
    in_tree_root: Path | None = None,
    taps_root: Path | None = None,
    components: PlatformComponents | None = None,
    auth: AuthHandoff | None = None,
    now: datetime | None = None,
    log: _EngineLogger | None = None,
) -> None:
    """Re-run ``configure`` hooks of plugins that target ``plugin``'s type.

    Each targeting plugin runs in its own transactional context. If the
    configure hook raises, that plugin's recorder rolls back the partial
    changes and the failure is collected; we do not stop the pass on the
    first error so that one broken plugin cannot block the others.
    """
    paths = _Paths.resolve(home)
    owns_log = log is None
    if owns_log:
        log = _open_log(paths, "reconfigure")
    assert log is not None
    try:
        components = components if components is not None else make_platform_components()
        auth_handoff: AuthHandoff = auth if auth is not None else NullAuthHandoff()
        when = now if now is not None else datetime.now(tz=UTC)

        state = _read_state(paths)
        available = discover_plugins(state, in_tree_root=in_tree_root, taps_root=taps_root)
        targeting = find_targeting(plugin, state.installed_plugins, available)
        log.event(
            "reconfigure.begin",
            new_plugin=plugin.name,
            targeting=[r.name for r in targeting],
        )
        if not targeting:
            return

        failures: dict[str, BaseException] = {}
        for record in targeting:
            discovered = available.get(record.name)
            if discovered is None:
                # Source went away; nothing we can do here.
                continue
            module = load_plugin_module(discovered)
            configure_hook = getattr(module, "configure", None)
            if not callable(configure_hook):
                # Targeting plugins are allowed to omit configure; only
                # plugins that *declare* configure get called.
                continue

            ctx = _build_ctx(
                plugin=discovered.manifest,
                state=state,
                components=components,
                available=available,
                auth=auth_handoff,
                when=when,
                logger=log.child_for_plugin(record.name),
            )
            log.event("reconfigure.plugin.begin", plugin=record.name)
            try:
                configure_hook(ctx)
            except BaseException as exc:  # noqa: BLE001
                ctx.manifest.rollback()
                failures[record.name] = exc
                log.event(
                    "reconfigure.plugin.failed",
                    plugin=record.name,
                    error=str(exc),
                )
                continue
            # Configure side effects extend the existing install manifest
            # so an eventual uninstall undoes them too.
            extra = ctx.manifest.commit()
            manifest_path = _manifest_path_for(paths, record.name)
            if manifest_path.exists():
                existing = load_install_manifest(manifest_path)
                merged = InstallManifest(
                    plugin_name=existing.plugin_name,
                    version=existing.version,
                    operations=list(existing.operations) + list(extra.operations),
                    installed_at=existing.installed_at,
                )
                save_install_manifest(merged, manifest_path)
            else:
                save_install_manifest(extra, manifest_path)
            log.event("reconfigure.plugin.commit", plugin=record.name)

        if failures:
            raise ReconfigureError(failures)
    finally:
        if owns_log:
            log.close()


def reinstall_plugin(
    plugin_name: str,
    *,
    home: Path | None = None,
    in_tree_root: Path | None = None,
    taps_root: Path | None = None,
    components: PlatformComponents | None = None,
    auth: AuthHandoff | None = None,
    now: datetime | None = None,
) -> None:
    """Re-run a single installed plugin's ``install`` hook.

    Used by ``cerebro doctor --action repair`` to bring drifted on-disk
    state back into agreement with the install manifest. Transactional:
    the recorder rolls back partial changes if the hook raises, and the
    existing install manifest is only replaced once the new run commits.

    Skips the dependency resolver (we are operating on an already
    installed plugin) and the post-install reconfigure pass (no new
    plugin type was added). Raises :class:`EngineError` if the plugin
    is not installed or its source is no longer discoverable.
    """
    paths = _Paths.resolve(home)
    log = _open_log(paths, "repair")
    try:
        components = components if components is not None else make_platform_components()
        auth_handoff: AuthHandoff = auth if auth is not None else NullAuthHandoff()
        when = now if now is not None else datetime.now(tz=UTC)

        state = _read_state(paths)
        if not any(p.name == plugin_name for p in state.installed_plugins):
            raise EngineError(f"plugin {plugin_name!r} is not installed")

        available = discover_plugins(state, in_tree_root=in_tree_root, taps_root=taps_root)
        if plugin_name not in available:
            raise EngineError(
                f"plugin {plugin_name!r} is no longer discoverable; "
                "cannot repair without its source"
            )

        manifest = available[plugin_name].manifest
        log.event("repair.begin", plugin=plugin_name)
        _install_one(
            manifest=manifest,
            available=available,
            state=state,
            components=components,
            auth=auth_handoff,
            paths=paths,
            when=when,
            log=log,
        )
        log.event("repair.complete", plugin=plugin_name)
    finally:
        log.close()


__all__ = [
    "DependencyInUseError",
    "EngineError",
    "PluginInstallError",
    "ReconfigureError",
    "TransactionRollbackError",
    "install",
    "reconfigure_targeting",
    "reinstall_plugin",
    "uninstall",
]
