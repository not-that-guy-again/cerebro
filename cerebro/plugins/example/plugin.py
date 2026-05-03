"""Example plugin used as the worked example in docs/authoring-plugins.md.

Intentionally minimal. Demonstrates the v1 plugin contract end-to-end:

* a ``behavior`` plugin that targets every installed ``agent`` plugin
* an ``install`` hook that writes a per-plugin state file via ``ctx.fs``
  and a managed comment block via ``ctx.blocks`` into every agent's
  rules file
* a ``configure`` hook that re-runs the block injection when a new agent
  plugin is installed
* a ``verify`` hook used by ``cerebro doctor`` to detect drift
* an ``uninstall`` hook that is a no-op because the engine replays the
  recorded operations in reverse

CI runs an end-to-end install + uninstall against this plugin so the
authoring guide does not drift from the code: if this plugin breaks,
the test fails and the docs are fixed alongside the implementation.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from cerebro.plugins.loader import discover_plugins, load_plugin_module
from cerebro.runtime.blocks import find_block, get_format
from cerebro.state import state_dir

if TYPE_CHECKING:
    from cerebro.models import InstalledPlugin
    from cerebro.runtime.context import HookContext

_PLUGIN_NAME = "example"
_BLOCK_FORMAT = "markdown"
_BLOCK_CONTENT = (
    "Hello from the example Cerebro plugin.\n"
    "\n"
    "This block is managed by the `example` plugin and is the worked\n"
    "example referenced in `docs/authoring-plugins.md`."
)
_GREETING_FILENAME = "greeting.txt"
_GREETING_BODY = (
    "This file was written by the example plugin.\n"
    "Removing the plugin removes this file.\n"
)


def install(ctx: HookContext) -> None:
    """Write the per-plugin greeting file and inject the managed block."""
    ctx.fs.write_file(_greeting_path(), _GREETING_BODY)
    _add_block_to_all_agents(ctx)


def uninstall(ctx: HookContext) -> None:
    """No-op hook; the engine replays the recorded manifest in reverse."""
    del ctx


def configure(ctx: HookContext) -> None:
    """Re-add the managed block when a new agent plugin is installed."""
    _add_block_to_all_agents(ctx)


def verify(ctx: HookContext) -> None:
    """Confirm the greeting file and managed block are present and unmodified."""
    greeting = _greeting_path()
    if not greeting.exists():
        raise RuntimeError(f"greeting file is missing at {greeting}")
    if greeting.read_text(encoding="utf-8") != _GREETING_BODY:
        raise RuntimeError(
            f"greeting file at {greeting} has been modified outside Cerebro; "
            "run `cerebro doctor --action repair` to restore."
        )

    syntax = get_format(_BLOCK_FORMAT)
    for record, rules_path in _agent_rules_files(ctx):
        if not rules_path.exists():
            raise RuntimeError(
                f"agent plugin {record.name!r} is installed but its rules "
                f"file is missing at {rules_path}"
            )
        text = rules_path.read_text(encoding="utf-8")
        block = find_block(text, _PLUGIN_NAME, syntax)
        if block is None:
            raise RuntimeError(
                f"managed {_PLUGIN_NAME!r} block missing from {rules_path} "
                f"(agent plugin {record.name!r})"
            )
        if block.strip() != _BLOCK_CONTENT.strip():
            raise RuntimeError(
                f"managed {_PLUGIN_NAME!r} block in {rules_path} has been "
                "modified outside Cerebro; run `cerebro doctor --action "
                "repair` to restore."
            )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _greeting_path() -> Path:
    return state_dir() / "plugins" / _PLUGIN_NAME / _GREETING_FILENAME


def _add_block_to_all_agents(ctx: HookContext) -> None:
    for record, rules_path in _agent_rules_files(ctx):
        ctx.blocks.add(
            rules_path,
            _PLUGIN_NAME,
            _BLOCK_CONTENT,
            format=_BLOCK_FORMAT,
        )
        ctx.log.info(
            "wrote example block to %s (agent plugin %r)",
            rules_path,
            record.name,
        )


def _agent_rules_files(
    ctx: HookContext,
) -> list[tuple[InstalledPlugin, Path]]:
    out: list[tuple[InstalledPlugin, Path]] = []
    agents = ctx.plugins_of_type("agent")
    if not agents:
        return out
    available = discover_plugins(ctx.state)
    for record in agents:
        discovered = available.get(record.name)
        if discovered is None:
            ctx.log.warning(
                "agent plugin %r is installed but no longer discoverable; "
                "skipping example injection.",
                record.name,
            )
            continue
        module = load_plugin_module(discovered)
        path = _resolve_rules_file_path(ctx, record.name, module)
        if path is None:
            continue
        out.append((record, path))
    return out


def _resolve_rules_file_path(
    ctx: HookContext, agent_name: str, module: ModuleType
) -> Path | None:
    fn = getattr(module, "rules_file_path", None)
    if fn is None or not callable(fn):
        ctx.log.warning(
            "agent plugin %r exposes no rules_file_path() hook; "
            "skipping example injection.",
            agent_name,
        )
        return None
    result = fn(ctx)
    if not isinstance(result, Path):
        raise RuntimeError(
            f"agent plugin {agent_name!r}.rules_file_path() returned "
            f"{type(result).__name__}, expected pathlib.Path"
        )
    return result


__all__ = [
    "configure",
    "install",
    "uninstall",
    "verify",
]
