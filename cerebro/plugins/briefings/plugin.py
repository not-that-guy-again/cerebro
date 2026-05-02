"""Briefings workflow plugin.

Registers seven slash commands (``/daily-briefing``, ``/meeting-prep``,
``/daily-summary``, ``/weekly-summary``, ``/monthly-summary``,
``/quarterly-summary``, ``/annual-summary``) against every installed
``agent`` plugin, plus three OS-native scheduled tasks that materialise
stub briefing notes in the vault.

Cross-cutting model
===================

This is the first ``workflow`` plugin in the tree, and like the
coding-standards behavior plugin, it establishes the pattern future
workflow plugins will follow:

1. ``install`` discovers every installed ``agent`` plugin and calls each
   agent's published ``register_slash_command(ctx, name, script_path)``
   hook (introduced by SPEC-13). The slash command scripts are the
   markdown templates in this plugin's ``commands/`` directory; the
   agent plugin owns the convention for where those scripts ultimately
   live on disk.
2. ``install`` also registers three scheduled tasks via ``ctx.tasks``.
   Quarterly and annual summaries are user-invoked only and have no
   scheduled counterpart.
3. ``configure`` re-runs registration for newly-installed agents so an
   agent installed after Briefings still gets the full slash-command
   set.
4. ``uninstall`` is a no-op hook; the engine replays this plugin's
   recorded operations to remove every slash command file and every
   scheduled task.

Scheduled tasks invoke ``cerebro internal briefings-write <period>``,
which materialises a stub note in ``<vault>/briefings/<period>/<date>.md``
filled with collected git log data. Headless agent invocation is out of
scope for v1 (per the spec); the user finishes the stub interactively
from a slash command.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from cerebro.plugins.loader import discover_plugins, load_plugin_module

if TYPE_CHECKING:
    from cerebro.models import InstalledPlugin
    from cerebro.runtime.context import HookContext

_PLUGIN_DIR = Path(__file__).resolve().parent
_COMMANDS_DIR = _PLUGIN_DIR / "commands"

_SLASH_COMMANDS: tuple[str, ...] = (
    "daily-briefing",
    "meeting-prep",
    "daily-summary",
    "weekly-summary",
    "monthly-summary",
    "quarterly-summary",
    "annual-summary",
)

# (task_name, cadence, period). The task name is the global-unique
# identifier persisted in launchd / systemd; the period is the argument
# passed to ``cerebro internal briefings-write``.
_SCHEDULED_TASKS: tuple[tuple[str, str, str], ...] = (
    ("cerebro-briefings-daily-summary", "daily@18:00", "daily"),
    ("cerebro-briefings-weekly-summary", "weekly:fri@17:00", "weekly"),
    (
        "cerebro-briefings-monthly-summary",
        "monthly:last-business-day@17:00",
        "monthly",
    ),
)

_CEREBRO_BINARY = "cerebro"


def install(ctx: HookContext) -> None:
    """Register slash commands with every agent and schedule periodic tasks."""
    agents = _agent_modules(ctx)
    if not agents:
        raise RuntimeError(
            "briefings plugin requires at least one agent plugin to be "
            "installed first; install an agent plugin (e.g. "
            "`cerebro install claude-code`) and try again."
        )
    _register_for_agents(ctx, agents)
    _register_scheduled_tasks(ctx)


def uninstall(ctx: HookContext) -> None:
    """No-op hook; uninstall is driven by the engine replaying the manifest.

    Every slash command write_file and every register_task op recorded
    during install is reversed in turn — slash command files are
    removed, scheduled tasks are torn down, and the user is left with
    no traces of the briefings plugin.
    """
    del ctx


def configure(ctx: HookContext) -> None:
    """Register slash commands with newly-installed agents.

    The engine reconfigure pass calls this after a new ``agent`` plugin
    is installed. Re-running across all agents is intentionally
    idempotent: ``register_slash_command`` writes through ``ctx.fs``,
    which records the existing-content as the inverse, so already-up-to-
    date agents get a redundant rewrite of the same bytes and freshly
    added agents get a new slash command file.
    """
    agents = _agent_modules(ctx)
    _register_for_agents(ctx, agents)


def verify(ctx: HookContext) -> None:
    """Confirm slash commands are present per agent and tasks are registered."""
    agents = _agent_modules(ctx)
    for record, module in agents:
        path_fn = getattr(module, "slash_command_path", None)
        if not callable(path_fn):
            ctx.log.warning(
                "agent plugin %r exposes no slash_command_path() hook; "
                "skipping briefings file verification for it",
                record.name,
            )
            continue
        for name in _SLASH_COMMANDS:
            destination = path_fn(ctx, name)
            if not Path(destination).exists():
                raise RuntimeError(
                    f"slash command file missing for agent {record.name!r} "
                    f"at {destination}"
                )

    for task_name, _cadence, _period in _SCHEDULED_TASKS:
        if not ctx.tasks.is_registered(task_name):
            raise RuntimeError(
                f"scheduled task {task_name!r} is not registered; "
                "run `cerebro doctor --action repair` to restore."
            )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _agent_modules(
    ctx: HookContext,
) -> list[tuple[InstalledPlugin, ModuleType]]:
    out: list[tuple[InstalledPlugin, ModuleType]] = []
    agents = ctx.plugins_of_type("agent")
    if not agents:
        return out
    available = discover_plugins(ctx.state)
    for record in agents:
        discovered = available.get(record.name)
        if discovered is None:
            ctx.log.warning(
                "agent plugin %r is installed but no longer discoverable; "
                "skipping briefings registration.",
                record.name,
            )
            continue
        out.append((record, load_plugin_module(discovered)))
    return out


def _register_for_agents(
    ctx: HookContext,
    agents: list[tuple[InstalledPlugin, ModuleType]],
) -> None:
    for record, module in agents:
        register = getattr(module, "register_slash_command", None)
        if register is None or not callable(register):
            ctx.log.warning(
                "agent plugin %r exposes no register_slash_command() hook; "
                "skipping briefings registration for it",
                record.name,
            )
            continue
        for name in _SLASH_COMMANDS:
            script = _COMMANDS_DIR / f"{name}.md"
            register(ctx, name, script)
            ctx.log.info(
                "registered /%s for agent %r",
                name,
                record.name,
            )


def _register_scheduled_tasks(ctx: HookContext) -> None:
    binary = shutil.which(_CEREBRO_BINARY) or _CEREBRO_BINARY
    for task_name, cadence, period in _SCHEDULED_TASKS:
        command = f"{binary} internal briefings-write {period}"
        ctx.tasks.register(task_name, cadence, command)
        ctx.log.info(
            "registered scheduled task %s (%s)", task_name, cadence
        )


__all__ = [
    "configure",
    "install",
    "uninstall",
    "verify",
]
