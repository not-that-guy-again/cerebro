"""Claude Code agent plugin.

Installs the Claude Code CLI, wires it to the Cerebro Obsidian vault as
its memory, and installs Claude Code's IDE companion in every installed
``ide`` plugin.

Installer mechanism
===================

The Claude Code CLI is installed via npm:

    npm install -g @anthropic-ai/claude-code

This is the official documented installer (npm requires Node.js 18+).
Anthropic also publishes a curl-based installer; we deliberately chose
npm because it produces a stable ``claude`` binary at a predictable
location and gives a clean inverse (``npm uninstall -g …``) for the
operation recorder. The expected binary name on PATH is ``claude``.

Rules file path
===============

Per-user rules live at ``~/.claude/CLAUDE.md`` — the global Claude Code
memory file. The plugin contributes a Cerebro-managed block to that
file pointing at the vault. The path is published to behavior plugins
through :func:`rules_file_path` so callers do not have to hardcode it.

Reversibility model
===================

Every recorded side effect goes through ``ctx``: the npm install (with
``pre_existing=True`` when ``claude`` was already on PATH so user-owned
installs are not removed), the managed block in ``CLAUDE.md``, and the
``install_companion_extension`` calls into each IDE plugin (which
themselves record ``run_command`` operations with VSCode-specific
inverses). The Obsidian app and the vault itself are scaffolded by
:mod:`cerebro.runtime.obsidian`, which intentionally bypasses the
recorder because that data is the user's, not Claude Code's.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from cerebro.plugins.loader import discover_plugins, load_plugin_module
from cerebro.runtime import obsidian
from cerebro.runtime.blocks import find_block, get_format

if TYPE_CHECKING:
    from cerebro.runtime.context import HookContext

_CLI_BINARY = "claude"
_NPM_PACKAGE = "@anthropic-ai/claude-code"

# Per-IDE companion extension IDs. Add an entry here when a new IDE
# plugin lands; the IDE plugin's ``install_companion_extension`` helper
# is invoked with the value as its argument.
_COMPANION_EXTENSION_IDS: dict[str, str] = {
    "vscode": "anthropic.claude-code",
}

_AUTH_MESSAGE = (
    "Claude Code is installed but needs to be authenticated. In another "
    "terminal, run:\n\n    claude login\n\n"
    "Complete the browser flow, then return here and press Enter to "
    "continue Cerebro setup."
)

_BLOCK_FORMAT = "markdown"


def rules_file_path(ctx: HookContext) -> Path:
    """Path to the global Claude Code rules file.

    Published surface for behavior plugins. Today this is the
    ``~/.claude/CLAUDE.md`` path that Claude Code reads on startup;
    callers should not hardcode that path themselves so this plugin can
    update it later without breaking sibling plugins.
    """
    del ctx
    return Path("~/.claude/CLAUDE.md").expanduser()


def install(ctx: HookContext) -> None:
    """Install Claude Code and wire it up to every installed IDE."""
    ide_plugins = ctx.plugins_of_type("ide")
    if not ide_plugins:
        raise RuntimeError(
            "claude-code requires at least one IDE plugin to be installed first; "
            "install an IDE plugin (e.g. `cerebro install vscode`) and try again."
        )

    obsidian.ensure_obsidian_installed(ctx)
    obsidian.ensure_vault_scaffold(ctx, ctx.state.vault_path)

    _install_cli(ctx)

    ctx.auth.request(message=_AUTH_MESSAGE)

    ctx.blocks.add(
        rules_file_path(ctx),
        "claude-code",
        _block_content(ctx.state.vault_path),
        format=_BLOCK_FORMAT,
    )

    _install_companion_for_each_ide(ctx)


def uninstall(ctx: HookContext) -> None:
    """No-op hook; uninstall is driven by the engine replaying the manifest.

    The recorded operations cover everything the plugin contributed: the
    managed block in ``CLAUDE.md``, every companion extension installed
    in an IDE, and the npm install of the CLI (skipped on uninstall when
    it was pre-existing). Obsidian and the vault are not in the manifest
    and remain on disk by design.
    """
    del ctx


def configure(ctx: HookContext) -> None:
    """Re-wire companion extensions when a new IDE plugin is added.

    Engine reconfigure pass calls this after a new ``ide`` plugin is
    installed. ``install_companion_extension`` is idempotent (records
    ``pre_existing=True`` when the extension is already installed), so
    re-running across all IDEs is safe; only freshly added IDEs produce
    new operations on the manifest.
    """
    _install_companion_for_each_ide(ctx)


def verify(ctx: HookContext) -> None:
    """Confirm the CLI runs and the managed block is intact."""
    cli = shutil.which(_CLI_BINARY)
    if cli is None:
        raise RuntimeError(f"`{_CLI_BINARY}` CLI is not on PATH")
    result = subprocess.run(
        [cli, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`{_CLI_BINARY} --version` failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    rules = rules_file_path(ctx)
    if not rules.exists():
        raise RuntimeError(f"rules file is missing at {rules}")
    text = rules.read_text(encoding="utf-8")
    block = find_block(text, "claude-code", get_format(_BLOCK_FORMAT))
    if block is None:
        raise RuntimeError(f"managed claude-code block missing in {rules}")
    expected = _block_content(ctx.state.vault_path)
    if block.strip() != expected.strip():
        raise RuntimeError(
            f"managed claude-code block in {rules} has been modified "
            "outside Cerebro; run `cerebro doctor --action repair` to restore."
        )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _install_cli(ctx: HookContext) -> None:
    already_present = shutil.which(_CLI_BINARY) is not None
    ctx.cmd.run(
        argv=["npm", "install", "-g", _NPM_PACKAGE],
        inverse_argv=["npm", "uninstall", "-g", _NPM_PACKAGE],
        pre_existing=already_present,
    )


def _install_companion_for_each_ide(ctx: HookContext) -> None:
    ide_plugins = ctx.plugins_of_type("ide")
    if not ide_plugins:
        return
    available = discover_plugins(ctx.state)
    for record in ide_plugins:
        ext_id = _COMPANION_EXTENSION_IDS.get(record.name)
        if ext_id is None:
            ctx.log.warning(
                "no Claude Code companion extension known for IDE plugin %r; "
                "skipping (add an entry to _COMPANION_EXTENSION_IDS)",
                record.name,
            )
            continue
        discovered = available.get(record.name)
        if discovered is None:
            ctx.log.warning(
                "IDE plugin %r is installed but no longer discoverable; skipping",
                record.name,
            )
            continue
        module = load_plugin_module(discovered)
        installer = getattr(module, "install_companion_extension", None)
        if installer is None or not callable(installer):
            ctx.log.warning(
                "IDE plugin %r exposes no install_companion_extension hook; skipping",
                record.name,
            )
            continue
        installer(ctx, ext_id)


def _block_content(vault_path: Path) -> str:
    return (
        "Cerebro manages an Obsidian vault for cross-repo memory at:\n"
        "\n"
        f"    {vault_path}\n"
        "\n"
        "When you start work on a repo:\n"
        "\n"
        f"1. Read `{vault_path}/repos/<repo-name>.md` for accumulated context\n"
        "   on that repo (architecture, agreements, prior decisions).\n"
        "2. If that file does not exist yet, scaffold it before doing\n"
        "   substantive work and use it as your working memory across\n"
        "   sessions.\n"
        "\n"
        "Each repo's own `decisions/` directory is the source of truth\n"
        "for that repo's intentional choices; vault notes about a repo\n"
        "are derived context."
    )


__all__ = [
    "configure",
    "install",
    "rules_file_path",
    "uninstall",
    "verify",
]
