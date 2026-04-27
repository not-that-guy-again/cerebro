from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cerebro.runtime.platform import InstallResult
from cerebro.runtime.recorder import (
    BlocksHelper,
    CommandHelper,
    FilesystemHelper,
    OperationRecorder,
    PackageManagerHelper,
    ScheduledTaskHelper,
)


class FakePackageManager:
    def __init__(self, already_present: set[str] | None = None) -> None:
        self.installed: set[str] = set(already_present or set())
        self.calls: list[tuple[str, str]] = []

    def is_installed(self, package: str) -> bool:
        return package in self.installed

    def install(self, package: str) -> InstallResult:
        already = package in self.installed
        self.installed.add(package)
        self.calls.append(("install", package))
        return InstallResult(already_installed=already)

    def uninstall(self, package: str) -> None:
        self.installed.discard(package)
        self.calls.append(("uninstall", package))


class FakeScheduler:
    def __init__(self, already_registered: set[str] | None = None) -> None:
        self.registered: set[str] = set(already_registered or set())
        self.calls: list[tuple[str, str]] = []

    def is_registered(self, name: str) -> bool:
        return name in self.registered

    def register(self, name: str, schedule: str, command: str) -> None:
        del schedule, command
        self.registered.add(name)
        self.calls.append(("register", name))

    def unregister(self, name: str) -> None:
        self.registered.discard(name)
        self.calls.append(("unregister", name))


def _make_recorder(
    *,
    package_manager: FakePackageManager | None = None,
    scheduler: FakeScheduler | None = None,
) -> tuple[OperationRecorder, FakePackageManager, FakeScheduler]:
    pm = package_manager or FakePackageManager()
    sched = scheduler or FakeScheduler()
    rec = OperationRecorder(
        plugin_name="demo",
        plugin_version="1.0.0",
        when=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
        package_manager=pm,
        scheduler=sched,
    )
    return rec, pm, sched


def test_write_file_creates_file_and_records_op(tmp_path: Path) -> None:
    rec, _, _ = _make_recorder()
    fs = FilesystemHelper(rec)
    target = tmp_path / "a.txt"
    fs.write_file(target, "hello")
    assert target.read_text() == "hello"
    ops = rec.operations
    assert len(ops) == 1
    assert ops[0].kind == "write_file"
    assert ops[0].inverse["previous_content"] is None


def test_write_file_rollback_removes_new_file(tmp_path: Path) -> None:
    rec, _, _ = _make_recorder()
    fs = FilesystemHelper(rec)
    target = tmp_path / "a.txt"
    fs.write_file(target, "hello")
    rec.rollback()
    assert not target.exists()


def test_write_file_rollback_restores_overwritten_file(tmp_path: Path) -> None:
    rec, _, _ = _make_recorder()
    fs = FilesystemHelper(rec)
    target = tmp_path / "a.txt"
    target.write_text("original")
    fs.write_file(target, "replaced")
    assert target.read_text() == "replaced"
    rec.rollback()
    assert target.read_text() == "original"


def test_write_file_rollback_removes_created_parents(tmp_path: Path) -> None:
    rec, _, _ = _make_recorder()
    fs = FilesystemHelper(rec)
    target = tmp_path / "deep" / "nest" / "a.txt"
    fs.write_file(target, "hello")
    assert target.exists()
    rec.rollback()
    assert not target.exists()
    assert not (tmp_path / "deep").exists()


def test_write_file_rollback_keeps_dirs_with_other_content(tmp_path: Path) -> None:
    rec, _, _ = _make_recorder()
    fs = FilesystemHelper(rec)
    parent = tmp_path / "shared"
    parent.mkdir()
    (parent / "sibling.txt").write_text("not ours")
    target = parent / "a.txt"
    fs.write_file(target, "hello")
    rec.rollback()
    assert not target.exists()
    assert (parent / "sibling.txt").exists()


