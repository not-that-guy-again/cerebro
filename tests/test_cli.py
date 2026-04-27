"""End-to-end CLI tests against synthetic plugins.

Two layers:

- Help-text snapshot tests catch UX drift (changing help wording is a
  deliberate act that updates the snapshot in the same commit).
- A scripted ``cerebro`` flow drives ``init``, ``list``, ``install``,
  ``uninstall`` and the tap commands against the same synthetic
  fixtures the engine tests use, with a fake package manager and
  scheduler so nothing touches the host system.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from cerebro.cli import cli, main
from cerebro.cli._errors import (
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_PRECONDITION,
)
from cerebro.runtime.platform import Platform, PlatformComponents
from tests.test_recorder import FakePackageManager, FakeScheduler

_FIXTURES = Path(__file__).parent / "fixtures" / "plugins"

_HELP_SNAPSHOTS: dict[tuple[str, ...], str] = {
    (): """\
Usage: cerebro [OPTIONS] COMMAND [ARGS]...

  Bootstrap and maintain an agentic development environment.

Options:
  --version   Show the version and exit.
  --verbose   Stream the engine's debug log to stderr in addition to the log
              file.
  --json      Emit machine-readable JSON for commands that support it.
  -h, --help  Show this message and exit.

Commands:
  disable      Disable a plugin and tear down its scheduled tasks.
  doctor       Detect drift between Cerebro state and the filesystem.
  enable       Enable a plugin and re-register its scheduled tasks.
  init         Interactively configure the vault and install starter plugins.
  install      Install a plugin and its dependencies.
  list         List installed plugins, or with --available, every...
  self-update  Update Cerebro itself by pulling its repo and reinstalling...
  tap          Manage plugin taps (out-of-tree git repositories).
  uninstall    Uninstall a plugin (refuses if other plugins depend on it).
""",
    ("init",): """\
Usage: cerebro init [OPTIONS]

  Interactively configure the vault and install starter plugins.

Options:
  -h, --help  Show this message and exit.
""",
    ("install",): """\
Usage: cerebro install [OPTIONS] PLUGIN

  Install a plugin and its dependencies.

Options:
  -h, --help  Show this message and exit.
""",
    ("uninstall",): """\
Usage: cerebro uninstall [OPTIONS] PLUGIN

  Uninstall a plugin (refuses if other plugins depend on it).

Options:
  -h, --help  Show this message and exit.
""",
    ("list",): """\
Usage: cerebro list [OPTIONS]

  List installed plugins, or with --available, every discoverable plugin.

Options:
  --available  List every discoverable plugin (in-tree and from every registered
               tap) instead.
  -h, --help   Show this message and exit.
""",
    ("enable",): """\
Usage: cerebro enable [OPTIONS] PLUGIN

  Enable a plugin and re-register its scheduled tasks.

Options:
  -h, --help  Show this message and exit.
""",
    ("disable",): """\
Usage: cerebro disable [OPTIONS] PLUGIN

  Disable a plugin and tear down its scheduled tasks.

Options:
  -h, --help  Show this message and exit.
""",
    ("doctor",): """\
Usage: cerebro doctor [OPTIONS]

  Detect drift between Cerebro state and the filesystem.

Options:
  --non-interactive              Do not prompt; pair with --action to apply a
                                 uniform action to every drifted plugin.
  --action [repair|accept|fail]  Action for non-interactive mode: re-run
                                 install, accept disk state, or exit non-zero.
  -h, --help                     Show this message and exit.
""",
    ("self-update",): """\
Usage: cerebro self-update [OPTIONS]

  Update Cerebro itself by pulling its repo and reinstalling into its venv.

Options:
  -h, --help  Show this message and exit.
""",
    ("tap",): """\
Usage: cerebro tap [OPTIONS] COMMAND [ARGS]...

  Manage plugin taps (out-of-tree git repositories).

Options:
  -h, --help  Show this message and exit.

Commands:
  add     Clone a tap from a git URL.
  list    List registered taps.
  remove  Remove a tap (refuses while any of its plugins are installed).
  update  git pull on one tap, or all taps if no name is given.
""",
    ("tap", "add"): """\
Usage: cerebro tap add [OPTIONS] GIT_URL

  Clone a tap from a git URL.

Options:
  --n TEXT    Override the directory name (default: derived).
  -h, --help  Show this message and exit.
""",
    ("tap", "remove"): """\
Usage: cerebro tap remove [OPTIONS] NAME

  Remove a tap (refuses while any of its plugins are installed).

Options:
  -h, --help  Show this message and exit.
""",
    ("tap", "list"): """\
Usage: cerebro tap list [OPTIONS]

  List registered taps.

Options:
  -h, --help  Show this message and exit.
""",
    ("tap", "update"): """\
Usage: cerebro tap update [OPTIONS] [NAME]

  git pull on one tap, or all taps if no name is given.

Options:
  -h, --help  Show this message and exit.
