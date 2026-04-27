"""End-to-end tests for the install/uninstall engine.

These tests exercise the engine through real plugin discovery + module
loading, with synthetic IDE and agent plugins under
``tests/fixtures/plugins/``. The package manager and scheduler are
faked (no real brew/apt/launchd calls), but everything above that line
is the production code path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cerebro.runtime.engine import (
    DependencyInUseError,
    ReconfigureError,
    TransactionRollbackError,
    install,
    uninstall,
)
from cerebro.runtime.platform import Platform, PlatformComponents
from cerebro.state import load_state
from tests.test_recorder import FakePackageManager, FakeScheduler

_FIXTURES = Path(__file__).parent / "fixtures" / "plugins"


@pytest.fixture
def fixture_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh per-test directory the synthetic plugins write into."""
    target = tmp_path / "fixture-output"
    target.mkdir()
    monkeypatch.setenv("CEREBRO_TEST_FIXTURE_DIR", str(target))
    return target


@pytest.fixture
def components() -> PlatformComponents:
    return PlatformComponents(
        platform=Platform.MACOS,
        package_manager=FakePackageManager(),
        scheduler=FakeScheduler(),
    )


def _plugins_root(only: list[str] | None = None, tmp_path: Path | None = None) -> Path:
    """Return the fixtures plugins root, optionally a filtered copy."""
    if only is None:
        return _FIXTURES
    assert tmp_path is not None
    target = tmp_path / "plugins-filtered"
    target.mkdir()
    for name in only:
        src = _FIXTURES / name
        dst = target / name
        dst.mkdir()
        for child in src.iterdir():
            if child.is_file():
                dst.joinpath(child.name).write_bytes(child.read_bytes())
    return target


def _now() -> datetime:
    return datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


def _state_names(home: Path) -> list[str]:
    state_path = home / "state.yaml"
    if not state_path.exists():
        return []
    return sorted(p.name for p in load_state(state_path).installed_plugins)


def test_install_agent_without_ide_available_fails(
    tmp_path: Path,
    fixture_dir: Path,
    components: PlatformComponents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))
    plugins_root = _plugins_root(only=["demo-agent"], tmp_path=tmp_path)

    with pytest.raises(Exception) as excinfo:
        install(
            "demo-agent",
            home=home,
            in_tree_root=plugins_root,
            taps_root=tmp_path / "taps",
            components=components,
            now=_now(),
        )
    # Either the resolver raises directly or the engine wraps it; in
    # both cases the message names the missing dep.
    assert "demo-ide" in str(excinfo.value)
    assert _state_names(home) == []


def test_install_ide_then_agent_succeeds(
    tmp_path: Path,
    fixture_dir: Path,
    components: PlatformComponents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))
    plugins_root = _plugins_root()

    install(
        "demo-ide",
        home=home,
        in_tree_root=plugins_root,
        taps_root=tmp_path / "taps",
        components=components,
        now=_now(),
    )
    assert _state_names(home) == ["demo-ide"]
    assert (fixture_dir / "demo-ide" / "marker.txt").exists()

    install(
        "demo-agent",
        home=home,
        in_tree_root=plugins_root,
        taps_root=tmp_path / "taps",
        components=components,
        now=_now(),
    )
    assert _state_names(home) == ["demo-agent", "demo-ide"]
    bindings = (fixture_dir / "demo-agent" / "ide-bindings.md").read_text()
    assert "- demo-ide" in bindings