def test_blocks_add_creates_file_and_rollback_removes_it(tmp_path: Path) -> None:
    rec, _, _ = _make_recorder()
    blocks = BlocksHelper(rec)
    target = tmp_path / "rules.md"
    blocks.add(target, "demo", "owned by demo", format="markdown")
    text = target.read_text()
    assert "owned by demo" in text
    assert "<!-- >>> cerebro:plugin:demo >>> -->" in text
    rec.rollback()
    assert not target.exists()


def test_blocks_add_appends_to_existing_file_and_rollback_restores(tmp_path: Path) -> None:
    rec, _, _ = _make_recorder()
    blocks = BlocksHelper(rec)
    target = tmp_path / "rc"
    target.write_text("# user content\n")
    blocks.add(target, "demo", "export X=1", format="shell")
    text = target.read_text()
    assert "# user content" in text
    assert "export X=1" in text
    rec.rollback()
    assert target.read_text() == "# user content\n"


def test_pkg_install_records_pre_existing_when_already_installed() -> None:
    pm = FakePackageManager(already_present={"git"})
    rec, _, _ = _make_recorder(package_manager=pm)
    pkg = PackageManagerHelper(rec, pm)
    pkg.install("git")
    ops = rec.operations
    assert len(ops) == 1
    assert ops[0].pre_existing is True
    rec.rollback()
    # Pre-existing means the package must NOT be uninstalled.
    assert "git" in pm.installed
    assert ("uninstall", "git") not in pm.calls


def test_pkg_install_rollback_uninstalls_newly_installed_package() -> None:
    pm = FakePackageManager()
    rec, _, _ = _make_recorder(package_manager=pm)
    pkg = PackageManagerHelper(rec, pm)
    pkg.install("ripgrep")
    assert "ripgrep" in pm.installed
    ops = rec.operations
    assert ops[0].pre_existing is False
    rec.rollback()
    assert "ripgrep" not in pm.installed


def test_tasks_register_records_pre_existing_when_already_present() -> None:
    sched = FakeScheduler(already_registered={"nightly"})
    rec, _, _ = _make_recorder(scheduler=sched)
    tasks = ScheduledTaskHelper(rec, sched)
    tasks.register("nightly", "daily@06:00", "echo")
    ops = rec.operations
    assert ops[0].pre_existing is True
    rec.rollback()
    assert "nightly" in sched.registered
    assert ("unregister", "nightly") not in sched.calls


def test_tasks_register_rollback_unregisters() -> None:
    sched = FakeScheduler()
    rec, _, _ = _make_recorder(scheduler=sched)
    tasks = ScheduledTaskHelper(rec, sched)
    tasks.register("nightly", "daily@06:00", "echo")
    assert "nightly" in sched.registered
    rec.rollback()
    assert "nightly" not in sched.registered


def test_commit_returns_install_manifest_with_all_operations(tmp_path: Path) -> None:
    rec, _, _ = _make_recorder()
    FilesystemHelper(rec).write_file(tmp_path / "a", "x")
    BlocksHelper(rec).add(tmp_path / "b.md", "demo", "y")
    manifest = rec.commit()
    assert manifest.plugin_name == "demo"
    assert manifest.version == "1.0.0"
    assert [op.kind for op in manifest.operations] == ["write_file", "add_block"]


def test_rollback_replays_inverses_in_reverse_order(tmp_path: Path) -> None:
    rec, _, _ = _make_recorder()
    fs = FilesystemHelper(rec)
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    fs.write_file(a, "first")
    fs.write_file(b, "second")
    fs.write_file(a, "first-overwritten")
    rec.rollback()
    assert not a.exists()
    assert not b.exists()


def test_install_manifest_serializes_with_pre_existing_flag(tmp_path: Path) -> None:
    pm = FakePackageManager(already_present={"git"})
    rec, _, _ = _make_recorder(package_manager=pm)
    PackageManagerHelper(rec, pm).install("git")
    manifest = rec.commit()
    dumped = manifest.model_dump(mode="json")
    assert dumped["operations"][0]["pre_existing"] is True


