"""Tests for the in-tree coding-standards behavior plugin.

Covers the unit-level hook contracts (the picker → render → block flow,
configure-on-new-agent, verify, drift detection) and an end-to-end
install + uninstall through the engine. The CLI ``code`` and ``claude``
binaries, npm, and brew are all faked; no real system mutation happens.

The unit tests redirect ``~/.claude`` under ``tmp_path`` via a patched
``Path.expanduser`` because the plugin's discovery loop re-imports the
real ``cerebro/plugins/claude-code/plugin.py`` and calls its
``rules_file_path`` directly, which would otherwise resolve to the
user's actual ``~/.claude/CLAUDE.md``.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from cerebro.models import CerebroState, InstalledPlugin, PluginManifest
from cerebro.plugins.loader import (
    IN_TREE_SOURCE,
    DiscoveredPlugin,
    load_plugin_module,
)
from cerebro.runtime.context import HookContext, build_context
from cerebro.runtime.engine import PluginAlreadyInstalledError
from cerebro.runtime.engine import install as engine_install
from cerebro.runtime.engine import uninstall as engine_uninstall
from cerebro.runtime.platform import Platform, PlatformComponents
from cerebro.runtime.prompt import StaticPrompt
from cerebro.state import load_plugin_manifest, state_dir
from tests.test_plugin_claude_code import (
    FakeBrewPackageManager,
    FakeRunner,
    RecordingAuthHandoff,
)
from tests.test_recorder import FakeScheduler

_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "cerebro" / "plugins"
_CODING_DIR = _PLUGINS_DIR / "coding-standards"
_CLAUDE_DIR = _PLUGINS_DIR / "claude-code"
_VSCODE_DIR = _PLUGINS_DIR / "vscode"


# ---------------------------------------------------------------------------
# default selection bundle (real plugin's full set of question keys)
# ---------------------------------------------------------------------------


def _default_yes_no() -> dict[str, bool]:
    return {
        "Allow committing directly to the main branch for trivial changes?": False,
        "Should the agent ask before creating a new branch?": True,
        "Require descriptive commit messages (why, not just what)?": True,
        "Use conventional commit prefixes (feat:, fix:, chore:)?": False,
        "Require GPG-signed commits?": False,
        "Require all tests to pass locally before opening a PR?": True,
        "Use a structured PR description template (Summary + Test plan)?": True,
        "Practice test-driven development (write the test first)?": False,
    }


def _default_choice() -> dict[str, str]:
    return {
        "Branch naming scheme": "<initials>-<short-description>",
        "Minimum coverage target for new code": "80%",
    }


def _default_prompt() -> StaticPrompt:
    return StaticPrompt(yes_no=_default_yes_no(), choice=_default_choice())


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _coding_module() -> ModuleType:
    discovered = DiscoveredPlugin(
        manifest=load_plugin_manifest(_CODING_DIR),
        module_path=_CODING_DIR,
        source=IN_TREE_SOURCE,
    )
    return load_plugin_module(discovered)


def _coding_manifest() -> PluginManifest:
    return load_plugin_manifest(_CODING_DIR)


def _claude_manifest() -> PluginManifest:
    return load_plugin_manifest(_CLAUDE_DIR)


def _state_with_claude(vault: Path) -> CerebroState:
    return CerebroState(
        core_version="0.1.0",
        vault_path=vault,
        installed_plugins=[
            InstalledPlugin(
                name="vscode",
                source=IN_TREE_SOURCE,
                version="1.0.0",
                installed_at=datetime(2026, 4, 27, 11, 0, 0, tzinfo=UTC),
                enabled=True,
            ),
            InstalledPlugin(
                name="claude-code",
                source=IN_TREE_SOURCE,
                version="1.0.0",
                installed_at=datetime(2026, 4, 27, 11, 30, 0, tzinfo=UTC),
                enabled=True,
            ),
        ],
    )


def _make_ctx(
    *,
    state: CerebroState,
    prompt: StaticPrompt | None = None,
) -> HookContext:
    plugin = _coding_manifest()
    claude_manifest = _claude_manifest()
    vscode_manifest = load_plugin_manifest(_VSCODE_DIR)

    def manifest_lookup(name: str) -> PluginManifest | None:
        if name == "claude-code":
            return claude_manifest
        if name == "vscode":
            return vscode_manifest
        if name == "coding-standards":
            return plugin
        return None

    return build_context(
        plugin=plugin,
        state=state,
        package_manager=FakeBrewPackageManager(),
        scheduler=FakeScheduler(),
        when=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        prompt=prompt,
        manifest_lookup=manifest_lookup,
    )


def _redirect_claude_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Make ``~/.claude/...`` resolve under ``tmp_path``.

    Returns the redirected ``~/.claude/CLAUDE.md`` path so tests can read
    and assert against it. The plugin's discovery loop re-imports
    ``cerebro/plugins/claude-code/plugin.py`` from disk and calls its
    real ``rules_file_path``, which expands ``~/.claude``; redirecting
    ``Path.expanduser`` is the only sound way to keep that off the
    user's real home directory.
    """
    real_expanduser = Path.expanduser

    def fake_expanduser(self: Path) -> Path:
        s = str(self)
        if s.startswith("~/.claude"):
            return tmp_path / "claude-home" / s[len("~/.claude/") :]
        return real_expanduser(self)

    monkeypatch.setattr(Path, "expanduser", fake_expanduser)
    return tmp_path / "claude-home" / "CLAUDE.md"


