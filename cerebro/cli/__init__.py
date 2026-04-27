"""Click-based CLI for Cerebro.

We picked Click over Typer. Typer is Click underneath, and the CLI's
arguments are mostly free-form strings (plugin names, git URLs) for
which Typer's type-hint inference adds no leverage. The init command's
multi-select prompts are bespoke either way, and we use Click's
``ClickException`` machinery directly for our exit-code policy. One
fewer indirection, one fewer dependency.

Help text wording is part of the public surface and is locked down by
snapshot tests in ``tests/test_cli.py``; intentional UX changes update
the snapshot in the same commit.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import click

from cerebro import __version__
from cerebro.cli._errors import EXIT_OK, handle_errors
from cerebro.cli._init import run_init
from cerebro.runtime.doctor import (
    DriftReport,
    OperationStatus,
    accept_plugin,
    repair_plugin,
    report_to_dict,
    run_doctor,
)
from cerebro.runtime.engine import install as engine_install
from cerebro.runtime.engine import uninstall as engine_uninstall
from cerebro.runtime.lifecycle import disable_plugin, enable_plugin
from cerebro.runtime.taps import (
    add_tap,
    discover_available,
    list_taps,
    remove_tap,
    update_tap,
)
from cerebro.state import load_state, state_dir

_VERBOSE_LOGGER_NAMES = ("cerebro",)


def _configure_verbose_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s: %(message)s"))
    handler.setLevel(logging.DEBUG)
    for name in _VERBOSE_LOGGER_NAMES:
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)


def _state_path(home: Path | None = None) -> Path:
    base = home if home is not None else state_dir()
    return base / "state.yaml"


def _load_state_or_empty(home: Path | None = None):
    path = _state_path(home)
    if not path.exists():
        return None
    return load_state(path)


@click.group(
    name="cerebro",
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Bootstrap and maintain an agentic development environment.",
)
@click.version_option(__version__, prog_name="cerebro")
@click.option(
    "--verbose",
    is_flag=True,
    help="Stream the engine's debug log to stderr in addition to the log file.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit machine-readable JSON for commands that support it.",
)
@click.pass_context
def cli(ctx: click.Context, verbose: bool, json_output: bool) -> None:
    if verbose:
        _configure_verbose_logging()
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["json"] = json_output


@cli.command("init", help="Interactively configure the vault and install starter plugins.")
@click.pass_context
@handle_errors
def init_command(ctx: click.Context) -> None:
    run_init(
        home=None,
        in_tree_root=None,
        components=None,
        core_version=__version__,
    )
    del ctx


@cli.command("install", help="Install a plugin and its dependencies.")
@click.argument("plugin")
@click.pass_context
@handle_errors
def install_command(ctx: click.Context, plugin: str) -> None:
    engine_install(plugin, now=datetime.now(tz=UTC))
    if ctx.obj.get("json"):
        click.echo(json.dumps({"installed": plugin}))
    else:
        click.echo(f"installed {plugin}")


@cli.command("uninstall", help="Uninstall a plugin (refuses if other plugins depend on it).")
@click.argument("plugin")
@click.pass_context
@handle_errors
def uninstall_command(ctx: click.Context, plugin: str) -> None:
    engine_uninstall(plugin, now=datetime.now(tz=UTC))
    if ctx.obj.get("json"):
        click.echo(json.dumps({"uninstalled": plugin}))
    else:
        click.echo(f"uninstalled {plugin}")


@cli.command("list", help="List installed plugins, or with --available, every discoverable plugin.")
@click.option(
    "--available",
    is_flag=True,
    help="List every discoverable plugin (in-tree and from every registered tap) instead.",
)
@click.pass_context
@handle_errors
def list_command(ctx: click.Context, available: bool) -> None:
    json_output = bool(ctx.obj.get("json"))
    if available:
        _print_available_plugins(json_output=json_output)
    else:
        _print_installed_plugins(json_output=json_output)


def _print_installed_plugins(*, json_output: bool) -> None:
    state = _load_state_or_empty()
    plugins = sorted(state.installed_plugins, key=lambda p: p.name) if state else []
    if json_output:
        click.echo(
            json.dumps(
                [
                    {
                        "name": p.name,
                        "version": p.version,
                        "type": _resolve_plugin_type(p.name),
                        "source": p.source,
                        "enabled": p.enabled,
                    }
                    for p in plugins
                ],
                default=str,
            )
        )
        return
    if not plugins:
        click.echo("(no plugins installed)")
        return
    for record in plugins:
        kind = _resolve_plugin_type(record.name) or "?"
        flag = "" if record.enabled else " (disabled)"
        click.echo(
            f"{record.name}  {record.version}  {kind}  source={record.source}{flag}"
        )


def _print_available_plugins(*, json_output: bool) -> None:
    state = _load_state_or_empty()
    if state is None:
        from cerebro.models import CerebroState

        state = CerebroState(
            core_version=__version__,
            vault_path=Path("~/ObsidianVault").expanduser(),
        )
    available = discover_available(state)
    rows = []
    for name in sorted(available):
        manifest = available[name].manifest  # type: ignore[attr-defined]
        source = available[name].source  # type: ignore[attr-defined]
        rows.append(
            {
                "name": name,
                "version": manifest.version,
                "type": manifest.type,
                "source": source,
                "description": manifest.description,
            }
        )
    if json_output:
        click.echo(json.dumps(rows))
        return
    if not rows:
        click.echo("(no plugins discoverable)")
        return
    for row in rows:
        click.echo(
            f"{row['name']}  {row['version']}  {row['type']}  source={row['source']}  "
            f"-- {row['description']}"
        )


def _resolve_plugin_type(name: str) -> str | None:
    state = _load_state_or_empty()
    if state is None:
        return None
    try:
        available = discover_available(state)
    except Exception:  # noqa: BLE001 - listing must not be blocked by tap conflicts
        return None
    discovered = available.get(name)
    if discovered is None:
        return None
    return discovered.manifest.type  # type: ignore[attr-defined]


@cli.command("enable", help="Enable a plugin and re-register its scheduled tasks.")
@click.argument("plugin")
@click.pass_context
@handle_errors
def enable_command(ctx: click.Context, plugin: str) -> None:
    changed = enable_plugin(plugin, now=datetime.now(tz=UTC))
    if ctx.obj.get("json"):
        click.echo(json.dumps({"plugin": plugin, "enabled": True, "changed": changed}))
    elif changed:
        click.echo(f"enabled {plugin}")
    else:
        click.echo(f"{plugin} is already enabled")


@cli.command("disable", help="Disable a plugin and tear down its scheduled tasks.")
@click.argument("plugin")
@click.pass_context
@handle_errors
def disable_command(ctx: click.Context, plugin: str) -> None:
    changed = disable_plugin(plugin, now=datetime.now(tz=UTC))
    if ctx.obj.get("json"):
        click.echo(json.dumps({"plugin": plugin, "enabled": False, "changed": changed}))
    elif changed:
        click.echo(f"disabled {plugin}")
    else:
        click.echo(f"{plugin} is already disabled")


@cli.group("tap", help="Manage plugin taps (out-of-tree git repositories).")
def tap_group() -> None:
    pass


@tap_group.command("add", help="Clone a tap from a git URL.")
@click.argument("git_url")
@click.option("--n", "name", default=None, help="Override the directory name (default: derived).")
@click.pass_context
@handle_errors
def tap_add_command(ctx: click.Context, git_url: str, name: str | None) -> None:
    info = add_tap(git_url, name=name)
    if ctx.obj.get("json"):
        click.echo(
            json.dumps(
                {
                    "name": info.name,
                    "path": str(info.path),
                    "url": info.url,
                    "plugin_count": info.plugin_count,
                }
            )
        )
    else:
        click.echo(f"added tap {info.name} ({info.plugin_count} plugins)")


@tap_group.command(
    "remove", help="Remove a tap (refuses while any of its plugins are installed)."
)
@click.argument("name")
@click.pass_context
@handle_errors
def tap_remove_command(ctx: click.Context, name: str) -> None:
    remove_tap(name)
    if ctx.obj.get("json"):
        click.echo(json.dumps({"removed": name}))
    else:
        click.echo(f"removed tap {name}")


@tap_group.command("list", help="List registered taps.")
@click.pass_context
@handle_errors
def tap_list_command(ctx: click.Context) -> None:
    taps = list_taps()
    if ctx.obj.get("json"):
        click.echo(
            json.dumps(
                [
                    {
                        "name": t.name,
                        "path": str(t.path),
                        "url": t.url,
                        "plugin_count": t.plugin_count,
                    }
                    for t in taps
                ]
            )
        )
        return
    if not taps:
        click.echo("(no taps registered)")
        return
    for tap in taps:
        url = tap.url or "(no origin)"
        click.echo(f"{tap.name}  {url}  ({tap.plugin_count} plugins)")


@tap_group.command("update", help="git pull on one tap, or all taps if no name is given.")
@click.argument("name", required=False)
@click.pass_context
@handle_errors
def tap_update_command(ctx: click.Context, name: str | None) -> None:
    updated = update_tap(name)
    if ctx.obj.get("json"):
        click.echo(json.dumps({"updated": updated}))
        return
    if not updated:
        click.echo("(no taps to update)")
        return
    for tap_name in updated:
        click.echo(f"updated {tap_name}")


@cli.command("doctor", help="Detect drift between Cerebro state and the filesystem.")
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Do not prompt; pair with --action to apply a uniform action to every drifted plugin.",
)
@click.option(
    "--action",
    type=click.Choice(["repair", "accept", "fail"]),
    default=None,
    help="Action for non-interactive mode: re-run install, accept disk state, or exit non-zero.",
)
@click.pass_context
@handle_errors
def doctor_command(ctx: click.Context, non_interactive: bool, action: str | None) -> None:
    json_output = bool(ctx.obj.get("json"))
    # --action implies non-interactive; --json never prompts.
    if action is not None or json_output:
        non_interactive = True

    reports = run_doctor()

    if json_output:
        _emit_doctor_json(reports, action)
        _maybe_apply_action(reports, action)
        return

    _print_doctor_table(reports)

    if not any(r.is_drifted for r in reports):
        return

    if non_interactive:
        if action == "fail":
            raise click.ClickException("drift detected (--action fail)")
        if action in ("repair", "accept"):
            _apply_doctor_action(reports, action)
        return

    _run_doctor_interactive(reports)


def _emit_doctor_json(reports: list[DriftReport], action: str | None) -> None:
    click.echo(
        json.dumps(
            {
                "reports": [report_to_dict(r) for r in reports],
                "action": action,
            },
            default=str,
        )
    )


def _maybe_apply_action(reports: list[DriftReport], action: str | None) -> None:
    if action in ("repair", "accept"):
        _apply_doctor_action(reports, action)
    elif action == "fail":
        if any(r.is_drifted for r in reports):
            raise click.ClickException("drift detected (--action fail)")


def _apply_doctor_action(reports: list[DriftReport], action: str) -> None:
    for report in reports:
        if not report.is_drifted:
            continue
        if action == "repair":
            repair_plugin(report.plugin_name)
            click.echo(f"repaired {report.plugin_name}")
        elif action == "accept":
            accept_plugin(report.plugin_name)
            click.echo(f"accepted {report.plugin_name}")


def _print_doctor_table(reports: list[DriftReport]) -> None:
    if not reports:
        click.echo("(no plugins installed)")
        return
    name_width = max(len("PLUGIN"), max(len(r.plugin_name) for r in reports))
    version_width = max(len("VERSION"), max(len(r.version) for r in reports))
    header = f"{'PLUGIN':<{name_width}}  {'VERSION':<{version_width}}  STATUS   DETAILS"
    click.echo(header)
    for report in reports:
        status = "drifted" if report.is_drifted else "ok"
        if report.manifest_missing:
            details = "install manifest is missing"
        elif report.is_drifted:
            counts = {"drifted": 0, "missing": 0}
            for c in report.checks:
                if c.status is OperationStatus.DRIFTED:
                    counts["drifted"] += 1
                elif c.status is OperationStatus.MISSING:
                    counts["missing"] += 1
            parts = [f"{v} {k}" for k, v in counts.items() if v]
            details = ", ".join(parts) if parts else ""
        else:
            details = ""
        click.echo(
            f"{report.plugin_name:<{name_width}}  "
            f"{report.version:<{version_width}}  "
            f"{status:<7}  {details}"
        )
    for report in reports:
        if not report.is_drifted:
            continue
        click.echo("")
        click.echo(f"{report.plugin_name}:")
        if report.manifest_missing:
            click.echo("  install manifest is missing; repair will re-run install")
            continue
        for check in report.drifted_checks():
            label = check.status.value
            click.echo(f"  {label:<8} {check.kind:<14} {check.target}  -- {check.detail}")


def _run_doctor_interactive(reports: list[DriftReport]) -> None:
    for report in reports:
        if not report.is_drifted:
            continue
        click.echo("")
        click.echo(f"{report.plugin_name}: drifted")
        choice = click.prompt(
            "  action",
            type=click.Choice(["repair", "accept", "skip"]),
            default="skip",
        )
        if choice == "repair":
            repair_plugin(report.plugin_name)
            click.echo(f"  repaired {report.plugin_name}")
        elif choice == "accept":
            accept_plugin(report.plugin_name)
            click.echo(f"  accepted {report.plugin_name}")
        else:
            click.echo(f"  skipped {report.plugin_name}")


@cli.command(
    "self-update",
    help="Update Cerebro itself by pulling its repo and reinstalling into its venv.",
)
@click.pass_context
@handle_errors
def self_update_command(ctx: click.Context) -> None:
    repo_root = _cerebro_repo_root()
    if repo_root is None:
        raise click.ClickException(
            "could not locate the Cerebro repository on disk; "
            "self-update only works for clones installed by the bootstrap script"
        )
    subprocess.run(["git", "-C", str(repo_root), "pull", "--ff-only"], check=True)
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", str(repo_root)],
        check=True,
    )
    if ctx.obj.get("json"):
        click.echo(json.dumps({"updated": True, "repo": str(repo_root)}))
    else:
        click.echo(f"self-updated from {repo_root}")


def _cerebro_repo_root() -> Path | None:
    """Walk up from this file looking for the repo's pyproject.toml."""
    current = Path(__file__).resolve()
    for parent in (current, *current.parents):
        candidate = parent / "pyproject.toml"
        if candidate.is_file():
            return parent
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point.

    Click normally raises ``SystemExit``; we translate that into an int
    return so callers (and our test harness) can use the function
    directly without trapping ``SystemExit``.
    """
    args = list(argv) if argv is not None else None
    try:
        cli.main(
            args=args,
            prog_name="cerebro",
            standalone_mode=False,
        )
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.exceptions.Exit as exc:
        return int(exc.exit_code)
    except SystemExit as exc:  # pragma: no cover - defensive
        code = exc.code
        if code is None:
            return EXIT_OK
        return int(code) if isinstance(code, int) else 1
    return EXIT_OK


__all__ = ["cli", "main"]
