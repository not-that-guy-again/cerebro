"""Claude Code token-optimization behavior plugin.

This is the first ``behavior`` plugin to target a specific named agent
(``claude-code``) rather than the whole ``agent`` type. The
``targets: [claude-code]`` field in ``plugin.yaml`` is what the resolver
keys on; ``configure`` only fires when claude-code itself is
re-installed or has its state perturbed.

Two side effects are produced on install:

1. A Cerebro-managed block in claude-code's rules file with the anti-
   platitude rule and the response-10 context-window warning. The block
   ID is ``claude-code-token-optimization`` so it coexists peacefully
   with the ``coding-standards`` block in the same ``CLAUDE.md``.
2. Installation of the third-party ``caveman`` Claude Code plugin via
   claude-code's plugin install mechanism (see CAVEMAN INSTALL MECHANISM
   below).

CAVEMAN INSTALL MECHANISM (volatile — do not document in spec/ADR)
==================================================================

Claude Code's plugin system has shipped under several different
shapes during 2025-2026, and the install path here is intentionally
narrow so that future drift is one constant change away. The current
assumption is that the ``claude`` CLI exposes a plugin subcommand:

    claude plugin install caveman      # install
    claude plugin uninstall caveman    # inverse, used on rollback / uninstall
    claude plugin list                 # presence check (verify, configure)

If/when this changes — for example to a marketplace-qualified name
(``caveman@<marketplace>``), to a JSON config file in
``~/.claude/plugins.json``, or to a git-clone-into-known-dir model —
update :data:`_CAVEMAN_INSTALL_ARGV`, :data:`_CAVEMAN_UNINSTALL_ARGV`,
and :func:`_is_caveman_installed`. The spec deliberately punts this
detail to the plugin source for that reason.

Reversibility model
===================

Both side effects are recorded through ``ctx``. The caveman install
goes through ``ctx.cmd.run(... pre_existing=already)`` with
``pre_existing=True`` when caveman was on the system before this plugin
ran, so a user-managed caveman install is never removed on uninstall.
The managed block goes through ``ctx.blocks.add``; the engine's inverse
pass strips just our block on uninstall, leaving sibling blocks in the
same file (e.g. ``claude-code``, ``coding-standards``) intact.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from cerebro.plugins.loader import discover_plugins, load_plugin_module
from cerebro.runtime.blocks import find_block, get_format

if TYPE_CHECKING:
    from cerebro.runtime.context import HookContext

_BLOCK_ID = "claude-code-token-optimization"
_BLOCK_FORMAT = "markdown"
_CLAUDE_AGENT_NAME = "claude-code"

# See module docstring "CAVEMAN INSTALL MECHANISM" for why these live here
# and what to change if the upstream plugin-install path moves.
_CAVEMAN_INSTALL_ARGV: list[str] = ["claude", "plugin", "install", "caveman"]
_CAVEMAN_UNINSTALL_ARGV: list[str] = ["claude", "plugin", "uninstall", "caveman"]
_CAVEMAN_LIST_ARGV: list[str] = ["claude", "plugin", "list"]
_CAVEMAN_PLUGIN_NAME = "caveman"

_BLOCK_CONTENT = (
    "## Token Optimization\n"
    "\n"
    "These rules apply to every conversation in this Claude Code session.\n"
    "\n"
    "### Avoid platitudes\n"
    "\n"
    "Do not use filler phrases like \"great question\", \"I'd be happy to\",\n"
    "\"feel free to ask\", \"let me know if you have any other questions\",\n"
    "or similar. Get to the point. Tokens spent on filler are tokens that\n"
    "cannot be spent on the work.\n"
    "\n"
    "### Long-conversation warning at response 10\n"
    "\n"
    "On the 10th response of any conversation, remind the user that long\n"
    "conversations bloat the context window and suggest starting a fresh\n"
    "session for a new topic. State this once at response 10; do not\n"
    "repeat the reminder on later responses in the same session."
)


def install(ctx: HookContext) -> None:
    """Install caveman into claude-code and add the managed block."""
    _install_caveman(ctx)
    _add_block(ctx)


def uninstall(ctx: HookContext) -> None:
    """No-op hook; the engine replays the recorded manifest.

    The recorded operations cover both side effects: the managed block
    in claude-code's rules file is removed by the engine's add_block
    inverse, and the caveman plugin uninstall command is replayed
    unless it was marked ``pre_existing`` at install time.
    """
    del ctx


def configure(ctx: HookContext) -> None:
    """Idempotently re-apply the managed block and caveman install.

    Called by the engine's reconfigure pass whenever claude-code itself
    is reinstalled (because this plugin sets ``targets: [claude-code]``).
    Both helpers below are idempotent: if caveman is already installed,
    the operation records ``pre_existing=True`` and does not re-shell;
    if the managed block is already present and unchanged, the blocks
    helper rewrites the same bytes.
    """
    _install_caveman(ctx)
    _add_block(ctx)


def verify(ctx: HookContext) -> None:
    """Confirm caveman is installed and the managed block is intact."""
    if not _is_caveman_installed():
        raise RuntimeError(
            "caveman plugin is not installed in Claude Code; run "
            "`cerebro doctor --action repair` to restore."
        )

    rules_path = _resolve_rules_path(ctx)
    if not rules_path.exists():
        raise RuntimeError(
            f"claude-code rules file is missing at {rules_path}"
        )
    text = rules_path.read_text(encoding="utf-8")
    block = find_block(text, _BLOCK_ID, get_format(_BLOCK_FORMAT))
    if block is None:
        raise RuntimeError(
            f"managed {_BLOCK_ID!r} block missing from {rules_path}"
        )
    if block.strip() != _BLOCK_CONTENT.strip():
        raise RuntimeError(
            f"managed {_BLOCK_ID!r} block in {rules_path} has been "
            "modified outside Cerebro; run `cerebro doctor --action "
            "repair` to restore."
        )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _install_caveman(ctx: HookContext) -> None:
    already = _is_caveman_installed()
    ctx.cmd.run(
        argv=_CAVEMAN_INSTALL_ARGV,
        inverse_argv=_CAVEMAN_UNINSTALL_ARGV,
        pre_existing=already,
    )
    ctx.log.info(
        "ensured caveman plugin in Claude Code (pre_existing=%s)", already
    )


def _add_block(ctx: HookContext) -> None:
    rules_path = _resolve_rules_path(ctx)
    ctx.blocks.add(
        rules_path,
        _BLOCK_ID,
        _BLOCK_CONTENT,
        format=_BLOCK_FORMAT,
    )
    ctx.log.info("wrote %s block to %s", _BLOCK_ID, rules_path)


def _is_caveman_installed() -> bool:
    """Check whether the caveman plugin is registered with Claude Code.

    Routed through ``subprocess.run`` so unit tests can monkeypatch it
    without requiring claude-code on the test machine. A non-zero exit
    from ``claude plugin list`` (claude not installed, plugin subsystem
    not present, etc.) is reported as "not installed" rather than
    raising — verify will surface the real failure with a better
    message.
    """
    try:
        result = subprocess.run(
            _CAVEMAN_LIST_ARGV,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    if result.returncode != 0:
        return False
    return _CAVEMAN_PLUGIN_NAME in (result.stdout or "")


def _resolve_rules_path(ctx: HookContext) -> Path:
    """Return claude-code's rules file path via its published surface."""
    module = _load_claude_module(ctx)
    fn = getattr(module, "rules_file_path", None)
    if fn is None or not callable(fn):
        raise RuntimeError(
            "claude-code plugin does not expose a rules_file_path() hook; "
            "cannot place token-optimization block."
        )
    result = fn(ctx)
    if not isinstance(result, Path):
        raise RuntimeError(
            "claude-code rules_file_path() returned "
            f"{type(result).__name__}, expected pathlib.Path"
        )
    return result


def _load_claude_module(ctx: HookContext) -> ModuleType:
    available = discover_plugins(ctx.state)
    discovered = available.get(_CLAUDE_AGENT_NAME)
    if discovered is None:
        raise RuntimeError(
            f"agent plugin {_CLAUDE_AGENT_NAME!r} is required but is not "
            "discoverable; install it before installing "
            "claude-code-token-optimization."
        )
    return load_plugin_module(discovered)


__all__ = [
    "configure",
    "install",
    "uninstall",
    "verify",
]
