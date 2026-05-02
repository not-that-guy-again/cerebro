"""Briefings runtime helpers shared by the workflow plugin and the CLI.

Two pieces of functionality live here:

- :func:`gather_context` walks the vault's ``repos/`` directory to
  enumerate tracked repos, then runs ``git log`` against each to assemble
  a markdown summary of recent activity for a given period. The output
  is what the slash command scripts embed via Claude Code's ``!command``
  syntax so the agent has concrete context to summarise.

- :func:`write_stub_note` materialises a stub markdown file under
  ``<vault>/briefings/<period>/<date>.md`` containing the gathered
  context. The scheduled tasks installed by the briefings plugin invoke
  ``cerebro internal briefings-write <period>`` which calls into this
  helper. Headless agent invocation is out of scope for v1; the user
  fills the stub in interactively from a slash command.

Both helpers stay agent-agnostic on purpose so a future agent plugin can
reuse them without depending on Claude Code specifics. They also avoid
the operation recorder: the data they touch is the user's vault content,
which lives outside Cerebro's transactional surface (same posture as the
Obsidian helper in :mod:`cerebro.runtime.obsidian`).
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

Period = Literal["daily", "weekly", "monthly", "quarterly", "annual"]

PERIODS: tuple[Period, ...] = ("daily", "weekly", "monthly", "quarterly", "annual")

_REPOS_DIRNAME = "repos"
_BRIEFINGS_DIRNAME = "briefings"
# A repo note may declare its filesystem location as either:
#   - path: /Users/me/repos/foo
# or a markdown link:
#   [foo](/Users/me/repos/foo)
# We accept either; the first match wins.
_PATH_FRONTMATTER_RE = re.compile(r"^\s*-?\s*path:\s*(?P<path>\S.*?)\s*$", re.MULTILINE)
_PATH_LINK_RE = re.compile(r"\]\((?P<path>/[^)]+)\)")


@dataclass(frozen=True)
class TrackedRepo:
    name: str
    path: Path
    note_path: Path


def list_tracked_repos(vault_path: Path) -> list[TrackedRepo]:
    """Enumerate repos referenced by ``<vault>/repos/*.md``.

    Notes that do not point at an existing directory are skipped; the
    vault is the user's data and may contain stubs an agent has not
    finished filling in. Returns repos sorted by name for determinism.
    """
    repos_dir = Path(vault_path) / _REPOS_DIRNAME
    if not repos_dir.is_dir():
        return []
    out: list[TrackedRepo] = []
    for note in sorted(repos_dir.glob("*.md")):
        path = _extract_repo_path(note)
        if path is None:
            continue
        if not path.is_dir():
            continue
        out.append(TrackedRepo(name=note.stem, path=path, note_path=note))
    return out


def _extract_repo_path(note: Path) -> Path | None:
    text = note.read_text(encoding="utf-8")
    m = _PATH_FRONTMATTER_RE.search(text)
    if m is not None:
        return Path(m.group("path")).expanduser()
    m = _PATH_LINK_RE.search(text)
    if m is not None:
        return Path(m.group("path")).expanduser()
    return None


def period_window(period: Period, *, today: date | None = None) -> tuple[date, date]:
    """Return ``(since, until)`` dates for a briefings period.

    ``until`` is the day after ``today`` so a single ``git log`` window
    includes commits made earlier the same day. Window ends are exclusive
    when fed to git via ``--since`` / ``--until``.
    """
    today = today or date.today()
    until = today + timedelta(days=1)
    if period == "daily":
        return today, until
    if period == "weekly":
        return today - timedelta(days=7), until
    if period == "monthly":
        return today - timedelta(days=30), until
    if period == "quarterly":
        return today - timedelta(days=90), until
    if period == "annual":
        return today - timedelta(days=365), until
    raise ValueError(f"unknown period: {period!r}")


GitRunner = Sequence[str]


def gather_context(
    period: Period,
    *,
    vault_path: Path,
    today: date | None = None,
    git_runner: object = subprocess,
) -> str:
    """Build a markdown summary of recent git activity across tracked repos.

    The output is what slash command scripts embed via ``!cerebro
    internal briefings-context <period>`` so the agent sees the same
    facts a scheduled run would. ``git_runner`` is injectable for tests;
    production calls go through stdlib ``subprocess``.
    """
    today = today or date.today()
    since, until = period_window(period, today=today)
    repos = list_tracked_repos(vault_path)

    lines: list[str] = []
    lines.append(f"# Briefings context — {period} ({today.isoformat()})")
    lines.append("")
    lines.append(f"Window: {since.isoformat()} .. {until.isoformat()} (until exclusive)")
    lines.append("")

    if not repos:
        lines.append("No tracked repos found in `repos/` of the vault.")
        lines.append("")
        return "\n".join(lines)

    lines.append("## Tracked repos")
    for repo in repos:
        lines.append(f"- {repo.name} — {repo.path}")
    lines.append("")

    lines.append("## Git activity")
    for repo in repos:
        log = _git_log(
            repo.path,
            since=since.isoformat(),
            until=until.isoformat(),
            runner=git_runner,
        )
        lines.append(f"### {repo.name}")
        if not log.strip():
            lines.append("(no commits in window)")
        else:
            lines.append("```")
            lines.append(log.strip())
            lines.append("```")
        lines.append("")

    return "\n".join(lines)


def _git_log(
    repo: Path, *, since: str, until: str, runner: object
) -> str:
    argv = [
        "git",
        "-C",
        str(repo),
        "log",
        f"--since={since}",
        f"--until={until}",
        "--pretty=format:%h %an %ad %s",
        "--date=short",
    ]
    run = getattr(runner, "run", None)
    if run is None:
        raise TypeError("git_runner must expose a callable `run` attribute")
    result = run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return f"(git log failed: {result.stderr.strip() or 'unknown error'})"
    return result.stdout


def briefings_dir(vault_path: Path, period: Period) -> Path:
    return Path(vault_path) / _BRIEFINGS_DIRNAME / period


def stub_note_path(vault_path: Path, period: Period, *, today: date | None = None) -> Path:
    today = today or date.today()
    return briefings_dir(vault_path, period) / f"{today.isoformat()}.md"


def write_stub_note(
    period: Period,
    *,
    vault_path: Path,
    today: date | None = None,
    git_runner: object = subprocess,
    when: datetime | None = None,
) -> Path:
    """Write a stub note for ``period`` under the vault, return the path.

    Idempotent: if the stub already exists, the existing file is left
    alone and its path returned. The stub is filled with collected git
    log data and intentionally left unfinished — the user wraps prose
    around the facts interactively from a slash command.
    """
    today = today or date.today()
    when = when or datetime.now()
    target = stub_note_path(vault_path, period, today=today)
    if target.exists():
        return target
    context = gather_context(
        period, vault_path=vault_path, today=today, git_runner=git_runner
    )
    body = (
        f"# {period.capitalize()} briefing — {today.isoformat()}\n"
        f"\n"
        f"_Generated by Cerebro at {when.isoformat(timespec='minutes')}._\n"
        f"\n"
        f"{context}\n"
        f"## Notes\n"
        f"\n"
        f"<!-- Replace this block with your interactive briefing prose. -->\n"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


__all__ = [
    "PERIODS",
    "Period",
    "TrackedRepo",
    "briefings_dir",
    "gather_context",
    "list_tracked_repos",
    "period_window",
    "stub_note_path",
    "write_stub_note",
]
