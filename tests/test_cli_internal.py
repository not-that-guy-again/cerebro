"""Tests for the hidden ``cerebro internal`` command group.

The group exists so plugin-installed scheduled tasks have a stable place
to shell into. ``briefings-context`` and ``briefings-write`` are exercised
end-to-end through the ``CliRunner`` against a synthetic vault and a
patched git runner; nothing real shells out.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from cerebro.cli import cli
from cerebro.cli._errors import EXIT_OK
from cerebro.models import CerebroState
from cerebro.state import save_state


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cerebro_home_with_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    save_state(
        CerebroState(core_version="0.1.0", vault_path=vault),
        home / "state.yaml",
    )
    monkeypatch.setenv("CEREBRO_HOME", str(home))
    return home, vault


def _patch_git_runner(
    monkeypatch: pytest.MonkeyPatch, output_by_repo: dict[str, str]
) -> None:
    """Replace subprocess.run inside the briefings runtime with a fake."""

    def fake_run(
        argv: Sequence[str],
        *,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
    ) -> SimpleNamespace:
        del capture_output, text, check
        argv_list = list(argv)
        repo_path = argv_list[2]
        repo_name = Path(repo_path).name
        return SimpleNamespace(
            returncode=0,
            stdout=output_by_repo.get(repo_name, ""),
            stderr="",
        )

    import cerebro.runtime.briefings as briefings_mod

    monkeypatch.setattr(briefings_mod.subprocess, "run", fake_run)


def _add_repo_note(vault: Path, name: str, repo_path: Path) -> None:
    repos = vault / "repos"
    repos.mkdir(parents=True, exist_ok=True)
    (repos / f"{name}.md").write_text(
        f"# {name}\n\n- path: {repo_path}\n",
        encoding="utf-8",
    )


def test_internal_group_is_hidden_from_top_level_help(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == EXIT_OK
    assert "internal" not in result.output


def test_internal_briefings_context_emits_markdown(
    runner: CliRunner,
    cerebro_home_with_vault: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, vault = cerebro_home_with_vault
    repo = tmp_path / "code" / "alpha"
    repo.mkdir(parents=True)
    _add_repo_note(vault, "alpha", repo)
    _patch_git_runner(
        monkeypatch,
        {"alpha": "abc123 brent 2026-04-27 fix(parser)\n"},
    )

    result = runner.invoke(cli, ["internal", "briefings-context", "daily"])
    assert result.exit_code == EXIT_OK, result.stderr or result.output
    assert "## Tracked repos" in result.output
    assert "- alpha" in result.output
    assert "fix(parser)" in result.output


def test_internal_briefings_context_rejects_unknown_period(
    runner: CliRunner, cerebro_home_with_vault: tuple[Path, Path]
) -> None:
    result = runner.invoke(cli, ["internal", "briefings-context", "fortnightly"])
    assert result.exit_code != EXIT_OK
    assert "fortnightly" in result.stderr or "fortnightly" in result.output


def test_internal_briefings_write_creates_stub_note(
    runner: CliRunner,
    cerebro_home_with_vault: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, vault = cerebro_home_with_vault
    repo = tmp_path / "code" / "alpha"
    repo.mkdir(parents=True)
    _add_repo_note(vault, "alpha", repo)
    _patch_git_runner(monkeypatch, {"alpha": "abc123 brent 2026-04-27 ship\n"})

    result = runner.invoke(cli, ["internal", "briefings-write", "daily"])
    assert result.exit_code == EXIT_OK, result.stderr or result.output

    written = Path(result.output.strip())
    assert written.exists()
    assert written.parent == vault / "briefings" / "daily"
    body = written.read_text(encoding="utf-8")
    assert "Daily briefing" in body
    assert "ship" in body
    assert "## Notes" in body


def test_internal_briefings_write_is_idempotent(
    runner: CliRunner,
    cerebro_home_with_vault: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, vault = cerebro_home_with_vault
    repo = tmp_path / "code" / "alpha"
    repo.mkdir(parents=True)
    _add_repo_note(vault, "alpha", repo)
    _patch_git_runner(monkeypatch, {"alpha": "abc123 brent 2026-04-27 ship\n"})

    first = runner.invoke(cli, ["internal", "briefings-write", "daily"])
    assert first.exit_code == EXIT_OK
    written = Path(first.output.strip())
    written.write_text("# user-finished briefing\n", encoding="utf-8")

    second = runner.invoke(cli, ["internal", "briefings-write", "daily"])
    assert second.exit_code == EXIT_OK
    # Idempotent: existing file is left alone.
    assert written.read_text(encoding="utf-8") == "# user-finished briefing\n"


def test_internal_briefings_write_json_emits_path(
    runner: CliRunner,
    cerebro_home_with_vault: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, vault = cerebro_home_with_vault
    repo = tmp_path / "code" / "alpha"
    repo.mkdir(parents=True)
    _add_repo_note(vault, "alpha", repo)
    _patch_git_runner(monkeypatch, {"alpha": ""})

    result = runner.invoke(cli, ["--json", "internal", "briefings-write", "weekly"])
    assert result.exit_code == EXIT_OK, result.stderr or result.output
    payload = json.loads(result.output)
    assert payload["period"] == "weekly"
    assert Path(payload["path"]).exists()


def test_internal_briefings_write_errors_when_state_missing(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))

    result = runner.invoke(cli, ["internal", "briefings-write", "daily"])
    assert result.exit_code != EXIT_OK
    assert "cerebro init" in result.stderr or "cerebro init" in result.output