def test_acceptance_full_plugin_failure_leaves_disk_and_state_unchanged(
    tmp_path: Path,
) -> None:
    """The contract from SPEC-04: a plugin that uses every helper and
    fails mid-install rolls back every recorded operation."""

    pm = FakePackageManager(already_present={"git"})  # mimics ADR-0007 footnote
    sched = FakeScheduler()
    rec, _, _ = _make_recorder(package_manager=pm, scheduler=sched)

    fs = FilesystemHelper(rec)
    blocks = BlocksHelper(rec)
    pkg = PackageManagerHelper(rec, pm)
    tasks = ScheduledTaskHelper(rec, sched)

    rules = tmp_path / "rules" / "global.md"
    rc = tmp_path / "rc"
    rc.write_text("# user content\n")
    rc_before = rc.read_text()

    def install() -> None:
        fs.write_file(tmp_path / "config" / "demo.yaml", "key: value\n")
        blocks.add(rules, "demo", "rule one", format="markdown")
        blocks.add(rc, "demo", "export DEMO=1", format="shell")
        pkg.install("git")            # pre-existing — must not be removed
        pkg.install("ripgrep")        # newly installed — must be removed
        tasks.register("demo-nightly", "daily@06:00", "demo run")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        install()

    rec.rollback()

    # Disk state restored.
    assert not (tmp_path / "config").exists()
    assert not rules.exists()
    assert not rules.parent.exists()
    assert rc.read_text() == rc_before

    # Package state: pre-existing kept, newly installed removed.
    assert "git" in pm.installed
    assert "ripgrep" not in pm.installed
    assert ("uninstall", "git") not in pm.calls
    assert ("uninstall", "ripgrep") in pm.calls

    # Scheduler state restored.
    assert "demo-nightly" not in sched.registered


class _FakeBrewLikePm(FakePackageManager):
    """``FakePackageManager`` plus brew-style cask methods."""

    def __init__(
        self,
        *,
        already_installed: set[str] | None = None,
        already_cask_installed: set[str] | None = None,
    ) -> None:
        super().__init__(already_present=already_installed)
        self.cask_installed: set[str] = set(already_cask_installed or set())

    def is_cask_installed(self, package: str) -> bool:
        return package in self.cask_installed

    def install_cask(self, package: str) -> InstallResult:
        already = package in self.cask_installed
        self.cask_installed.add(package)
        self.calls.append(("install_cask", package))
        return InstallResult(already_installed=already)

    def uninstall_cask(self, package: str) -> None:
        self.cask_installed.discard(package)
        self.calls.append(("uninstall_cask", package))


def test_pkg_install_cask_records_cask_flag_and_pre_existing() -> None:
    pm = _FakeBrewLikePm(already_cask_installed={"visual-studio-code"})
    rec, _, _ = _make_recorder(package_manager=pm)
    helper = PackageManagerHelper(rec, pm)

    helper.install_cask("visual-studio-code")
    helper.install_cask("rectangle")

    ops = rec.operations
    assert ops[0].parameters == {
        "action": "install",
        "package": "visual-studio-code",
        "cask": True,
    }
    assert ops[0].pre_existing is True
    assert ops[1].parameters["cask"] is True
    assert ops[1].pre_existing is False


def test_pkg_install_cask_rollback_uses_uninstall_cask() -> None:
    pm = _FakeBrewLikePm()
    rec, _, _ = _make_recorder(package_manager=pm)
    helper = PackageManagerHelper(rec, pm)

    helper.install_cask("rectangle")
    assert "rectangle" in pm.cask_installed
    rec.rollback()
    assert "rectangle" not in pm.cask_installed
    assert ("uninstall_cask", "rectangle") in pm.calls
    assert ("uninstall", "rectangle") not in pm.calls


def test_pkg_install_cask_pre_existing_skips_uninstall_on_rollback() -> None:
    pm = _FakeBrewLikePm(already_cask_installed={"visual-studio-code"})
    rec, _, _ = _make_recorder(package_manager=pm)
    helper = PackageManagerHelper(rec, pm)

    helper.install_cask("visual-studio-code")
    rec.rollback()

    assert "visual-studio-code" in pm.cask_installed
    assert ("uninstall_cask", "visual-studio-code") not in pm.calls


