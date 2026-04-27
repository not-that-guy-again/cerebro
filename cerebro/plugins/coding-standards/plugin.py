"""Coding-standards behavior plugin.

This is the first ``behavior`` plugin in the tree. It establishes the
pattern every future behavior plugin will follow:

1. ``install`` runs an interactive picker through ``ctx.prompt`` to
   collect the user's preferences.
2. The selections are persisted to a plugin-owned state file under
   ``$CEREBRO_HOME/plugins/<plugin-name>/`` — separate from Cerebro's
   global ``state.yaml`` because the schema is plugin-specific and the
   engine has no business reading it.
3. The selections are rendered through a Jinja template that lives next
   to ``plugin.py``. Keeping the template separate keeps the prose
   readable; the rendered output is what an LLM ends up reading, so
   clarity beats cleverness.
4. The rendered block is dropped into every installed ``agent``
   plugin's global rules file via ``ctx.blocks.add(...)``. The block ID
   (``coding-standards``) is unique to this plugin so other behavior
   plugins (e.g. token-optimization) can coexist in the same file.

Re-running ``cerebro install coding-standards`` after the plugin is
installed is rejected by the engine. Updating selections currently
requires ``cerebro uninstall`` followed by ``cerebro install``; a
``cerebro reconfigure`` command is a planned future improvement that
will replace the uninstall+reinstall cycle without losing the recorded
manifest.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from cerebro.plugins.loader import discover_plugins, load_plugin_module
from cerebro.runtime.blocks import find_block, get_format
from cerebro.state import state_dir

if TYPE_CHECKING:
    from cerebro.models import InstalledPlugin
    from cerebro.runtime.context import HookContext

_PLUGIN_NAME = "coding-standards"
_BLOCK_ID = "coding-standards"
_BLOCK_FORMAT = "markdown"
_TEMPLATE_DIRNAME = "templates"
_TEMPLATE_NAME = "standards.md.j2"
_SELECTIONS_FILENAME = "selections.yaml"
_PLUGIN_DIR = Path(__file__).resolve().parent

_BRANCH_NAMING_SCHEMES = (
    "<initials>-<short-description>",
    "<type>/<short-description>",
    "<ticket>-<short-description>",
    "freeform",
)
_COVERAGE_TARGETS = ("none", "60%", "80%", "90%")


def install(ctx: HookContext) -> None:
    """Collect selections, persist them, and inject the block everywhere."""
    selections = _prompt_for_selections(ctx)
    _save_selections(ctx, selections)
    rendered = _render(selections)
    _add_block_to_all_agents(ctx, rendered)


def uninstall(ctx: HookContext) -> None:
    """No-op hook; the engine replays the manifest.

    Both side effects of install — the per-agent managed blocks and the
    selections file under ``$CEREBRO_HOME/plugins/coding-standards/`` —
    were recorded through ``ctx.blocks`` and ``ctx.fs``, so the engine's
    inverse pass cleans them up.
    """
    del ctx


def configure(ctx: HookContext) -> None:
    """Re-add the managed block when a new agent plugin is installed.

    The engine triggers configure on every targeting plugin (this one
    targets every agent) after a new agent is installed. Re-running over
    every installed agent is intentionally idempotent: ``ctx.blocks.add``
    upserts, so existing blocks are rewritten with the (unchanged)
    rendered content and new agents pick up a fresh block.
    """
    selections = _load_selections(ctx)
    rendered = _render(selections)
    _add_block_to_all_agents(ctx, rendered)


def verify(ctx: HookContext) -> None:
    """Confirm every installed agent has the block and matches our render."""
    selections = _load_selections(ctx)
    expected = _render(selections).strip()
    syntax = get_format(_BLOCK_FORMAT)

    for record, rules_path in _agent_rules_files(ctx):
        if not rules_path.exists():
            raise RuntimeError(
                f"agent plugin {record.name!r} is installed but its rules "
                f"file is missing at {rules_path}"
            )
        text = rules_path.read_text(encoding="utf-8")
        block = find_block(text, _BLOCK_ID, syntax)
        if block is None:
            raise RuntimeError(
                f"managed {_BLOCK_ID!r} block missing from {rules_path} "
                f"(agent plugin {record.name!r})"
            )
        if block.strip() != expected:
            raise RuntimeError(
                f"managed {_BLOCK_ID!r} block in {rules_path} (agent "
                f"plugin {record.name!r}) has been modified outside "
                "Cerebro; run `cerebro doctor --action repair` to restore."
            )


# ---------------------------------------------------------------------------
# selections
# ---------------------------------------------------------------------------


def _prompt_for_selections(ctx: HookContext) -> dict[str, dict[str, Any]]:
    """Walk the user through every category and return a selections dict.

    Each category is a flat ``dict[str, bool | str]``; the rendered
    template only ever indexes by category name + question key, so this
    schema is the contract between the picker and the template.
    """
    p = ctx.prompt
    return {
        "branching": {
            "work_on_main_allowed": p.yes_no(
                question="Allow committing directly to the main branch for trivial changes?",
                default=False,
            ),
            "ask_before_creating": p.yes_no(
                question="Should the agent ask before creating a new branch?",
                default=True,
            ),
            "naming_scheme": p.choice(
                question="Branch naming scheme",
                choices=_BRANCH_NAMING_SCHEMES,
                default=_BRANCH_NAMING_SCHEMES[0],
            ),
        },
        "commits": {
            "descriptive_messages": p.yes_no(
                question="Require descriptive commit messages (why, not just what)?",
                default=True,
            ),
            "conventional": p.yes_no(
                question="Use conventional commit prefixes (feat:, fix:, chore:)?",
                default=False,
            ),
            "signed": p.yes_no(
                question="Require GPG-signed commits?",
                default=False,
            ),
        },
        "prs": {
            "tests_must_pass": p.yes_no(
                question="Require all tests to pass locally before opening a PR?",
                default=True,
            ),
            "description_template": p.yes_no(
                question="Use a structured PR description template (Summary + Test plan)?",
                default=True,
            ),
        },
        "testing": {
            "tdd": p.yes_no(
                question="Practice test-driven development (write the test first)?",
                default=False,
            ),
            "min_coverage": p.choice(
                question="Minimum coverage target for new code",
                choices=_COVERAGE_TARGETS,
                default="80%",
            ),
        },
        "style": {},
    }


def _selections_path(ctx: HookContext) -> Path:
    del ctx
    return state_dir() / "plugins" / _PLUGIN_NAME / _SELECTIONS_FILENAME


def _save_selections(
    ctx: HookContext, selections: dict[str, dict[str, Any]]
) -> None:
    target = _selections_path(ctx)
    body = yaml.safe_dump(selections, sort_keys=False, default_flow_style=False)
    ctx.fs.write_file(target, body)


def _load_selections(ctx: HookContext) -> dict[str, dict[str, Any]]:
    path = _selections_path(ctx)
    if not path.exists():
        raise RuntimeError(
            f"coding-standards selections file missing at {path}; "
            "uninstall and reinstall the plugin to regenerate it."
        )
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise RuntimeError(
            f"coding-standards selections at {path} are not a mapping; "
            "uninstall and reinstall the plugin to regenerate it."
        )
    return data


# ---------------------------------------------------------------------------
# rendering and block placement
# ---------------------------------------------------------------------------


def _render(selections: dict[str, dict[str, Any]]) -> str:
    env = Environment(
        loader=FileSystemLoader(str(_PLUGIN_DIR / _TEMPLATE_DIRNAME)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
        autoescape=False,
    )
    template = env.get_template(_TEMPLATE_NAME)
    return template.render(**selections)


def _add_block_to_all_agents(ctx: HookContext, rendered: str) -> None:
    for record, rules_path in _agent_rules_files(ctx):
        ctx.blocks.add(
            rules_path,
            _BLOCK_ID,
            rendered,
            format=_BLOCK_FORMAT,
        )
        ctx.log.info(
            "wrote coding-standards block to %s (agent plugin %r)",
            rules_path,
            record.name,
        )


def _agent_rules_files(
    ctx: HookContext,
) -> list[tuple[InstalledPlugin, Path]]:
    """Return ``(record, rules_path)`` for every installed agent plugin.

    Agent plugins that do not (yet) publish a ``rules_file_path`` hook
    are logged-and-skipped rather than raising — the same forgiving
    posture the Claude Code plugin takes when an IDE has no companion
    extension entry. SPEC-15 makes the hook mandatory, so missing it is
    a plugin-author bug we surface at warning level.
    """
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
                "skipping coding-standards injection.",
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
            "skipping coding-standards injection.",
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
