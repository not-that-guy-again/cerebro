"""Tests for ``cerebro.runtime.doctor`` and the ``cerebro doctor`` CLI.

The unit tests drive ``check_plugin`` / ``run_doctor`` directly against
hand-built install manifests; the CLI tests drive a full install of the
synthetic ``demo-full`` plugin (which uses every recorded operation
kind) through the engine, then deliberately corrupt disk state.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from cerebro.cli import cli
from cerebro.cli._errors import EXIT_ERROR, EXIT_OK
from cerebro.models import (
    CerebroState,
    InstalledPlugin,
    InstallManifest,
    Operation,
)
from cerebro.runtime.blocks import get_format, upsert_block
from cerebro.runtime.doctor import (
    OperationStatus,
    accept_plugin,
    check_plugin,
    repair_plugin,
    run_doctor,
)
from cerebro.runtime.platform import Platform, PlatformComponents
from cerebro.state import (
    load_install_manifest,
    save_install_manifest,
    save_state,
)
from tests.test_recorder import FakePackageManager, FakeScheduler

_FIXTURES = Path(__file__).parent / "fixtures" / "plugins"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_components() -> PlatformComponents:
    return PlatformComponents(
        platform=Platform.MACOS,
        package_manager=FakePackageManager(),
        scheduler=FakeScheduler(),
    )


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
def runner() -> CliRunner:
    return CliRunner()


def _seed_state(home: Path, plugin: InstalledPlugin) -> None:
    state = CerebroState(
        core_version="0.0.0",
        vault_path=home / "vault",
        installed_plugins=[plugin],
    )
    save_state(state, home / "state.yaml")


def _seed_manifest(home: Path, manifest: InstallManifest) -> Path:
    path = home / "manifests" / manifest.plugin_name / "install.yaml"
    save_install_manifest(manifest, path)
    return path


def _make_record(name: str = "demo", version: str = "1.0.0") -> InstalledPlugin:
    return InstalledPlugin(
        name=name,
        source="in-tree",
        version=version,
        installed_at=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
        enabled=True,
    )


# ---------------------------------------------------------------------------
# Unit tests: per-op verification
# ---------------------------------------------------------------------------


def test_clean_plugin_reports_no_drift(
    cerebro_home: Path, fake_components: PlatformComponents, tmp_path: Path
) -> None:
    target = tmp_path / "marker.txt"
    target.write_text("hello", encoding="utf-8")

    record = _make_record()
    _seed_state(cerebro_home, record)
    _seed_manifest(
        cerebro_home,
        InstallManifest(
            plugin_name="demo",
            version="1.0.0",
            operations=[
                Operation(
                    kind="write_file",
                    parameters={"path": str(target), "content": "hello"},
                    inverse={"path": str(target), "previous_content": None, "created_dirs": []},
                )
            ],
            installed_at=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
        ),
    )
    report = check_plugin(record, home=cerebro_home, components=fake_components)
    assert report.is_drifted is False
    assert [c.status for c in report.checks] == [OperationStatus.OK]


def test_write_file_missing_is_reported(
    cerebro_home: Path, fake_components: PlatformComponents, tmp_path: Path
) -> None:
    target = tmp_path / "marker.txt"
    record = _make_record()
    _seed_state(cerebro_home, record)
    _seed_manifest(
        cerebro_home,
        InstallManifest(
            plugin_name="demo",
            version="1.0.0",
            operations=[
                Operation(
                    kind="write_file",
                    parameters={"path": str(target), "content": "hello"},
                    inverse={"path": str(target), "previous_content": None, "created_dirs": []},
                )
            ],
            installed_at=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
        ),
    )
    report = check_plugin(record, home=cerebro_home, components=fake_components)
    assert report.is_drifted is True
    assert report.checks[0].status is OperationStatus.MISSING


def test_write_file_drifted_content_is_reported(
    cerebro_home: Path, fake_components: PlatformComponents, tmp_path: Path
) -> None:
    target = tmp_path / "marker.txt"
    target.write_text("tampered", encoding="utf-8")
    record = _make_record()
    _seed_state(cerebro_home, record)
    _seed_manifest(
        cerebro_home,
        InstallManifest(
            plugin_name="demo",
            version="1.0.0",
            operations=[
                Operation(
                    kind="write_file",
                    parameters={"path": str(target), "content": "hello"},
                    inverse={"path": str(target), "previous_content": None, "created_dirs": []},
                )
            ],
            installed_at=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
        ),
    )
    report = check_plugin(record, home=cerebro_home, components=fake_components)
    assert report.checks[0].status is OperationStatus.DRIFTED


def test_add_block_drift_is_reported(
    cerebro_home: Path, fake_components: PlatformComponents, tmp_path: Path
) -> None:
    syntax = get_format("markdown")
    target = tmp_path / "rules.md"
    target.write_text(upsert_block("", "demo", syntax, "manually edited"), encoding="utf-8")

    record = _make_record()
    _seed_state(cerebro_home, record)
    _seed_manifest(
        cerebro_home,
        InstallManifest(
            plugin_name="demo",
            version="1.0.0",
            operations=[
                Operation(
                    kind="add_block",
                    parameters={
                        "path": str(target),
                        "plugin_name": "demo",
                        "format": "markdown",
                        "content": "expected body",
                    },
                    inverse={
                        "path": str(target),
                        "previous_content": None,
                        "previous_block": None,
                        "created_dirs": [],
                    },
                )
            ],
            installed_at=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
        ),
    )
    report = check_plugin(record, home=cerebro_home, components=fake_components)
    assert report.checks[0].status is OperationStatus.DRIFTED
    assert "block content" in report.checks[0].detail


def test_add_block_missing_when_block_was_removed(
    cerebro_home: Path, fake_components: PlatformComponents, tmp_path: Path
) -> None:
    target = tmp_path / "rules.md"
    target.write_text("just user content\n", encoding="utf-8")

    record = _make_record()
    _seed_state(cerebro_home, record)
    _seed_manifest(
        cerebro_home,
        InstallManifest(
            plugin_name="demo",
            version="1.0.0",
            operations=[
                Operation(
                    kind="add_block",
                    parameters={
                        "path": str(target),
                        "plugin_name": "demo",
                        "format": "markdown",
                        "content": "expected body",
                    },
                    inverse={
                        "path": str(target),
                        "previous_content": "just user content\n",
                        "previous_block": None,
                        "created_dirs": [],
                    },
                )
            ],
            installed_at=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
        ),
    )
    report = check_plugin(record, home=cerebro_home, components=fake_components)
    assert report.checks[0].status is OperationStatus.MISSING


def test_run_pkg_missing_is_reported(cerebro_home: Path, tmp_path: Path) -> None:
    pm = FakePackageManager()
    components = PlatformComponents(
        platform=Platform.MACOS, package_manager=pm, scheduler=FakeScheduler()
    )
    record = _make_record()
    _seed_state(cerebro_home, record)
    _seed_manifest(
        cerebro_home,
        InstallManifest(
            plugin_name="demo",
            version="1.0.0",
            operations=[
                Operation(
                    kind="run_pkg",
                    parameters={"action": "install", "package": "ripgrep"},
                    inverse={"action": "uninstall", "package": "ripgrep"},
                )
            ],
            installed_at=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
        ),
    )
    report = check_plugin(record, home=cerebro_home, components=components)
    assert report.checks[0].status is OperationStatus.MISSING

    # Once installed, the next check is clean.
    pm.installed.add("ripgrep")
    report = check_plugin(record, home=cerebro_home, components=components)
    assert report.checks[0].status is OperationStatus.OK


def test_register_task_missing_is_reported(cerebro_home: Path) -> None:
    sched = FakeScheduler()
    components = PlatformComponents(
        platform=Platform.MACOS, package_manager=FakePackageManager(), scheduler=sched
    )
    record = _make_record()
    _seed_state(cerebro_home, record)
    _seed_manifest(
        cerebro_home,
        InstallManifest(
            plugin_name="demo",
            version="1.0.0",
            operations=[
                Operation(
                    kind="register_task",
                    parameters={
                        "name": "demo-nightly",
                        "schedule": "daily@06:00",
                        "command": "demo run",
                    },
                    inverse={"name": "demo-nightly"},
                )
            ],
            installed_at=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
        ),
    )
    report = check_plugin(record, home=cerebro_home, components=components)
    assert report.checks[0].status is OperationStatus.MISSING

    sched.registered.add("demo-nightly")
    report = check_plugin(record, home=cerebro_home, components=components)
    assert report.checks[0].status is OperationStatus.OK


def test_run_doctor_walks_state(
    cerebro_home: Path, fake_components: PlatformComponents, tmp_path: Path
) -> None:
    target = tmp_path / "marker.txt"
    target.write_text("hello", encoding="utf-8")
    state = CerebroState(
        core_version="0.0.0",
        vault_path=cerebro_home / "vault",
        installed_plugins=[_make_record("demo-a"), _make_record("demo-b")],
    )
    save_state(state, cerebro_home / "state.yaml")
    for name in ("demo-a", "demo-b"):
        _seed_manifest(
            cerebro_home,
            InstallManifest(
                plugin_name=name,
                version="1.0.0",
                operations=[
                    Operation(
                        kind="write_file",
                        parameters={"path": str(target), "content": "hello"},
                        inverse={
                            "path": str(target),
                            "previous_content": None,
                            "created_dirs": [],
                        },
                    )
                ],
                installed_at=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
            ),
        )
    reports = run_doctor(home=cerebro_home, components=fake_components)
    assert [r.plugin_name for r in reports] == ["demo-a", "demo-b"]
    assert all(not r.is_drifted for r in reports)


def test_manifest_missing_is_reported(
    cerebro_home: Path, fake_components: PlatformComponents
) -> None:
    record = _make_record()
    _seed_state(cerebro_home, record)
    report = check_plugin(record, home=cerebro_home, components=fake_components)
    assert report.manifest_missing is True
    assert report.is_drifted is True


# ---------------------------------------------------------------------------
# Integration tests: full install through engine, then drift + repair/accept
# ---------------------------------------------------------------------------


@pytest.fixture
def installed_full_plugin(
    cerebro_home: Path,
    fixture_dir: Path,
    fake_components: PlatformComponents,
) -> Iterator[Path]:
    from cerebro.runtime.engine import install as engine_install

    engine_install(
        "demo-full",
        home=cerebro_home,
        in_tree_root=_FIXTURES,
        components=fake_components,
        now=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
    )
    yield fixture_dir / "demo-full"


def test_full_install_is_clean(
    cerebro_home: Path,
    fake_components: PlatformComponents,
    installed_full_plugin: Path,
) -> None:
    reports = run_doctor(home=cerebro_home, components=fake_components)
    assert len(reports) == 1
    assert not reports[0].is_drifted


def test_each_drift_kind_is_detected(
    cerebro_home: Path,
    fake_components: PlatformComponents,
    installed_full_plugin: Path,
) -> None:
    # Delete the marker file (write_file -> missing).
    (installed_full_plugin / "marker.txt").unlink()
    # Edit the managed block (add_block -> drifted).
    shared = installed_full_plugin / "shared.md"
    shared.write_text(
        shared.read_text().replace("owned by demo-full", "tampered"),
        encoding="utf-8",
    )
    # Remove the package (run_pkg -> missing).
    fake_components.package_manager.installed.discard("demo-pkg")
    # Remove the scheduled task (register_task -> missing).
    fake_components.scheduler.registered.discard("demo-full-nightly")

    reports = run_doctor(home=cerebro_home, components=fake_components)
    assert len(reports) == 1
    by_kind = {c.kind: c for c in reports[0].checks}
    assert by_kind["write_file"].status is OperationStatus.MISSING
    assert by_kind["add_block"].status is OperationStatus.DRIFTED
    assert by_kind["run_pkg"].status is OperationStatus.MISSING
    assert by_kind["register_task"].status is OperationStatus.MISSING


def test_repair_restores_disk(
    cerebro_home: Path,
    fake_components: PlatformComponents,
    installed_full_plugin: Path,
) -> None:
    (installed_full_plugin / "marker.txt").unlink()
    fake_components.package_manager.installed.discard("demo-pkg")

    repair_plugin(
        "demo-full",
        home=cerebro_home,
        in_tree_root=_FIXTURES,
        components=fake_components,
        now=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
    )

    assert (installed_full_plugin / "marker.txt").read_text() == "demo-full installed\n"
    assert "demo-pkg" in fake_components.package_manager.installed
    reports = run_doctor(home=cerebro_home, components=fake_components)
    assert not reports[0].is_drifted


def test_accept_rewrites_manifest_to_match_disk(
    cerebro_home: Path,
    fake_components: PlatformComponents,
    installed_full_plugin: Path,
) -> None:
    # Remove the package so it is reported missing, and tamper with the
    # block so it is reported drifted. After accept, both should be OK.
    fake_components.package_manager.installed.discard("demo-pkg")
    shared = installed_full_plugin / "shared.md"
    shared.write_text(
        shared.read_text().replace("owned by demo-full", "tampered"),
        encoding="utf-8",
    )

    follow_up = accept_plugin(
        "demo-full", home=cerebro_home, components=fake_components
    )
    assert follow_up.is_drifted is False

    # Re-running doctor confirms the plugin is now clean.
    reports = run_doctor(home=cerebro_home, components=fake_components)
    assert not reports[0].is_drifted

    # The accepted manifest no longer mentions the missing package, and
    # the block content reflects the tampered state.
    manifest = load_install_manifest(
        cerebro_home / "manifests" / "demo-full" / "install.yaml"
    )
    kinds = [op.kind for op in manifest.operations]
    assert "run_pkg" not in kinds
    block_op = next(op for op in manifest.operations if op.kind == "add_block")
    assert block_op.parameters["content"] == "tampered"


# ---------------------------------------------------------------------------
# CLI tests: interactive (mocked prompts) + non-interactive
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_doctor_components(
    monkeypatch: pytest.MonkeyPatch,
    fake_components: PlatformComponents,
) -> Iterator[None]:
    """Force the CLI's doctor entry points to use the fake components.

    The CLI calls ``run_doctor`` / ``repair_plugin`` / ``accept_plugin``
    without injecting components, so production plugin authors don't
    have to either; tests patch the imports inside the CLI module to
    redirect them at the fake.
    """
    import cerebro.cli as cli_module

    real_run = cli_module.run_doctor
    real_repair = cli_module.repair_plugin
    real_accept = cli_module.accept_plugin

    def run_with_components(**kwargs):
        kwargs.setdefault("components", fake_components)
        return real_run(**kwargs)

    def repair_with_components(name, **kwargs):
        kwargs.setdefault("in_tree_root", _FIXTURES)
        kwargs.setdefault("components", fake_components)
        return real_repair(name, **kwargs)

    def accept_with_components(name, **kwargs):
        kwargs.setdefault("components", fake_components)
        return real_accept(name, **kwargs)

    monkeypatch.setattr(cli_module, "run_doctor", run_with_components)
    monkeypatch.setattr(cli_module, "repair_plugin", repair_with_components)
    monkeypatch.setattr(cli_module, "accept_plugin", accept_with_components)
    yield


def _install_demo_full(cerebro_home: Path, fake_components: PlatformComponents) -> None:
    from cerebro.runtime.engine import install as engine_install

    engine_install(
        "demo-full",
        home=cerebro_home,
        in_tree_root=_FIXTURES,
        components=fake_components,
        now=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
    )


def test_cli_doctor_clean_table(
    runner: CliRunner,
    cerebro_home: Path,
    fixture_dir: Path,
    fake_components: PlatformComponents,
    patch_doctor_components: None,
) -> None:
    _install_demo_full(cerebro_home, fake_components)
    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == EXIT_OK, result.output
    assert "PLUGIN" in result.output
    assert "demo-full" in result.output
    assert "ok" in result.output


def test_cli_doctor_json(
    runner: CliRunner,
    cerebro_home: Path,
    fixture_dir: Path,
    fake_components: PlatformComponents,
    patch_doctor_components: None,
) -> None:
    _install_demo_full(cerebro_home, fake_components)
    fake_components.scheduler.registered.discard("demo-full-nightly")
    result = runner.invoke(cli, ["--json", "doctor"])
    assert result.exit_code == EXIT_OK, result.output
    payload = json.loads(result.output)
    assert payload["reports"][0]["drifted"] is True
    kinds = {c["kind"]: c for c in payload["reports"][0]["checks"]}
    assert kinds["register_task"]["status"] == "missing"


def test_cli_doctor_non_interactive_fail_exits_non_zero(
    runner: CliRunner,
    cerebro_home: Path,
    fixture_dir: Path,
    fake_components: PlatformComponents,
    patch_doctor_components: None,
) -> None:
    _install_demo_full(cerebro_home, fake_components)
    fake_components.scheduler.registered.discard("demo-full-nightly")
    result = runner.invoke(cli, ["doctor", "--non-interactive", "--action", "fail"])
    assert result.exit_code == EXIT_ERROR
    assert "drift detected" in result.stderr


def test_cli_doctor_non_interactive_repair(
    runner: CliRunner,
    cerebro_home: Path,
    fixture_dir: Path,
    fake_components: PlatformComponents,
    patch_doctor_components: None,
) -> None:
    _install_demo_full(cerebro_home, fake_components)
    target = fixture_dir / "demo-full" / "marker.txt"
    target.unlink()
    fake_components.scheduler.registered.discard("demo-full-nightly")

    result = runner.invoke(cli, ["doctor", "--non-interactive", "--action", "repair"])
    assert result.exit_code == EXIT_OK, result.stderr or result.output
    assert "repaired demo-full" in result.output
    assert target.exists()
    assert "demo-full-nightly" in fake_components.scheduler.registered


def test_cli_doctor_non_interactive_accept(
    runner: CliRunner,
    cerebro_home: Path,
    fixture_dir: Path,
    fake_components: PlatformComponents,
    patch_doctor_components: None,
) -> None:
    _install_demo_full(cerebro_home, fake_components)
    fake_components.scheduler.registered.discard("demo-full-nightly")

    result = runner.invoke(cli, ["doctor", "--non-interactive", "--action", "accept"])
    assert result.exit_code == EXIT_OK, result.stderr or result.output
    assert "accepted demo-full" in result.output
    # After accept the next doctor run is clean.
    follow_up = runner.invoke(cli, ["doctor"])
    assert "ok" in follow_up.output


def test_cli_doctor_interactive_repair_via_prompt(
    runner: CliRunner,
    cerebro_home: Path,
    fixture_dir: Path,
    fake_components: PlatformComponents,
    patch_doctor_components: None,
) -> None:
    _install_demo_full(cerebro_home, fake_components)
    target = fixture_dir / "demo-full" / "marker.txt"
    target.unlink()

    result = runner.invoke(cli, ["doctor"], input="repair\n")
    assert result.exit_code == EXIT_OK, result.stderr or result.output
    assert "repaired demo-full" in result.output
    assert target.exists()


def test_cli_doctor_interactive_accept_via_prompt(
    runner: CliRunner,
    cerebro_home: Path,
    fixture_dir: Path,
    fake_components: PlatformComponents,
    patch_doctor_components: None,
) -> None:
    _install_demo_full(cerebro_home, fake_components)
    fake_components.package_manager.installed.discard("demo-pkg")

    result = runner.invoke(cli, ["doctor"], input="accept\n")
    assert result.exit_code == EXIT_OK, result.stderr or result.output
    assert "accepted demo-full" in result.output


def test_cli_doctor_interactive_skip_via_prompt(
    runner: CliRunner,
    cerebro_home: Path,
    fixture_dir: Path,
    fake_components: PlatformComponents,
    patch_doctor_components: None,
) -> None:
    _install_demo_full(cerebro_home, fake_components)
    fake_components.scheduler.registered.discard("demo-full-nightly")

    result = runner.invoke(cli, ["doctor"], input="skip\n")
    assert result.exit_code == EXIT_OK, result.stderr or result.output
    assert "skipped demo-full" in result.output
    # State unchanged: the missing task is still missing on the next run.
    assert "demo-full-nightly" not in fake_components.scheduler.registered


def test_repair_failure_leaves_existing_manifest_intact(
    cerebro_home: Path,
    fixture_dir: Path,
    fake_components: PlatformComponents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing repair must not corrupt the install manifest."""
    _install_demo_full(cerebro_home, fake_components)
    manifest_path = cerebro_home / "manifests" / "demo-full" / "install.yaml"
    before = manifest_path.read_bytes()

    (fixture_dir / "demo-full" / "marker.txt").unlink()

    # Patch the engine's loader to swap in a hook that raises, so the
    # transactional repair must roll back without rewriting the manifest.
    from cerebro.plugins import loader as loader_module
    from cerebro.runtime import engine as engine_module

    original_load = loader_module.load_plugin_module

    def loader_with_failure(discovered):
        module = original_load(discovered)
        if discovered.manifest.name == "demo-full":
            def boom(_ctx):
                raise RuntimeError("forced repair failure")

            module.install = boom  # type: ignore[attr-defined]
        return module

    monkeypatch.setattr(engine_module, "load_plugin_module", loader_with_failure)

    from cerebro.runtime.engine import PluginInstallError

    with pytest.raises(PluginInstallError, match="forced repair failure"):
        repair_plugin(
            "demo-full",
            home=cerebro_home,
            in_tree_root=_FIXTURES,
            components=fake_components,
            now=datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC),
        )

    after = manifest_path.read_bytes()
    assert after == before, "failed repair must not rewrite the install manifest"