""",
}


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cerebro_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))
    return home


@pytest.fixture
def fixture_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "fixture-output"
    target.mkdir()
    monkeypatch.setenv("CEREBRO_TEST_FIXTURE_DIR", str(target))
    return target


@pytest.fixture
def fake_components() -> PlatformComponents:
    return PlatformComponents(
        platform=Platform.MACOS,
        package_manager=FakePackageManager(),
        scheduler=FakeScheduler(),
    )


@pytest.fixture
def patch_engine_to_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    fake_components: PlatformComponents,
) -> Iterator[None]:
    """Force engine + tap helpers to use the fixture plugins root.

    The CLI deliberately calls the engine without ``in_tree_root`` /
    ``components`` overrides — production callers do too — so we patch
    the imported names inside the CLI module to redirect them.
    """
    import cerebro.cli as cli_module

    real_install = cli_module.engine_install
    real_uninstall = cli_module.engine_uninstall
    real_discover = cli_module.discover_available

    def install_with_fixtures(plugin: str, **kwargs):
        kwargs.setdefault("in_tree_root", _FIXTURES)
        kwargs.setdefault("components", fake_components)
        return real_install(plugin, **kwargs)

    def uninstall_with_fixtures(plugin: str, **kwargs):
        kwargs.setdefault("in_tree_root", _FIXTURES)
        kwargs.setdefault("components", fake_components)
        return real_uninstall(plugin, **kwargs)

    def discover_with_fixtures(state, **kwargs):
        kwargs.setdefault("in_tree_root", _FIXTURES)
        return real_discover(state, **kwargs)

    monkeypatch.setattr(cli_module, "engine_install", install_with_fixtures)
    monkeypatch.setattr(cli_module, "engine_uninstall", uninstall_with_fixtures)
    monkeypatch.setattr(cli_module, "discover_available", discover_with_fixtures)
    yield


@pytest.mark.parametrize("path", sorted(_HELP_SNAPSHOTS))
def test_help_text_snapshot(runner: CliRunner, path: tuple[str, ...]) -> None:
    args = [*path, "--help"]
    result = runner.invoke(cli, args)
    assert result.exit_code == EXIT_OK, result.output
    assert result.output == _HELP_SNAPSHOTS[path]


def test_version(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == EXIT_OK
    assert "cerebro, version" in result.output


def test_list_empty_state(runner: CliRunner, cerebro_home: Path) -> None:
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == EXIT_OK
    assert result.output.strip() == "(no plugins installed)"


def test_list_available_json(
    runner: CliRunner,
    cerebro_home: Path,
    patch_engine_to_fixtures: None,
) -> None:
    result = runner.invoke(cli, ["--json", "list", "--available"])
    assert result.exit_code == EXIT_OK, result.output
    rows = json.loads(result.output)
    names = {row["name"] for row in rows}
    assert {"demo-ide", "demo-agent", "other-ide"} <= names


def test_install_unknown_plugin_exits_3(
    runner: CliRunner,
    cerebro_home: Path,
    patch_engine_to_fixtures: None,
) -> None:
    result = runner.invoke(cli, ["install", "no-such-plugin"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert "no-such-plugin" in result.stderr
    assert "Traceback" not in result.stderr


def test_install_then_list_then_uninstall(
    runner: CliRunner,
    cerebro_home: Path,
    fixture_dir: Path,
    patch_engine_to_fixtures: None,
) -> None:
    result = runner.invoke(cli, ["install", "demo-ide"])
    assert result.exit_code == EXIT_OK, result.stderr or result.output
    assert "installed demo-ide" in result.output

    result = runner.invoke(cli, ["list"])
    assert result.exit_code == EXIT_OK
    assert "demo-ide" in result.output

    result = runner.invoke(cli, ["--json", "list"])
    assert result.exit_code == EXIT_OK
    rows = json.loads(result.output)
    assert [r["name"] for r in rows] == ["demo-ide"]
    assert rows[0]["enabled"] is True

    result = runner.invoke(cli, ["uninstall", "demo-ide"])
    assert result.exit_code == EXIT_OK
    assert "uninstalled demo-ide" in result.output

    result = runner.invoke(cli, ["list"])
    assert "(no plugins installed)" in result.output


def test_uninstall_dependency_in_use_exits_4(
    runner: CliRunner,
    cerebro_home: Path,
    fixture_dir: Path,
    patch_engine_to_fixtures: None,
) -> None:
    assert runner.invoke(cli, ["install", "demo-ide"]).exit_code == EXIT_OK
    assert runner.invoke(cli, ["install", "demo-agent"]).exit_code == EXIT_OK

    result = runner.invoke(cli, ["uninstall", "demo-ide"])
    assert result.exit_code == EXIT_PRECONDITION
    assert "demo-agent" in result.stderr


def test_enable_disable_round_trip(
    runner: CliRunner,
    cerebro_home: Path,
    fixture_dir: Path,
    patch_engine_to_fixtures: None,
) -> None:
    assert runner.invoke(cli, ["install", "demo-ide"]).exit_code == EXIT_OK

    result = runner.invoke(cli, ["disable", "demo-ide"])
    assert result.exit_code == EXIT_OK
    assert "disabled demo-ide" in result.output

    result = runner.invoke(cli, ["--json", "list"])
    assert result.exit_code == EXIT_OK
    rows = json.loads(result.output)
    assert rows[0]["enabled"] is False

    result = runner.invoke(cli, ["disable", "demo-ide"])
    assert "already disabled" in result.output

    result = runner.invoke(cli, ["enable", "demo-ide"])
    assert result.exit_code == EXIT_OK
    assert "enabled demo-ide" in result.output


def test_tap_add_and_remove_with_fake_git(
    runner: CliRunner,
    cerebro_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end tap flow against a local file:// git repo (no network)."""
    upstream = tmp_path / "demo-tap-upstream"
    _make_local_tap(upstream, plugin_name="tap-only-plugin")

    # Build a bare clone so file:// can be used as a remote URL.
    bare = tmp_path / "demo-tap-upstream.git"
    subprocess.run(
        ["git", "clone", "--bare", str(upstream), str(bare)],
        check=True,
        capture_output=True,
    )
    url = bare.as_uri()

    result = runner.invoke(cli, ["tap", "add", url])
    assert result.exit_code == EXIT_OK, result.stderr or result.output
    assert "added tap demo-tap-upstream" in result.output

    result = runner.invoke(cli, ["tap", "list"])
    assert result.exit_code == EXIT_OK
    assert "demo-tap-upstream" in result.output

    result = runner.invoke(cli, ["--json", "tap", "list"])
    rows = json.loads(result.output)
    assert rows[0]["name"] == "demo-tap-upstream"
    assert rows[0]["plugin_count"] == 1

    result = runner.invoke(cli, ["tap", "update", "demo-tap-upstream"])
    assert result.exit_code == EXIT_OK
    assert "updated demo-tap-upstream" in result.output

    result = runner.invoke(cli, ["tap", "remove", "demo-tap-upstream"])
    assert result.exit_code == EXIT_OK

    result = runner.invoke(cli, ["tap", "list"])
    assert "(no taps registered)" in result.output

    del monkeypatch


