"""Brew-style tap management (ADR-0009).

A tap is a git repository with a ``plugins/`` directory at its root.
``cerebro tap add`` clones it into ``<state_dir>/taps/<name>``, and the
plugin loader picks it up automatically on the next discovery pass.
The ``git`` binary is required at runtime; we shell out rather than
pulling in a Python git library to keep the dependency surface small.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cerebro.models import CerebroState
from cerebro.plugins.loader import (
    PluginConflictError,
    discover_plugins,
)
from cerebro.state import load_state, state_dir

_TAPS_DIRNAME = "taps"
_STATE_FILENAME = "state.yaml"


class TapError(RuntimeError):
    """Raised for tap-management preconditions that fail."""


@dataclass(frozen=True)
class TapInfo:
    name: str
    path: Path
    url: str | None
    plugin_count: int


def taps_root(home: Path | None = None) -> Path:
    base = home if home is not None else state_dir()
    return base / _TAPS_DIRNAME


def derive_tap_name(url: str) -> str:
    """Derive the default tap directory name from a git URL.

    Strips a trailing ``.git`` and uses the last path segment, mirroring
    what ``git clone`` would do without an explicit target directory.
    """
    last = url.rstrip("/").rsplit("/", 1)[-1]
    last = last.rsplit(":", 1)[-1]  # handle scp-style git@host:owner/repo
    if last.endswith(".git"):
        last = last[: -len(".git")]
    if not last:
        raise TapError(f"could not derive tap name from URL {url!r}")
    return last


def add_tap(
    url: str,
    *,
    name: str | None = None,
    home: Path | None = None,
    git: str = "git",
) -> TapInfo:
    """Clone ``url`` into the taps directory. Returns the resulting tap."""
    root = taps_root(home)
    resolved_name = name if name else derive_tap_name(url)
    target = root / resolved_name
    if target.exists():
        raise TapError(f"tap {resolved_name!r} already exists at {target}")
    root.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [git, "clone", url, str(target)],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise TapError(
            "git was not found on PATH; install git to manage taps"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise TapError(
            f"git clone failed for {url!r}: {exc.stderr.strip() or exc.stdout.strip() or exc}"
        ) from exc
    return _info_for(target, name=resolved_name, git=git)


def remove_tap(
    name: str,
    *,
    home: Path | None = None,
    git: str = "git",
) -> None:
    """Remove a tap.

    Refuses if any installed plugin's ``source`` is this tap. We do not
    consult the tap's ``plugins/`` directory directly because the user's
    state is the authoritative record of what is installed; removing the
    tap while one of its plugins is installed would orphan the install.
    """
    base = home if home is not None else state_dir()
    target = taps_root(base) / name
    if not target.is_dir():
        raise TapError(f"tap {name!r} is not registered")

    state_path = base / _STATE_FILENAME
    if state_path.exists():
        state = load_state(state_path)
        installed_from_tap = [
            p.name for p in state.installed_plugins if p.source == name
        ]
        if installed_from_tap:
            listed = ", ".join(sorted(installed_from_tap))
            raise TapError(
                f"cannot remove tap {name!r}: plugins from this tap are still installed: {listed}"
            )

    shutil.rmtree(target)
    del git  # currently unused; reserved for a future "git for sanity check" hook


def list_taps(*, home: Path | None = None, git: str = "git") -> list[TapInfo]:
    """Return registered taps sorted by name."""
    root = taps_root(home)
    if not root.is_dir():
        return []
    out: list[TapInfo] = []
    for entry in sorted(p for p in root.iterdir() if p.is_dir()):
        out.append(_info_for(entry, name=entry.name, git=git))
    return out


def update_tap(
    name: str | None = None,
    *,
    home: Path | None = None,
    git: str = "git",
) -> list[str]:
    """Run ``git pull`` on one tap or all taps. Returns the names updated."""
    root = taps_root(home)
    if name is not None:
        target = root / name
        if not target.is_dir():
            raise TapError(f"tap {name!r} is not registered")
        _git_pull(target, git=git, tap_name=name)
        return [name]

    if not root.is_dir():
        return []
    updated: list[str] = []
    for entry in sorted(p for p in root.iterdir() if p.is_dir()):
        _git_pull(entry, git=git, tap_name=entry.name)
        updated.append(entry.name)
    return updated


def discover_available(
    state: CerebroState,
    *,
    home: Path | None = None,
    in_tree_root: Path | None = None,
) -> dict[str, object]:
    """Discover all available plugins (in-tree + every tap), keyed by name.

    Surfaces a friendlier ``TapError`` message when two sources collide
    so the CLI does not have to reason about the loader's internals.
    """
    try:
        return discover_plugins(  # type: ignore[return-value]
            state,
            in_tree_root=in_tree_root,
            taps_root=taps_root(home),
        )
    except PluginConflictError as exc:
        raise TapError(str(exc)) from exc


def _info_for(path: Path, *, name: str, git: str) -> TapInfo:
    url = _read_origin_url(path, git=git)
    plugin_count = _count_plugins(path)
    return TapInfo(name=name, path=path, url=url, plugin_count=plugin_count)


def _read_origin_url(path: Path, *, git: str) -> str | None:
    try:
        result = subprocess.run(
            [git, "-C", str(path), "remote", "get-url", "origin"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    url = result.stdout.strip()
    return url or None


def _count_plugins(path: Path) -> int:
    plugins_dir = path / "plugins"
    if not plugins_dir.is_dir():
        return 0
    return sum(
        1
        for entry in plugins_dir.iterdir()
        if entry.is_dir() and (entry / "plugin.yaml").is_file()
    )


def _git_pull(path: Path, *, git: str, tap_name: str) -> None:
    try:
        subprocess.run(
            [git, "-C", str(path), "pull", "--ff-only"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise TapError(
            "git was not found on PATH; install git to manage taps"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise TapError(
            f"git pull failed for tap {tap_name!r}: "
            f"{exc.stderr.strip() or exc.stdout.strip() or exc}"
        ) from exc


__all__ = [
    "TapError",
    "TapInfo",
    "add_tap",
    "derive_tap_name",
    "discover_available",
    "list_taps",
    "remove_tap",
    "taps_root",
    "update_tap",
]