def test_per_plugin_rollback_when_only_failing_plugin_in_transaction(
    tmp_path: Path,
    fixture_dir: Path,
    components: PlatformComponents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))
    plugins_root = _plugins_root()

    install(
        "demo-ide",
        home=home,
        in_tree_root=plugins_root,
        taps_root=tmp_path / "taps",
        components=components,
        now=_now(),
    )
    assert _state_names(home) == ["demo-ide"]

    monkeypatch.setenv("CEREBRO_TEST_FAIL_INSTALL", "demo-agent")
    with pytest.raises(TransactionRollbackError) as excinfo:
        install(
            "demo-agent",
            home=home,
            in_tree_root=plugins_root,
            taps_root=tmp_path / "taps",
            components=components,
            now=_now(),
        )
    assert excinfo.value.failed_plugin == "demo-agent"
    # demo-ide was already installed before this transaction; nothing in
    # the transaction succeeded, so nothing to roll back across plugins.
    assert excinfo.value.rolled_back == []
    assert _state_names(home) == ["demo-ide"]
    assert (fixture_dir / "demo-ide" / "marker.txt").exists()
    # Per-plugin rollback wiped demo-agent's partial output.
    assert not (fixture_dir / "demo-agent" / "marker.txt").exists()
    assert not (fixture_dir / "demo-agent").exists() or list(
        (fixture_dir / "demo-agent").iterdir()
    ) == []


def test_cross_plugin_rollback_when_dep_succeeds_but_target_fails(
    tmp_path: Path,
    fixture_dir: Path,
    components: PlatformComponents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))
    plugins_root = _plugins_root()

    monkeypatch.setenv("CEREBRO_TEST_FAIL_INSTALL", "demo-agent")
    with pytest.raises(TransactionRollbackError) as excinfo:
        install(
            "demo-agent",
            home=home,
            in_tree_root=plugins_root,
            taps_root=tmp_path / "taps",
            components=components,
            now=_now(),
        )

    # demo-ide installed first, then demo-agent failed. The engine must
    # have rolled demo-ide back too: state empty, marker gone.
    assert excinfo.value.failed_plugin == "demo-agent"
    assert "demo-ide" in excinfo.value.rolled_back
    assert _state_names(home) == []
    assert not (fixture_dir / "demo-ide" / "marker.txt").exists()
    assert not (home / "manifests" / "demo-ide" / "install.yaml").exists()
    assert not (home / "manifests" / "demo-agent" / "install.yaml").exists()


def test_uninstall_dependency_with_dependent_present_fails(
    tmp_path: Path,
    fixture_dir: Path,
    components: PlatformComponents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))
    plugins_root = _plugins_root()

    install(
        "demo-ide",
        home=home,
        in_tree_root=plugins_root,
        taps_root=tmp_path / "taps",
        components=components,
        now=_now(),
    )
    install(
        "demo-agent",
        home=home,
        in_tree_root=plugins_root,
        taps_root=tmp_path / "taps",
        components=components,
        now=_now(),
    )

    with pytest.raises(DependencyInUseError) as excinfo:
        uninstall(
            "demo-ide",
            home=home,
            in_tree_root=plugins_root,
            taps_root=tmp_path / "taps",
            components=components,
            now=_now(),
        )
    assert excinfo.value.dependents == ["demo-agent"]
    assert _state_names(home) == ["demo-agent", "demo-ide"]


def test_uninstall_in_correct_order_leaves_disk_and_state_clean(
    tmp_path: Path,
    fixture_dir: Path,
    components: PlatformComponents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))
    plugins_root = _plugins_root()

    install(
        "demo-ide",
        home=home,
        in_tree_root=plugins_root,
        taps_root=tmp_path / "taps",
        components=components,
        now=_now(),
    )
    install(
        "demo-agent",
        home=home,
        in_tree_root=plugins_root,
        taps_root=tmp_path / "taps",
        components=components,
        now=_now(),
    )

    uninstall(
        "demo-agent",
        home=home,
        in_tree_root=plugins_root,
        taps_root=tmp_path / "taps",
        components=components,
        now=_now(),
    )
    assert _state_names(home) == ["demo-ide"]
    assert not (fixture_dir / "demo-agent" / "ide-bindings.md").exists()
    assert not (home / "manifests" / "demo-agent" / "install.yaml").exists()

    uninstall(
        "demo-ide",
        home=home,
        in_tree_root=plugins_root,
        taps_root=tmp_path / "taps",
        components=components,
        now=_now(),
    )
    assert _state_names(home) == []
    assert not (fixture_dir / "demo-ide" / "marker.txt").exists()
    assert not (home / "manifests" / "demo-ide" / "install.yaml").exists()