# ---------------------------------------------------------------------------
# install hook (unit)
# ---------------------------------------------------------------------------


def test_install_writes_selections_file_and_block_into_each_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    rules_path = _redirect_claude_home(monkeypatch, tmp_path)
    module = _coding_module()
    state = _state_with_claude(tmp_path / "vault")
    ctx = _make_ctx(state=state, prompt=_default_prompt())

    module.install(ctx)

    # Selections persisted to the plugin-owned state directory.
    selections_path = (
        state_dir() / "plugins" / "coding-standards" / "selections.yaml"
    )
    assert selections_path.exists()
    persisted = yaml.safe_load(selections_path.read_text(encoding="utf-8"))
    assert persisted["branching"]["work_on_main_allowed"] is False
    assert persisted["branching"]["ask_before_creating"] is True
    assert (
        persisted["branching"]["naming_scheme"]
        == "<initials>-<short-description>"
    )
    assert persisted["commits"]["descriptive_messages"] is True
    assert persisted["commits"]["conventional"] is False
    assert persisted["commits"]["signed"] is False
    assert persisted["prs"]["tests_must_pass"] is True
    assert persisted["prs"]["description_template"] is True
    assert persisted["testing"]["tdd"] is False
    assert persisted["testing"]["min_coverage"] == "80%"

    # Block landed in claude-code's rules file.
    assert rules_path.exists()
    body = rules_path.read_text(encoding="utf-8")
    assert ">>> cerebro:plugin:coding-standards >>>" in body
    assert "Coding Standards" in body
    assert "Never commit directly to the main branch" in body
    assert "Ask before creating a new branch" in body
    assert "<initials>-<short-description>" in body
    assert "TDD is not required" in body

    # Recorded ops: write_file (selections.yaml) + add_block (per agent).
    kinds = [op.kind for op in ctx.manifest.operations]
    assert kinds == ["write_file", "add_block"]
    block_op = ctx.manifest.operations[1]
    assert block_op.parameters["plugin_name"] == "coding-standards"
    assert block_op.parameters["path"] == str(rules_path)