def test_pkg_install_cask_raises_on_non_cask_capable_pm() -> None:
    pm = FakePackageManager()  # no install_cask attribute
    rec, _, _ = _make_recorder(package_manager=pm)
    helper = PackageManagerHelper(rec, pm)

    with pytest.raises(RuntimeError, match="brew-style"):
        helper.install_cask("visual-studio-code")
    assert rec.operations == []


def test_pkg_is_cask_installed_returns_false_on_non_brew_pm() -> None:
    pm = FakePackageManager()
    helper = PackageManagerHelper(_make_recorder(package_manager=pm)[0], pm)
    assert helper.is_cask_installed("visual-studio-code") is False


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, check):
        del check
        self.calls.append(list(argv))

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()


def test_cmd_run_records_run_command_and_runs_argv() -> None:
    runner = _FakeRunner()
    pm = FakePackageManager()
    rec = OperationRecorder(
        plugin_name="demo",
        plugin_version="1.0.0",
        when=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
        package_manager=pm,
        scheduler=FakeScheduler(),
        command_runner=runner,
    )
    helper = CommandHelper(rec, runner)

    helper.run(
        argv=["code", "--install-extension", "ext.id"],
        inverse_argv=["code", "--uninstall-extension", "ext.id"],
    )

    assert runner.calls == [["code", "--install-extension", "ext.id"]]
    op = rec.operations[0]
    assert op.kind == "run_command"
    assert op.parameters == {"argv": ["code", "--install-extension", "ext.id"]}
    assert op.inverse == {"argv": ["code", "--uninstall-extension", "ext.id"]}
    assert op.pre_existing is False


def test_cmd_run_pre_existing_skips_invocation_but_still_records() -> None:
    runner = _FakeRunner()
    pm = FakePackageManager()
    rec = OperationRecorder(
        plugin_name="demo",
        plugin_version="1.0.0",
        when=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
        package_manager=pm,
        scheduler=FakeScheduler(),
        command_runner=runner,
    )
    helper = CommandHelper(rec, runner)

    helper.run(
        argv=["code", "--install-extension", "ext.id"],
        inverse_argv=["code", "--uninstall-extension", "ext.id"],
        pre_existing=True,
    )

    assert runner.calls == []
    assert rec.operations[0].pre_existing is True


def test_cmd_run_rollback_runs_inverse_argv() -> None:
    runner = _FakeRunner()
    pm = FakePackageManager()
    rec = OperationRecorder(
        plugin_name="demo",
        plugin_version="1.0.0",
        when=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
        package_manager=pm,
        scheduler=FakeScheduler(),
        command_runner=runner,
    )
    helper = CommandHelper(rec, runner)

    helper.run(
        argv=["code", "--install-extension", "ext.id"],
        inverse_argv=["code", "--uninstall-extension", "ext.id"],
    )
    rec.rollback()

    assert runner.calls[-1] == ["code", "--uninstall-extension", "ext.id"]


def test_cmd_run_rollback_skips_inverse_for_pre_existing() -> None:
    runner = _FakeRunner()
    pm = FakePackageManager()
    rec = OperationRecorder(
        plugin_name="demo",
        plugin_version="1.0.0",
        when=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
        package_manager=pm,
        scheduler=FakeScheduler(),
        command_runner=runner,
    )
    helper = CommandHelper(rec, runner)

    helper.run(
        argv=["code", "--install-extension", "ext.id"],
        inverse_argv=["code", "--uninstall-extension", "ext.id"],
        pre_existing=True,
    )
    rec.rollback()

    assert runner.calls == []


def test_rollback_continues_past_failing_step(tmp_path: Path) -> None:
    """If one inverse fails, the remaining inverses still run."""
    rec, _, _ = _make_recorder()
    fs = FilesystemHelper(rec)
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    fs.write_file(a, "x")
    fs.write_file(b, "y")
    # Drop a.txt out from under the recorder; the rollback for b should
    # still run, the rollback for a should silently no-op (file is gone).
    a.unlink()
    rec.rollback()
    assert not b.exists()


