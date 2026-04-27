"""VSCode IDE plugin.

Installs Visual Studio Code through the platform package manager
(``brew --cask`` on macOS, ``code`` on apt and pacman) and verifies
that the ``code`` CLI is on PATH. Publishes
``install_companion_extension`` and ``uninstall_companion_extension``
helpers that agent plugins call to bind themselves to VSCode.

Reversibility model
===================

Every side effect goes through ``ctx`` so the operation recorder can
roll it back. ``ctx.pkg.install_cask`` records the install with
``pre_existing=True`` when VSCode was already on the system, so the
engine's uninstall replay skips the cask removal — Cerebro does not
remove software the user had before Cerebro touched the system. The
same pre-existing handling applies to ``install_companion_extension``:
extensions that are already present at call time are recorded as
pre-existing and left in place when the calling plugin is uninstalled.

VSCode user ``settings.json`` is intentionally untouched (ADR-0014).
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from typing import TYPE_CHECKING

from cerebro.runtime import platform as _platform
from cerebro.runtime.platform import Platform

if TYPE_CHECKING:
    from cerebro.runtime.context import HookContext

_CASK_NAME = "visual-studio-code"
_LINUX_PACKAGE = "code"
_CODE_CLI = "code"
_PATH_HELP_MACOS = (
    "VSCode is installed but the `code` CLI is not on PATH. Open VSCode and "
    "run \"Shell Command: Install 'code' command in PATH\" from the Command "
    "Palette (Cmd+Shift+P)."
)
_PATH_HELP_LINUX = (
    "VSCode is installed but the `code` CLI is not on PATH. Add the VSCode "
    "binary directory to your shell PATH (e.g. /usr/share/code/bin)."
)


def install(ctx: HookContext) -> None:
    """Install VSCode via the platform package manager and verify the CLI."""
    plat = _platform.current_platform()
    if plat is Platform.MACOS:
        ctx.pkg.install_cask(_CASK_NAME)
    elif plat in (Platform.LINUX_APT, Platform.LINUX_PACMAN):
        ctx.pkg.install(_LINUX_PACKAGE)
    else:
        raise RuntimeError(
            f"vscode plugin does not support platform {plat.value!r}"
        )

    if shutil.which(_CODE_CLI) is None:
        raise RuntimeError(
            _PATH_HELP_MACOS if plat is Platform.MACOS else _PATH_HELP_LINUX
        )


def uninstall(ctx: HookContext) -> None:
    """No-op hook.

    Uninstall is driven by the engine replaying this plugin's recorded
    install manifest in reverse. Operations marked ``pre_existing=True``
    (notably the brew cask install, when VSCode was already present) are
    skipped, so user-installed VSCode is left in place.
    """
    del ctx


def verify(ctx: HookContext) -> None:
    """Confirm that ``code --version`` runs successfully."""
    del ctx
    code_bin = shutil.which(_CODE_CLI)
    if code_bin is None:
        raise RuntimeError("`code` CLI is not on PATH")
    result = subprocess.run(
        [code_bin, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`code --version` failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def install_companion_extension(ctx: HookContext, extension_id: str) -> bool:
    """Install a VSCode extension on behalf of a sibling plugin.

    Recorded through ``ctx`` so it is reversed when the calling plugin
    is uninstalled. Extensions that are already present at call time
    are recorded as pre-existing and left in place during uninstall.
    Returns ``True`` if the call was a no-op (extension was already
    installed), ``False`` if the extension was newly installed.
    """
    code_bin = _require_code_cli()
    already = _extension_installed(code_bin, extension_id)
    ctx.cmd.run(
        argv=[code_bin, "--install-extension", extension_id],
        inverse_argv=[code_bin, "--uninstall-extension", extension_id],
        pre_existing=already,
    )
    return already


def uninstall_companion_extension(ctx: HookContext, extension_id: str) -> bool:
    """Remove a VSCode extension on behalf of a sibling plugin.

    The inverse of :func:`install_companion_extension`. The reverse-on-
    uninstall behavior is symmetric: if a plugin reverses its own
    earlier ``install_companion_extension`` call by invoking this
    helper, replaying that call on uninstall will reinstall the
    extension. Returns ``True`` if the extension was present and was
    removed, ``False`` if it was not installed (no-op).
    """
    code_bin = _require_code_cli()
    present = _extension_installed(code_bin, extension_id)
    if not present:
        return False
    ctx.cmd.run(
        argv=[code_bin, "--uninstall-extension", extension_id],
        inverse_argv=[code_bin, "--install-extension", extension_id],
        pre_existing=False,
    )
    return True


def _require_code_cli() -> str:
    code_bin = shutil.which(_CODE_CLI)
    if code_bin is None:
        raise RuntimeError(
            "`code` CLI is not on PATH; install or repair the vscode plugin first"
        )
    return code_bin


def _extension_installed(code_bin: str, extension_id: str) -> bool:
    listed = _list_extensions(code_bin)
    needle = extension_id.casefold()
    return any(line.casefold() == needle for line in listed)


def _list_extensions(code_bin: str) -> Sequence[str]:
    result = subprocess.run(
        [code_bin, "--list-extensions"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
