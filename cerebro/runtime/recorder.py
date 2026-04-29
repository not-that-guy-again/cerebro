"""Operation recorder and the helpers that feed it.

The recorder is the heart of transactional install (ADR-0007). Every
side effect a hook performs goes through one of the helpers attached to
``ctx``; the helper executes the side effect and records both the
operation and an inverse description. On success ``commit`` returns an
``InstallManifest`` that the engine persists. On failure ``rollback``
replays the inverses in reverse order.

Operations whose resource was already present at install time are
recorded with ``pre_existing=True`` and skipped on rollback so we do not
remove things the user had before Cerebro touched the system.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cerebro.models import InstallManifest, Operation
from cerebro.runtime.auth import AuthHandoff
from cerebro.runtime.blocks import find_block, get_format, remove_block, upsert_block
from cerebro.runtime.pkg_managers import CommandRunner, _default_runner
from cerebro.runtime.platform import PackageManager
from cerebro.runtime.scheduling import Scheduler

_log = logging.getLogger(__name__)


def _ensure_parents(path: Path) -> list[Path]:
    """Create missing parent directories for ``path``. Return what we made."""
    created: list[Path] = []
    missing: list[Path] = []
    p = path.parent
    while not p.exists() and p != p.parent:
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        d.mkdir()
        created.append(d)
    return created


def _restore_file(path: Path, previous_content: str | None) -> None:
    if previous_content is None:
        if path.exists():
            path.unlink()
    else:
        path.write_text(previous_content, encoding="utf-8")


def _remove_created_dirs(created_dirs: list[str]) -> None:
    # Reverse so the deepest directory is removed first.
    for raw in reversed(created_dirs):
        d = Path(raw)
        try:
            d.rmdir()
        except OSError:
            # Non-empty (another plugin or the user dropped something here)
            # or already gone — both are fine, leave the directory alone.
            pass


@dataclass
class _RecorderConfig:
    plugin_name: str
    plugin_version: str
    when: datetime


class OperationRecorder:
    """Collects operations as helpers run, replays inverses on rollback."""

    def __init__(
        self,
        *,
        plugin_name: str,
        plugin_version: str,
        when: datetime,
        package_manager: PackageManager,
        scheduler: Scheduler,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self._cfg = _RecorderConfig(
            plugin_name=plugin_name,
            plugin_version=plugin_version,
            when=when,
        )
        self._package_manager = package_manager
        self._scheduler = scheduler
        self._command_runner = command_runner or _default_runner
        self._operations: list[Operation] = []

    @property
    def plugin_name(self) -> str:
        return self._cfg.plugin_name

    @property
    def operations(self) -> list[Operation]:
        return list(self._operations)

    def record(self, operation: Operation) -> None:
        self._operations.append(operation)

    def commit(self) -> InstallManifest:
        return InstallManifest(
            plugin_name=self._cfg.plugin_name,
            version=self._cfg.plugin_version,
            operations=list(self._operations),
            installed_at=self._cfg.when,
        )

    def rollback(self) -> None:
        for op in reversed(self._operations):
            if op.pre_existing:
                continue
            try:
                self._invert(op)
            except Exception:  # noqa: BLE001
                _log.exception(
                    "rollback step failed for %s op %s; continuing",
                    self._cfg.plugin_name,
                    op.kind,
                )
        self._operations.clear()

    def _invert(self, op: Operation) -> None:
        if op.kind == "write_file":
            self._invert_file_op(op)
        elif op.kind == "add_block":
            self._invert_file_op(op)
        elif op.kind == "run_pkg":
            self._invert_run_pkg(op)
        elif op.kind == "register_task":
            self._invert_register_task(op)
        elif op.kind == "run_command":
            self._invert_run_command(op)
        else:  # pragma: no cover - guarded by Operation kind validation
            raise ValueError(f"unknown operation kind: {op.kind!r}")

    @staticmethod
    def _invert_file_op(op: Operation) -> None:
        path = Path(op.inverse["path"])
        previous = op.inverse.get("previous_content")
        created_dirs = list(op.inverse.get("created_dirs", []))
        _restore_file(path, previous)
        _remove_created_dirs(created_dirs)

    def _invert_run_pkg(self, op: Operation) -> None:
        package = op.inverse["package"]
        if op.inverse.get("cask"):
            uninstall_cask = getattr(self._package_manager, "uninstall_cask", None)
            if uninstall_cask is None:
                raise RuntimeError(
                    f"cannot invert cask install of {package!r}: "
                    "package manager has no uninstall_cask"
                )
            uninstall_cask(package)
        else:
            self._package_manager.uninstall(package)

    def _invert_register_task(self, op: Operation) -> None:
        name = op.inverse["name"]
        self._scheduler.unregister(name)

    def _invert_run_command(self, op: Operation) -> None:
        argv = list(op.inverse["argv"])
        self._command_runner(argv, check=True)


class FilesystemHelper:
    """``ctx.fs``: write files through the recorder so we can roll back."""

    def __init__(self, recorder: OperationRecorder) -> None:
        self._recorder = recorder

    def write_file(self, path: Path | str, content: str) -> None:
        target = Path(path)
        previous_content: str | None
        if target.exists():
            previous_content = target.read_text(encoding="utf-8")
        else:
            previous_content = None
        created_dirs = _ensure_parents(target)
        target.write_text(content, encoding="utf-8")
        self._recorder.record(
            Operation(
                kind="write_file",
                parameters={"path": str(target), "content": content},
                inverse={
                    "path": str(target),
                    "previous_content": previous_content,
                    "created_dirs": [str(d) for d in created_dirs],
                },
            )
        )


class BlocksHelper:
    """``ctx.blocks``: contribute a delimited block to a shared file."""

    def __init__(self, recorder: OperationRecorder) -> None:
        self._recorder = recorder

    def add(
        self,
        path: Path | str,
        plugin_name: str,
        content: str,
        *,
        format: str = "markdown",  # noqa: A002 - matches comment-syntax key
    ) -> None:
        target = Path(path)
        syntax = get_format(format)
        previous_content: str | None
        if target.exists():
            existing = target.read_text(encoding="utf-8")
            previous_content = existing
            previous_block = find_block(existing, plugin_name, syntax)
        else:
            existing = ""
            previous_content = None
            previous_block = None
        new_text = upsert_block(existing, plugin_name, syntax, content)
        created_dirs = _ensure_parents(target)
        target.write_text(new_text, encoding="utf-8")
        self._recorder.record(
            Operation(
                kind="add_block",
                parameters={
                    "path": str(target),
                    "plugin_name": plugin_name,
                    "format": format,
                    "content": content,
                },
                inverse={
                    "path": str(target),
                    "previous_content": previous_content,
                    "previous_block": previous_block,
                    "created_dirs": [str(d) for d in created_dirs],
                },
            )
        )

    @staticmethod
    def remove(
        path: Path | str,
        plugin_name: str,
        *,
        format: str = "markdown",  # noqa: A002
    ) -> None:
        """Remove a plugin's block from ``path`` if present.

        This is a non-recording utility for the install engine and tests
        to use during uninstall replay; hooks themselves should not call
        it directly.
        """
        target = Path(path)
        if not target.exists():
            return
        syntax = get_format(format)
        text = target.read_text(encoding="utf-8")
        new = remove_block(text, plugin_name, syntax)
        target.write_text(new, encoding="utf-8")


class PackageManagerHelper:
    """``ctx.pkg``: install packages through the recorder.

    The plain ``install`` / ``is_installed`` methods route through the
    cross-platform :class:`PackageManager` protocol. ``install_cask`` /
    ``is_cask_installed`` are macOS-only convenience wrappers; they
    delegate to the underlying brew package manager and raise on
    platforms that have no cask concept.
    """

    def __init__(
        self,
        recorder: OperationRecorder,
        package_manager: PackageManager,
    ) -> None:
        self._recorder = recorder
        self._pm = package_manager

    def is_installed(self, package: str) -> bool:
        return self._pm.is_installed(package)

    def install(self, package: str) -> None:
        result = self._pm.install(package)
        self._recorder.record(
            Operation(
                kind="run_pkg",
                parameters={"action": "install", "package": package},
                inverse={"action": "uninstall", "package": package},
                pre_existing=result.already_installed,
            )
        )

    def is_cask_installed(self, package: str) -> bool:
        checker = getattr(self._pm, "is_cask_installed", None)
        if checker is None:
            return False
        return bool(checker(package))

    def install_cask(self, package: str) -> None:
        installer = getattr(self._pm, "install_cask", None)
        if installer is None:
            raise RuntimeError(
                f"install_cask({package!r}) requires a brew-style package manager"
            )
        result = installer(package)
        self._recorder.record(
            Operation(
                kind="run_pkg",
                parameters={"action": "install", "package": package, "cask": True},
                inverse={"action": "uninstall", "package": package, "cask": True},
                pre_existing=result.already_installed,
            )
        )


class ScheduledTaskHelper:
    """``ctx.tasks``: register scheduled tasks through the recorder."""

    def __init__(
        self,
        recorder: OperationRecorder,
        scheduler: Scheduler,
    ) -> None:
        self._recorder = recorder
        self._scheduler = scheduler

    def is_registered(self, name: str) -> bool:
        """Read-only check of whether ``name`` is currently registered.

        Plugins use this in ``verify`` hooks to confirm previously
        registered tasks are still present. Not recorded as an operation.
        """
        return self._scheduler.is_registered(name)

    def register(self, name: str, schedule: str, command: str) -> None:
        already = self._scheduler.is_registered(name)
        if not already:
            self._scheduler.register(name, schedule, command)
        self._recorder.record(
            Operation(
                kind="register_task",
                parameters={
                    "name": name,
                    "schedule": schedule,
                    "command": command,
                },
                inverse={"name": name},
                pre_existing=already,
            )
        )


class CommandHelper:
    """``ctx.cmd``: run an arbitrary command and record an inverse for rollback.

    Use this for tools that have no first-class helper (e.g.
    ``code --install-extension`` for VSCode). The plugin supplies the
    inverse argv; the recorder runs it on rollback or uninstall. Mark
    ``pre_existing=True`` when the resource was already in the desired
    state so the inverse is skipped — same semantics as ``ctx.pkg``.
    """

    def __init__(
        self,
        recorder: OperationRecorder,
        runner: CommandRunner | None = None,
    ) -> None:
        self._recorder = recorder
        self._run = runner or _default_runner

    def run(
        self,
        argv: Sequence[str],
        *,
        inverse_argv: Sequence[str],
        pre_existing: bool = False,
    ) -> None:
        argv_list = list(argv)
        inverse_list = list(inverse_argv)
        if not pre_existing:
            self._run(argv_list, check=True)
        self._recorder.record(
            Operation(
                kind="run_command",
                parameters={"argv": argv_list},
                inverse={"argv": inverse_list},
                pre_existing=pre_existing,
            )
        )


class AuthHandoffHelper:
    """``ctx.auth``: pause for an interactive auth step.

    Auth handoffs are not recorded operations. Pausing for the user is
    not a side effect Cerebro can roll back; the third-party state lives
    outside this process. The helper exists so plugins have one obvious
    place to call for a handoff.
    """

    def __init__(self, handoff: AuthHandoff) -> None:
        self._handoff = handoff

    def request(self, message: str) -> None:
        self._handoff.request(message=message)


__all__ = [
    "AuthHandoffHelper",
    "BlocksHelper",
    "CommandHelper",
    "FilesystemHelper",
    "OperationRecorder",
    "PackageManagerHelper",
    "ScheduledTaskHelper",
]