def test_installing_second_ide_triggers_agent_reconfigure(
    tmp_path: Path,
    fixture_dir: Path,
    components: PlatformComponents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))
    plugins_root = _plugins_root()

    install(
        "demo-ide",
        home=home,
        in_tree_root=plugins_root,
        taps_root=tmp_path / "taps",
        components=components,
        now=_now(),
    )
    install(
        "demo-agent",
        home=home,
        in_tree_root=plugins_root,
        taps_root=tmp_path / "taps",
        components=components,
        now=_now(),
    )

    bindings_path = fixture_dir / "demo-agent" / "ide-bindings.md"
    initial = bindings_path.read_text()
    assert "- demo-ide" in initial
    assert "- other-ide" not in initial

    install(
        "other-ide",
        home=home,
        in_tree_root=plugins_root,
        taps_root=tmp_path / "taps",
        components=components,
        now=_now(),
    )

    refreshed = bindings_path.read_text()
    assert "- demo-ide" in refreshed
    assert "- other-ide" in refreshed
    assert _state_names(home) == ["demo-agent", "demo-ide", "other-ide"]


def test_reconfigure_failure_leaves_install_in_place_but_raises(
    tmp_path: Path,
    fixture_dir: Path,
    components: PlatformComponents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chosen policy: reconfigure failure does NOT roll back the install.

    See module docstring on ``cerebro.runtime.engine``. The just-completed
    install stays in place, the failed reconfigure's recorder rolls its
    own changes back, and ``ReconfigureError`` surfaces to the caller.
    """
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))
    plugins_root = _plugins_root()

    install(
        "demo-ide",
        home=home,
        in_tree_root=plugins_root,
        taps_root=tmp_path / "taps",
        components=components,
        now=_now(),
    )
    install(
        "demo-agent",
        home=home,
        in_tree_root=plugins_root,
        taps_root=tmp_path / "taps",
        components=components,
        now=_now(),
    )

    monkeypatch.setenv("CEREBRO_TEST_FAIL_CONFIGURE", "demo-agent")
    with pytest.raises(ReconfigureError) as excinfo:
        install(
            "other-ide",
            home=home,
            in_tree_root=plugins_root,
            taps_root=tmp_path / "taps",
            components=components,
            now=_now(),
        )
    assert "demo-agent" in excinfo.value.failures

    # other-ide remains installed (chosen policy).
    assert "other-ide" in _state_names(home)
    assert (fixture_dir / "other-ide" / "marker.txt").exists()
    # demo-agent's recorder rolled back its partial configure output.
    assert not (fixture_dir / "demo-agent" / "configure-attempted.txt").exists()


def test_install_writes_jsonl_log_under_logs_dir(
    tmp_path: Path,
    fixture_dir: Path,
    components: PlatformComponents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "cerebro-home"
    home.mkdir()
    monkeypatch.setenv("CEREBRO_HOME", str(home))
    plugins_root = _plugins_root()

    install(
        "demo-ide",
        home=home,
        in_tree_root=plugins_root,
        taps_root=tmp_path / "taps",
        components=components,
        now=_now(),
    )

    logs_dir = home / "logs"
    files = list(logs_dir.glob("*-install.log"))
    assert len(files) == 1
    lines = [
        line for line in files[0].read_text().splitlines() if line.strip()
    ]
    # Every line must be valid JSON with an "event" field.
    import json

    events = [json.loads(line) for line in lines]
    kinds = {e["event"] for e in events}
    assert "install.begin" in kinds
    assert "plugin.install.commit" in kinds
    assert "install.complete" in kinds