def test_tap_remove_unknown_exits_4(runner: CliRunner, cerebro_home: Path) -> None:
    result = runner.invoke(cli, ["tap", "remove", "ghost"])
    assert result.exit_code == EXIT_PRECONDITION
    assert "ghost" in result.stderr


def test_doctor_clean_state(runner: CliRunner, cerebro_home: Path) -> None:
    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == EXIT_OK
    assert "(no plugins installed)" in result.output


def test_init_drives_install(
    runner: CliRunner,
    cerebro_home: Path,
    fixture_dir: Path,
    fake_components: PlatformComponents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the full init flow non-interactively by patching ``run_init``.

    We can't easily script the multi-select prompt through CliRunner
    (numbered indices change with fixture order), so we exercise the
    underlying ``run_init`` directly with ``auto_select`` and let the
    real engine + state writes happen. That gives us coverage of the
    init -> install path end to end.
    """
    from cerebro.cli._init import run_init

    run_init(
        home=cerebro_home,
        in_tree_root=_FIXTURES,
        components=fake_components,
        core_version="0.0.0",
        prompt_vault=False,
        auto_select={"ide": ["demo-ide"], "agent": ["demo-agent"]},
    )
    state_path = cerebro_home / "state.yaml"
    assert state_path.exists()
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == EXIT_OK
    assert "demo-ide" in result.output
    assert "demo-agent" in result.output
    del monkeypatch


def test_main_returns_zero_for_help() -> None:
    rc = main(["--help"])
    assert rc == EXIT_OK


def test_self_update_uses_repo_root(
    runner: CliRunner,
    cerebro_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(args, check, **kwargs):  # noqa: ARG001
        calls.append(args)
        return mock.Mock(returncode=0)

    monkeypatch.setattr("cerebro.cli.subprocess.run", fake_run)
    result = runner.invoke(cli, ["self-update"])
    assert result.exit_code == EXIT_OK, result.stderr or result.output
    assert any("pull" in a for a in calls[0])
    assert any("install" in a for a in calls[1])


def _make_local_tap(path: Path, *, plugin_name: str) -> None:
    """Create a tiny git repo on disk that looks like a Cerebro tap."""
    plugin_dir = path / "plugins" / plugin_name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "\n".join(
            [
                f"name: {plugin_name}",
                "version: 1.0.0",
                "type: behavior",
                "description: A behavior plugin from a local tap.",
                "dependencies: {}",
                "min_core_version: 0.0.0",
                "supported_platforms:",
                "  - macos",
                "  - linux",
                "hooks_declared:",
                "  - install",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (plugin_dir / "plugin.py").write_text(
        "def install(ctx): pass\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "init"],
        check=True,
    )
