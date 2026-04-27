"""Interactive ``cerebro init`` flow.

The only command in the surface that is genuinely conversational. The
flow is split out from the rest of the command file because it has
enough prompt logic that inlining it would crowd everything else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import click

from cerebro.models import CerebroState, PluginType
from cerebro.runtime.engine import install
from cerebro.runtime.platform import PlatformComponents
from cerebro.runtime.taps import discover_available
from cerebro.state import load_state, save_state, state_dir

_DEFAULT_VAULT = "~/ObsidianVault"
_TYPE_DISPLAY_ORDER: tuple[PluginType, ...] = ("ide", "agent", "behavior", "workflow")
_REQUIRED_TYPES: tuple[PluginType, ...] = ("ide", "agent")
_STATE_FILENAME = "state.yaml"


def run_init(
    *,
    home: Path | None,
    in_tree_root: Path | None,
    components: PlatformComponents | None,
    core_version: str,
    prompt_vault: bool = True,
    auto_select: dict[str, list[str]] | None = None,
) -> None:
    """Drive the interactive init.

    ``auto_select`` short-circuits the multi-select prompts; tests use it
    so they can exercise the full flow without simulating keystrokes.
    """
    base = home if home is not None else state_dir()
    state_path = base / _STATE_FILENAME

    if prompt_vault:
        vault_input = click.prompt(
            "Vault location",
            default=_DEFAULT_VAULT,
            show_default=True,
        )
    else:
        vault_input = _DEFAULT_VAULT
    vault_path = Path(vault_input).expanduser()

    state = _load_or_init_state(state_path, core_version=core_version, vault=vault_path)
    available = discover_available(state, home=base, in_tree_root=in_tree_root)

    grouped: dict[str, list[str]] = {kind: [] for kind in _TYPE_DISPLAY_ORDER}
    for name in sorted(available):
        manifest = available[name].manifest  # type: ignore[attr-defined]
        grouped.setdefault(manifest.type, []).append(name)

    selections: dict[str, list[str]] = {}
    for kind in _TYPE_DISPLAY_ORDER:
        names = grouped.get(kind, [])
        required = kind in _REQUIRED_TYPES
        if auto_select is not None:
            selections[kind] = list(auto_select.get(kind, []))
            if required and not selections[kind]:
                raise click.ClickException(
                    f"at least one {kind} plugin must be selected"
                )
            continue
        selections[kind] = _prompt_selection(kind, names, required=required)

    chosen = [name for kind in _TYPE_DISPLAY_ORDER for name in selections.get(kind, [])]
    if not chosen:
        click.echo("No plugins selected; nothing to install.", err=True)
        return

    for plugin_name in chosen:
        click.echo(f"Installing {plugin_name}...")
        install(
            plugin_name,
            home=base,
            in_tree_root=in_tree_root,
            components=components,
            now=datetime.now(tz=UTC),
        )
    click.echo("Done.")


def _load_or_init_state(
    state_path: Path,
    *,
    core_version: str,
    vault: Path,
) -> CerebroState:
    if state_path.exists():
        existing = load_state(state_path)
        if existing.vault_path != vault:
            updated = CerebroState(
                core_version=existing.core_version,
                vault_path=vault,
                installed_plugins=existing.installed_plugins,
            )
            save_state(updated, state_path)
            return updated
        return existing
    fresh = CerebroState(core_version=core_version, vault_path=vault, installed_plugins=[])
    save_state(fresh, state_path)
    return fresh


def _prompt_selection(kind: str, names: list[str], *, required: bool) -> list[str]:
    if not names:
        if required:
            raise click.ClickException(
                f"no {kind} plugins are available; cannot satisfy required selection"
            )
        click.echo(f"No {kind} plugins available. Skipping.")
        return []
    click.echo(f"\nAvailable {kind} plugins:")
    for index, name in enumerate(names, start=1):
        click.echo(f"  {index}) {name}")
    suffix = " (at least one required)" if required else " (optional, blank to skip)"
    while True:
        raw = click.prompt(
            f"Select {kind}s by number, comma-separated{suffix}",
            default="" if not required else None,
            show_default=False,
        )
        chosen = _parse_selection(raw, len(names))
        if chosen is None:
            click.echo("invalid selection; try again.", err=True)
            continue
        if required and not chosen:
            click.echo("at least one selection required.", err=True)
            continue
        return [names[i - 1] for i in chosen]


def _parse_selection(raw: str, count: int) -> list[int] | None:
    text = raw.strip()
    if not text:
        return []
    seen: set[int] = set()
    out: list[int] = []
    for part in text.split(","):
        token = part.strip()
        if not token:
            continue
        if not token.isdigit():
            return None
        index = int(token)
        if not 1 <= index <= count:
            return None
        if index in seen:
            continue
        seen.add(index)
        out.append(index)
    return out


__all__ = ["run_init"]