def test_install_template_picks_up_alternate_selections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Toggling each yes/no flips the rendered prose to the matching branch."""
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    rules_path = _redirect_claude_home(monkeypatch, tmp_path)
    module = _coding_module()
    state = _state_with_claude(tmp_path / "vault")

    yes_no = _default_yes_no()
    yes_no["Allow committing directly to the main branch for trivial changes?"] = True
    yes_no["Should the agent ask before creating a new branch?"] = False
    yes_no["Use conventional commit prefixes (feat:, fix:, chore:)?"] = True
    yes_no["Require GPG-signed commits?"] = True
    yes_no["Practice test-driven development (write the test first)?"] = True
    yes_no["Require all tests to pass locally before opening a PR?"] = False
    yes_no["Use a structured PR description template (Summary + Test plan)?"] = False
    choice = _default_choice()
    choice["Branch naming scheme"] = "<ticket>-<short-description>"
    choice["Minimum coverage target for new code"] = "90%"
    prompt = StaticPrompt(yes_no=yes_no, choice=choice)

    ctx = _make_ctx(state=state, prompt=prompt)
    module.install(ctx)

    body = rules_path.read_text(encoding="utf-8")
    assert "Working directly on the main branch is allowed" in body
    assert "Create branches as needed without asking" in body
    assert "<ticket>-<short-description>" in body
    assert "Use conventional commit prefixes" in body
    assert "All commits must be GPG-signed" in body
    assert "Practice test-driven development" in body
    assert "CI is the\n  source of truth" in body
    assert "PR description format is freeform" in body
    assert "Minimum coverage expectation for new code: 90%" in body


def test_install_with_no_agent_plugins_records_only_selections_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    _redirect_claude_home(monkeypatch, tmp_path)
    module = _coding_module()
    state = CerebroState(
        core_version="0.1.0",
        vault_path=tmp_path / "vault",
        installed_plugins=[],
    )
    ctx = _make_ctx(state=state, prompt=_default_prompt())

    module.install(ctx)

    kinds = [op.kind for op in ctx.manifest.operations]
    assert kinds == ["write_file"]


def test_install_skips_agent_plugin_without_rules_file_path_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent plugin missing the published surface is logged-and-skipped, not fatal."""
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    module = _coding_module()

    # Build a synthetic agent fixture on disk so discover_plugins finds it.
    agents_root = tmp_path / "in-tree"
    fake_dir = agents_root / "fake-agent"
    fake_dir.mkdir(parents=True)
    (fake_dir / "plugin.yaml").write_text(
        "name: fake-agent\n"
        "version: 1.0.0\n"
        "type: agent\n"
        "description: synthetic agent without a rules_file_path\n"
        "min_core_version: 0.1.0\n"
        "supported_platforms: [macos]\n"
        "hooks_declared: [install]\n",
        encoding="utf-8",
    )
    (fake_dir / "plugin.py").write_text(
        "def install(ctx):\n    pass\n",
        encoding="utf-8",
    )

    import cerebro.plugins.loader as loader_mod

    monkeypatch.setattr(loader_mod, "default_in_tree_root", lambda: agents_root)

    state = CerebroState(
        core_version="0.1.0",
        vault_path=tmp_path / "vault",
        installed_plugins=[
            InstalledPlugin(
                name="fake-agent",
                source=IN_TREE_SOURCE,
                version="1.0.0",
                installed_at=datetime(2026, 4, 27, 11, 0, 0, tzinfo=UTC),
                enabled=True,
            ),
        ],
    )
    plugin = _coding_manifest()
    fake_manifest = PluginManifest(
        name="fake-agent",
        version="1.0.0",
        type="agent",
        description="synthetic agent without a rules_file_path",
        min_core_version="0.1.0",
        supported_platforms=["macos"],
    )

    def manifest_lookup(name: str) -> PluginManifest | None:
        if name == "fake-agent":
            return fake_manifest
        if name == "coding-standards":
            return plugin
        return None

    ctx = build_context(
        plugin=plugin,
        state=state,
        package_manager=FakeBrewPackageManager(),
        scheduler=FakeScheduler(),
        when=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        prompt=_default_prompt(),
        manifest_lookup=manifest_lookup,
    )

    module.install(ctx)

    # Only the selections file got written; no block was added.
    kinds = [op.kind for op in ctx.manifest.operations]
    assert kinds == ["write_file"]


