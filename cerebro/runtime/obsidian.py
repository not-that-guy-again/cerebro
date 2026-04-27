"""Obsidian app installer and global vault scaffolder.

Not a plugin in v1: invoked from the claude-code agent plugin's install
hook (and reusable by any future agent plugin that wants the same
setup). Both helpers are idempotent so re-running them after a partial
failure or after adding a second agent plugin is a safe no-op.

Why these helpers bypass the operation recorder
===============================================

Per ADR-0013 and SPEC-11, Obsidian and the global vault are user data:
they survive uninstall of any plugin that triggered their creation. The
recorder's contract is "if you went through ``ctx``, the core will undo
it on rollback", so to keep these effects out of every plugin's install
manifest the helpers perform their writes directly (``Path.mkdir`` and
``subprocess.run``) and use only the read-side of ``ctx`` (the logger
and the package manager's ``is_*`` checks). Idempotency is preserved by
checking before each write.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from cerebro.runtime import platform as _platform
from cerebro.runtime.platform import Platform

if TYPE_CHECKING:
    from cerebro.runtime.context import HookContext

Runner = Callable[[Sequence[str]], None]

_OBSIDIAN_CASK = "obsidian"
# On Linux the package name "obsidian" is what's used by Arch's official
# repos. Debian/Ubuntu does not ship Obsidian in apt; the install will
# fail loudly there and the user installs it themselves. We do not try
# to wrap snap/flatpak/AppImage from inside the helper.
_OBSIDIAN_LINUX_PACKAGE = "obsidian"

_VAULT_SUBDIRS: tuple[str, ...] = (
    "repos",
    "briefings",
    "decisions-mirror",
    "templates",
)

_VAULT_README = """\
# Cerebro Vault

This is your global Obsidian vault, scaffolded by Cerebro. It is the
single home for cross-repo memory: agent context, briefings, and ADR
mirrors.

## Layout

- `repos/` — one note per code repo you work in. Cerebro's agent plugins
  scaffold a stub here (`repos/<repo-name>.md`) the first time they see
  a repo and accumulate context there as they work.
- `briefings/` — generated daily/weekly briefings.
- `decisions-mirror/` — read-only mirror of `decisions/` directories
  from your code repos.
- `templates/` — Obsidian templates used by agent and workflow plugins.

The vault is intentionally outside any tracked code repo (ADR-0013).
Cerebro never deletes the vault or its contents, even when uninstalling
the plugin that created them.
"""


def ensure_obsidian_installed(
    ctx: HookContext,
    *,
    runner: Runner | None = None,
) -> None:
    """Install Obsidian via the platform package manager. Idempotent.

    No-op if Obsidian is already present. Bypasses the operation
    recorder: per ADR-0013/SPEC-11, Obsidian is user data and is not
    removed by Cerebro on uninstall, even if Cerebro installed it. The
    optional ``runner`` is for tests; production calls go through
    :func:`subprocess.run`.
    """
    plat = _platform.current_platform()
    run = runner if runner is not None else _default_runner

    if plat is Platform.MACOS:
        if ctx.pkg.is_cask_installed(_OBSIDIAN_CASK):
            ctx.log.info("Obsidian already installed; skipping cask install.")
            return
        ctx.log.info("Installing Obsidian via brew --cask…")
        run(["brew", "install", "--cask", _OBSIDIAN_CASK])
        return

    if plat is Platform.LINUX_APT:
        if ctx.pkg.is_installed(_OBSIDIAN_LINUX_PACKAGE):
            ctx.log.info("Obsidian already installed; skipping apt install.")
            return
        ctx.log.info("Installing Obsidian via apt-get…")
        run(["sudo", "apt-get", "install", "-y", _OBSIDIAN_LINUX_PACKAGE])
        return

    if plat is Platform.LINUX_PACMAN:
        if ctx.pkg.is_installed(_OBSIDIAN_LINUX_PACKAGE):
            ctx.log.info("Obsidian already installed; skipping pacman install.")
            return
        ctx.log.info("Installing Obsidian via pacman…")
        run(["sudo", "pacman", "-S", "--noconfirm", _OBSIDIAN_LINUX_PACKAGE])
        return

    raise RuntimeError(
        f"obsidian helper does not support platform {plat.value!r}"
    )


def ensure_vault_scaffold(ctx: HookContext, vault_path: Path | str) -> None:
    """Create the global vault layout under ``vault_path``. Idempotent.

    Creates ``repos/``, ``briefings/``, ``decisions-mirror/``, and
    ``templates/`` plus a top-level ``README.md`` describing the layout.
    Existing directories or an existing README are left alone, so
    re-running after install (or from a second agent plugin) is a no-op.
    """
    target = Path(vault_path).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    for sub in _VAULT_SUBDIRS:
        (target / sub).mkdir(parents=True, exist_ok=True)
    readme = target / "README.md"
    if not readme.exists():
        readme.write_text(_VAULT_README, encoding="utf-8")
    ctx.log.info("Vault scaffold ensured at %s", target)


def _default_runner(argv: Sequence[str]) -> None:
    subprocess.run(list(argv), check=True)


__all__ = [
    "Runner",
    "ensure_obsidian_installed",
    "ensure_vault_scaffold",
]
