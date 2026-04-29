"""Tests for ``cerebro.runtime.briefings``.

The runtime module is shared between the briefings plugin and the
``cerebro internal briefings-*`` CLI commands. These tests exercise the
shared helpers directly with synthetic vaults and a fake git runner so
no real git commands run.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from cerebro.runtime.briefings import (
    PERIODS,
    TrackedRepo,
    briefings_dir,
    gather_context,
    list_tracked_repos,
    period_window,
    stub_note_path,
    write_stub_note,
)


class FakeGitRunner:
    """``subprocess``-shaped stub that returns a canned ``git log`` per repo."""

    def __init__(self, *, output_by_repo: dict[str, str] | None = None) -> None:
        self.output_by_repo = dict(output_by_repo or {})
        self.calls: list[list[str]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
    ) -> SimpleNamespace:
        del capture_output, text, check
        argv_list = list(argv)
        self.calls.append(argv_list)
        # argv = ["git", "-C", "<path>", "log", ...]
        repo_path = argv_list[2]
        repo_name = Path(repo_path).name
        stdout = self.output_by_repo.get(repo_name, "")
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _make_repo_note(
    vault: Path,
    name: str,
    *,
    repo_path: Path,
    style: str = "frontmatter",
) -> Path:
    """Write a vault repo note pointing at ``repo_path`` and return its path."""
    repos = vault / "repos"
    repos.mkdir(parents=True, exist_ok=True)
    note = repos / f"{name}.md"
    if style == "frontmatter":
        body = (
            f"# {name}\n"
            f"\n"
            f"- path: {repo_path}\n"
            f"\n"
            f"context goes here\n"
        )
    elif style == "link":
        body = f"# {name}\n\nWorking copy: [local]({repo_path})\n"
    else:
        raise ValueError(style)
    note.write_text(body, encoding="utf-8")
    return note


def _make_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / "code" / name
    repo.mkdir(parents=True, exist_ok=True)
    return repo


# ---------------------------------------------------------------------------
# list_tracked_repos
# ---------------------------------------------------------------------------


def test_list_tracked_repos_returns_repos_with_frontmatter_path(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    repo = _make_repo(tmp_path, "alpha")
    _make_repo_note(vault, "alpha", repo_path=repo)

    out = list_tracked_repos(vault)

    assert out == [TrackedRepo(name="alpha", path=repo, note_path=vault / "repos" / "alpha.md")]


def test_list_tracked_repos_returns_repos_with_markdown_link_path(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    repo = _make_repo(tmp_path, "beta")
    _make_repo_note(vault, "beta", repo_path=repo, style="link")

    out = list_tracked_repos(vault)

    assert [r.name for r in out] == ["beta"]
    assert out[0].path == repo


def test_list_tracked_repos_skips_notes_without_a_path(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    repos = vault / "repos"
    repos.mkdir(parents=True)
    (repos / "stub.md").write_text("# stub\n\nno path yet\n", encoding="utf-8")

    out = list_tracked_repos(vault)

    assert out == []


def test_list_tracked_repos_skips_notes_pointing_at_missing_dirs(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_repo_note(vault, "ghost", repo_path=tmp_path / "does-not-exist")

    out = list_tracked_repos(vault)

    assert out == []


def test_list_tracked_repos_returns_empty_when_repos_dir_missing(tmp_path: Path) -> None:
    assert list_tracked_repos(tmp_path / "no-vault") == []


# ---------------------------------------------------------------------------
# period_window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "period,delta_days",
    [
        ("daily", 0),
        ("weekly", 7),
        ("monthly", 30),
        ("quarterly", 90),
        ("annual", 365),
    ],
)
def test_period_window_returns_expected_offset(period: str, delta_days: int) -> None:
    today = date(2026, 4, 27)
    since, until = period_window(period, today=today)  # type: ignore[arg-type]
    assert (today - since).days == delta_days
    assert (until - today).days == 1


def test_period_window_rejects_unknown_period() -> None:
    with pytest.raises(ValueError, match="unknown period"):
        period_window("bogus", today=date(2026, 4, 27))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# gather_context
# ---------------------------------------------------------------------------


def test_gather_context_includes_each_repos_git_log(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    alpha = _make_repo(tmp_path, "alpha")
    beta = _make_repo(tmp_path, "beta")
    _make_repo_note(vault, "alpha", repo_path=alpha)
    _make_repo_note(vault, "beta", repo_path=beta)

    runner = FakeGitRunner(
        output_by_repo={
            "alpha": "abc123 brent 2026-04-27 fix(parser): handle EOF\n",
            "beta": "def456 brent 2026-04-27 add tests\n",
        }
    )

    out = gather_context(
        "daily",
        vault_path=vault,
        today=date(2026, 4, 27),
        git_runner=runner,
    )

    assert "## Tracked repos" in out
    assert "- alpha" in out
    assert "- beta" in out
    assert "### alpha" in out
    assert "### beta" in out
    assert "fix(parser): handle EOF" in out
    assert "add tests" in out
    # We invoked git --since/--until with the daily window.
    daily_since = "--since=2026-04-27"
    assert any(daily_since in call for call in runner.calls)


def test_gather_context_when_no_repos_says_so(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "repos").mkdir(parents=True)

    out = gather_context(
        "daily",
        vault_path=vault,
        today=date(2026, 4, 27),
        git_runner=FakeGitRunner(),
    )

    assert "No tracked repos" in out


def test_gather_context_marks_repos_with_no_commits(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    repo = _make_repo(tmp_path, "quiet")
    _make_repo_note(vault, "quiet", repo_path=repo)

    runner = FakeGitRunner(output_by_repo={"quiet": ""})
    out = gather_context(
        "weekly",
        vault_path=vault,
        today=date(2026, 4, 27),
        git_runner=runner,
    )

    assert "(no commits in window)" in out


def test_gather_context_handles_git_failure_gracefully(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    repo = _make_repo(tmp_path, "broken")
    _make_repo_note(vault, "broken", repo_path=repo)

    class FailingRunner:
        def run(self, argv, *, capture_output, text, check):  # type: ignore[no-untyped-def]
            del argv, capture_output, text, check
            return SimpleNamespace(
                returncode=128,
                stdout="",
                stderr="not a git repository",
            )

    out = gather_context(
        "daily",
        vault_path=vault,
        today=date(2026, 4, 27),
        git_runner=FailingRunner(),
    )

    assert "git log failed" in out


# ---------------------------------------------------------------------------
# write_stub_note
# ---------------------------------------------------------------------------


def test_write_stub_note_creates_file_with_context_and_notes_section(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    repo = _make_repo(tmp_path, "alpha")
    _make_repo_note(vault, "alpha", repo_path=repo)
    runner = FakeGitRunner(
        output_by_repo={"alpha": "abc123 brent 2026-04-27 ship feature\n"}
    )

    today = date(2026, 4, 27)
    path = write_stub_note(
        "daily",
        vault_path=vault,
        today=today,
        git_runner=runner,
    )

    assert path == stub_note_path(vault, "daily", today=today)
    assert path.parent == briefings_dir(vault, "daily")
    body = path.read_text(encoding="utf-8")
    assert "# Daily briefing — 2026-04-27" in body
    assert "ship feature" in body
    assert "## Notes" in body


def test_write_stub_note_is_idempotent_when_target_exists(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    today = date(2026, 4, 27)
    target = stub_note_path(vault, "daily", today=today)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# already here\n", encoding="utf-8")

    path = write_stub_note(
        "daily",
        vault_path=vault,
        today=today,
        git_runner=FakeGitRunner(),
    )

    assert path == target
    assert path.read_text(encoding="utf-8") == "# already here\n"


# ---------------------------------------------------------------------------
# PERIODS sanity check
# ---------------------------------------------------------------------------


def test_periods_constant_matches_expected_set() -> None:
    assert PERIODS == ("daily", "weekly", "monthly", "quarterly", "annual")