# ---------------------------------------------------------------------------
# configure hook
# ---------------------------------------------------------------------------


def test_configure_writes_block_into_every_installed_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Engine fires configure when a new agent is installed; we re-render."""
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    rules_path = _redirect_claude_home(monkeypatch, tmp_path)
    module = _coding_module()
    state = _state_with_claude(tmp_path / "vault")
    ctx = _make_ctx(state=state, prompt=_default_prompt())

    # Pretend install already ran: persist a selections file directly.
    selections_path = (
        state_dir() / "plugins" / "coding-standards" / "selections.yaml"
    )
    selections_path.parent.mkdir(parents=True, exist_ok=True)
    selections_path.write_text(
        yaml.safe_dump(
            {
                "branching": {
                    "work_on_main_allowed": False,
                    "ask_before_creating": True,
                    "naming_scheme": "<initials>-<short-description>",
                },
                "commits": {
                    "descriptive_messages": True,
                    "conventional": False,
                    "signed": False,
                },
                "prs": {
                    "tests_must_pass": True,
                    "description_template": True,
                },
                "testing": {"tdd": False, "min_coverage": "80%"},
                "style": {},
            }
        ),
        encoding="utf-8",
    )

    module.configure(ctx)

    assert rules_path.exists()
    body = rules_path.read_text(encoding="utf-8")
    assert ">>> cerebro:plugin:coding-standards >>>" in body
    # configure recorded only the add_block (selections file already on disk).
    assert [op.kind for op in ctx.manifest.operations] == ["add_block"]


def test_configure_raises_when_selections_file_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    _redirect_claude_home(monkeypatch, tmp_path)
    module = _coding_module()
    state = _state_with_claude(tmp_path / "vault")
    ctx = _make_ctx(state=state, prompt=_default_prompt())

    with pytest.raises(RuntimeError, match="selections file missing"):
        module.configure(ctx)


# ---------------------------------------------------------------------------
# verify hook
# ---------------------------------------------------------------------------


def test_verify_succeeds_when_block_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    _redirect_claude_home(monkeypatch, tmp_path)
    module = _coding_module()
    state = _state_with_claude(tmp_path / "vault")
    ctx = _make_ctx(state=state, prompt=_default_prompt())

    module.install(ctx)
    module.verify(ctx)


def test_verify_raises_when_block_modified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    rules_path = _redirect_claude_home(monkeypatch, tmp_path)
    module = _coding_module()
    state = _state_with_claude(tmp_path / "vault")
    ctx = _make_ctx(state=state, prompt=_default_prompt())

    module.install(ctx)

    rules_path.write_text(
        "<!-- >>> cerebro:plugin:coding-standards >>> -->\n"
        "user-edited content\n"
        "<!-- <<< cerebro:plugin:coding-standards <<< -->\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="modified outside Cerebro"):
        module.verify(ctx)


def test_verify_raises_when_block_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CEREBRO_HOME", str(tmp_path / "home"))
    rules_path = _redirect_claude_home(monkeypatch, tmp_path)
    module = _coding_module()
    state = _state_with_claude(tmp_path / "vault")
    ctx = _make_ctx(state=state, prompt=_default_prompt())

    module.install(ctx)

    rules_path.write_text("# nothing here\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="block missing"):
        module.verify(ctx)


# ---------------------------------------------------------------------------
# Engine integration: full install / configure / uninstall lifecycle
# ---------------------------------------------------------------------------


def _materialize_in_tree(tmp_path: Path) -> Path:
    """Copy the in-tree plugins (vscode, claude-code, coding-standards) into ``tmp_path``."""
    root = tmp_path / "in-tree"
    for src in (_VSCODE_DIR, _CLAUDE_DIR, _CODING_DIR):
        target = root / src.name
        target.mkdir(parents=True)
        for entry in src.rglob("*"):
            if entry.is_file():
                rel = entry.relative_to(src)
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(entry.read_bytes())
    return root


def _patch_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    import cerebro.runtime.platform as platform_mod

    monkeypatch.setattr(platform_mod, "current_platform", lambda: Platform.MACOS)


def _patch_subprocess_run(
    monkeypatch: pytest.MonkeyPatch, fake_runner: FakeRunner
) -> None:
    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_runner)


def _patch_engine_subprocess(
    monkeypatch: pytest.MonkeyPatch, fake_runner: FakeRunner
) -> None:
    import cerebro.runtime.engine as engine_mod

    monkeypatch.setattr(engine_mod.subprocess, "run", fake_runner)


def _patch_obsidian_runner(
    monkeypatch: pytest.MonkeyPatch, fake_runner: FakeRunner
) -> None:
    import cerebro.runtime.obsidian as obsidian_mod

    monkeypatch.setattr(obsidian_mod, "_default_runner", fake_runner)


def _patch_default_in_tree(monkeypatch: pytest.MonkeyPatch, in_tree: Path) -> None:
    import cerebro.plugins.loader as loader_mod

    monkeypatch.setattr(loader_mod, "default_in_tree_root", lambda: in_tree)


def _patch_which(
    monkeypatch: pytest.MonkeyPatch, *, claude: bool, code: bool = True
) -> None:
    def fake_which(name: str) -> str | None:
        if name == "claude":
            return "/fake/bin/claude" if claude else None
        if name == "code":
            return "/fake/bin/code" if code else None
        return None

    monkeypatch.setattr(shutil, "which", fake_which)


def test_engine_install_then_uninstall_full_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))

    _patch_platform(monkeypatch)
    fake_runner = FakeRunner()
    _patch_subprocess_run(monkeypatch, fake_runner)
    _patch_engine_subprocess(monkeypatch, fake_runner)
    _patch_obsidian_runner(monkeypatch, fake_runner)
    _patch_which(monkeypatch, claude=False, code=True)

    in_tree = _materialize_in_tree(tmp_path)
    _patch_default_in_tree(monkeypatch, in_tree)
    rules_path = _redirect_claude_home(monkeypatch, tmp_path)

    vault = tmp_path / "ObsidianVault"

    pm = FakeBrewPackageManager()
    components = PlatformComponents(
        platform=Platform.MACOS,
        package_manager=pm,
        scheduler=FakeScheduler(),
    )
    auth = RecordingAuthHandoff()
    prompt = _default_prompt()

    from cerebro.state import save_state
    save_state(
        CerebroState(core_version="0.1.0", vault_path=vault),
        home / "state.yaml",
    )

    engine_install(
        "vscode",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
        prompt=prompt,
    )
    engine_install(
        "claude-code",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
        prompt=prompt,
    )
    engine_install(
        "coding-standards",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
        prompt=prompt,
    )

    body = rules_path.read_text(encoding="utf-8")
    assert ">>> cerebro:plugin:claude-code >>>" in body
    assert ">>> cerebro:plugin:coding-standards >>>" in body
    assert "Coding Standards" in body

    selections_path = home / "plugins" / "coding-standards" / "selections.yaml"
    assert selections_path.exists()

    engine_uninstall(
        "coding-standards",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
        prompt=prompt,
    )

    body_after = rules_path.read_text(encoding="utf-8")
    # Our block is gone.
    assert ">>> cerebro:plugin:coding-standards >>>" not in body_after
    # The Claude Code block (sibling) is untouched.
    assert ">>> cerebro:plugin:claude-code >>>" in body_after
    # Selections file was removed by the recorder's inverse pass.
    assert not selections_path.exists()


def test_engine_install_when_already_installed_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))

    _patch_platform(monkeypatch)
    fake_runner = FakeRunner()
    _patch_subprocess_run(monkeypatch, fake_runner)
    _patch_engine_subprocess(monkeypatch, fake_runner)
    _patch_obsidian_runner(monkeypatch, fake_runner)
    _patch_which(monkeypatch, claude=False, code=True)

    in_tree = _materialize_in_tree(tmp_path)
    _patch_default_in_tree(monkeypatch, in_tree)
    _redirect_claude_home(monkeypatch, tmp_path)

    vault = tmp_path / "ObsidianVault"
    pm = FakeBrewPackageManager()
    components = PlatformComponents(
        platform=Platform.MACOS,
        package_manager=pm,
        scheduler=FakeScheduler(),
    )
    auth = RecordingAuthHandoff()
    prompt = _default_prompt()

    from cerebro.state import save_state
    save_state(
        CerebroState(core_version="0.1.0", vault_path=vault),
        home / "state.yaml",
    )

    engine_install(
        "vscode",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
        prompt=prompt,
    )
    engine_install(
        "claude-code",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
        prompt=prompt,
    )
    engine_install(
        "coding-standards",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
        prompt=prompt,
    )

    with pytest.raises(PluginAlreadyInstalledError):
        engine_install(
            "coding-standards",
            home=home,
            in_tree_root=in_tree,
            taps_root=tmp_path / "taps",
            components=components,
            auth=auth,
            prompt=prompt,
        )


def test_engine_configure_runs_when_second_agent_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding a synthetic second agent triggers coding-standards' configure
    pass, which adds the managed block to the new agent's rules file."""
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))

    _patch_platform(monkeypatch)
    fake_runner = FakeRunner()
    _patch_subprocess_run(monkeypatch, fake_runner)
    _patch_engine_subprocess(monkeypatch, fake_runner)
    _patch_obsidian_runner(monkeypatch, fake_runner)
    _patch_which(monkeypatch, claude=False, code=True)

    in_tree = _materialize_in_tree(tmp_path)
    _patch_default_in_tree(monkeypatch, in_tree)
    _redirect_claude_home(monkeypatch, tmp_path)

    vault = tmp_path / "ObsidianVault"
    pm = FakeBrewPackageManager()
    components = PlatformComponents(
        platform=Platform.MACOS,
        package_manager=pm,
        scheduler=FakeScheduler(),
    )
    auth = RecordingAuthHandoff()
    prompt = _default_prompt()

    from cerebro.state import save_state
    save_state(
        CerebroState(core_version="0.1.0", vault_path=vault),
        home / "state.yaml",
    )

    engine_install(
        "vscode",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
        prompt=prompt,
    )
    engine_install(
        "claude-code",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
        prompt=prompt,
    )
    engine_install(
        "coding-standards",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
        prompt=prompt,
    )

    # Drop a second synthetic agent into the in-tree root. It depends on
    # vscode (so the resolver is happy) and publishes a rules_file_path
    # pointing under tmp_path so we can check the block lands there.
    second_rules = tmp_path / "fake-agent-rules.md"
    fake_dir = in_tree / "fake-agent"
    fake_dir.mkdir()
    (fake_dir / "plugin.yaml").write_text(
        "name: fake-agent\n"
        "version: 1.0.0\n"
        "type: agent\n"
        "description: synthetic second agent\n"
        "dependencies:\n  vscode: \">=1.0.0\"\n"
        "min_core_version: 0.1.0\n"
        "supported_platforms: [macos]\n"
        "hooks_declared: [install]\n",
        encoding="utf-8",
    )
    (fake_dir / "plugin.py").write_text(
        "from pathlib import Path\n"
        f"_RULES = Path({str(second_rules)!r})\n"
        "def install(ctx):\n    pass\n"
        "def rules_file_path(ctx):\n    return _RULES\n",
        encoding="utf-8",
    )

    engine_install(
        "fake-agent",
        home=home,
        in_tree_root=in_tree,
        taps_root=tmp_path / "taps",
        components=components,
        auth=auth,
        prompt=prompt,
    )

    # coding-standards' configure should have written its block to the
    # new agent's rules file.
    assert second_rules.exists()
    body = second_rules.read_text(encoding="utf-8")
    assert ">>> cerebro:plugin:coding-standards >>>" in body
    assert "Coding Standards" in body
