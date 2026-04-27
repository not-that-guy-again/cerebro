from __future__ import annotations

import plistlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cerebro.runtime.schedulers import (
    LaunchdScheduler,
    SystemdUserScheduler,
    build_launchd_plist,
    build_systemd_units,
)
from cerebro.runtime.scheduling import ScheduleSpec


@dataclass
class _FakeResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class _FakeRunner:
    def __init__(self, responses: Sequence[_FakeResult] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[list[str], bool]] = []

    def __call__(self, argv: Sequence[str], *, check: bool) -> _FakeResult:
        self.calls.append((list(argv), check))
        if not self.responses:
            return _FakeResult()
        return self.responses.pop(0)


# ---------------------------------------------------------------------------
# Plist generation
# ---------------------------------------------------------------------------


def test_build_plist_daily() -> None:
    spec = ScheduleSpec.parse("daily@06:00")
    blob = build_launchd_plist(label="com.cerebro.demo", schedule=spec, command="echo hi")
    parsed = plistlib.loads(blob)
    assert parsed["Label"] == "com.cerebro.demo"
    assert parsed["ProgramArguments"] == ["/bin/sh", "-c", "echo hi"]
    assert parsed["StartCalendarInterval"] == {"Hour": 6, "Minute": 0}
    assert parsed["RunAtLoad"] is False


def test_build_plist_hourly() -> None:
    spec = ScheduleSpec.parse("hourly")
    blob = build_launchd_plist(label="com.cerebro.demo", schedule=spec, command="echo hi")
    parsed = plistlib.loads(blob)
    assert parsed["StartCalendarInterval"] == {"Minute": 0}


def test_build_plist_weekly_uses_launchd_weekday_numbers() -> None:
    # launchd Weekday: Sun=0, Mon=1, ... Sat=6
    spec = ScheduleSpec.parse("weekly:wed@14:30")
    blob = build_launchd_plist(label="com.cerebro.demo", schedule=spec, command="echo hi")
    parsed = plistlib.loads(blob)
    assert parsed["StartCalendarInterval"] == {"Hour": 14, "Minute": 30, "Weekday": 3}


def test_build_plist_weekly_sunday() -> None:
    spec = ScheduleSpec.parse("weekly:sun@10:00")
    blob = build_launchd_plist(label="com.cerebro.demo", schedule=spec, command="echo hi")
    parsed = plistlib.loads(blob)
    assert parsed["StartCalendarInterval"]["Weekday"] == 0


def test_build_plist_monthly_wraps_command_in_guard() -> None:
    spec = ScheduleSpec.parse("monthly:last-business-day@09:00")
    blob = build_launchd_plist(
        label="com.cerebro.demo", schedule=spec, command="run-report.sh"
    )
    parsed = plistlib.loads(blob)
    assert parsed["StartCalendarInterval"] == {"Hour": 9, "Minute": 0}
    assert parsed["ProgramArguments"][0] == "/bin/sh"
    assert parsed["ProgramArguments"][1] == "-c"
    wrapped = parsed["ProgramArguments"][2]
    assert wrapped.endswith("&& run-report.sh")
    assert "python3" in wrapped
    assert "calendar.monthrange" in wrapped


# ---------------------------------------------------------------------------
# LaunchdScheduler
# ---------------------------------------------------------------------------


def test_launchd_register_writes_plist_and_invokes_launchctl(tmp_path: Path) -> None:
    runner = _FakeRunner()
    sched = LaunchdScheduler(plist_dir=tmp_path, runner=runner)
    sched.register("demo", "daily@06:00", "echo hi")

    plist_path = tmp_path / "com.cerebro.demo.plist"
    assert plist_path.exists()
    parsed = plistlib.loads(plist_path.read_bytes())
    assert parsed["Label"] == "com.cerebro.demo"

    # First call should be bootstrap (or load if bootstrap unavailable);
    # both reference our plist path.
    assert any(str(plist_path) in call[0] for call in runner.calls)
    assert all(call[0][0] == "launchctl" for call in runner.calls)


def test_launchd_is_registered_reflects_filesystem(tmp_path: Path) -> None:
    sched = LaunchdScheduler(plist_dir=tmp_path, runner=_FakeRunner())
    assert sched.is_registered("demo") is False
    sched.register("demo", "daily@06:00", "echo hi")
    assert sched.is_registered("demo") is True


def test_launchd_unregister_removes_plist(tmp_path: Path) -> None:
    runner = _FakeRunner()
    sched = LaunchdScheduler(plist_dir=tmp_path, runner=runner)
    sched.register("demo", "daily@06:00", "echo hi")
    assert sched.is_registered("demo") is True

    sched.unregister("demo")
    assert sched.is_registered("demo") is False
    # Either bootout or unload was called against the plist.
    deactivate_calls = [
        c for c in runner.calls if c[0][1] in {"bootout", "unload"}
    ]
    assert deactivate_calls, runner.calls


def test_launchd_unregister_missing_is_noop(tmp_path: Path) -> None:
    runner = _FakeRunner()
    sched = LaunchdScheduler(plist_dir=tmp_path, runner=runner)
    sched.unregister("never-registered")
    assert runner.calls == []


def test_launchd_register_idempotent_overwrites(tmp_path: Path) -> None:
    runner = _FakeRunner()
    sched = LaunchdScheduler(plist_dir=tmp_path, runner=runner)
    sched.register("demo", "daily@06:00", "echo hi")
    sched.register("demo", "daily@07:00", "echo bye")
    parsed = plistlib.loads((tmp_path / "com.cerebro.demo.plist").read_bytes())
    assert parsed["StartCalendarInterval"] == {"Hour": 7, "Minute": 0}
    assert parsed["ProgramArguments"][2] == "echo bye"


# ---------------------------------------------------------------------------
# Systemd unit generation
# ---------------------------------------------------------------------------


def test_build_systemd_units_daily() -> None:
    spec = ScheduleSpec.parse("daily@06:00")
    service, timer = build_systemd_units(
        name="demo", description="Cerebro task: demo", schedule=spec, command="echo hi"
    )
    assert "Description=Cerebro task: demo" in service
    assert "Type=oneshot" in service
    assert "ExecStart=/bin/sh -c 'echo hi'" in service
    assert "OnCalendar=*-*-* 06:00:00" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


def test_build_systemd_units_hourly() -> None:
    spec = ScheduleSpec.parse("hourly")
    _, timer = build_systemd_units(
        name="demo", description="d", schedule=spec, command="echo"
    )
    assert "OnCalendar=*-*-* *:00:00" in timer


def test_build_systemd_units_weekly() -> None:
    spec = ScheduleSpec.parse("weekly:fri@17:30")
    _, timer = build_systemd_units(
        name="demo", description="d", schedule=spec, command="echo"
    )
    assert "OnCalendar=Fri *-*-* 17:30:00" in timer


def test_build_systemd_units_monthly_wraps_command_in_guard() -> None:
    spec = ScheduleSpec.parse("monthly:last-business-day@09:00")
    service, timer = build_systemd_units(
        name="demo", description="d", schedule=spec, command="run-report.sh"
    )
    # Daily timer; the guard handles the day-of-month check.
    assert "OnCalendar=*-*-* 09:00:00" in timer
    assert "python3" in service
    assert "&& run-report.sh" in service


def test_build_systemd_units_quotes_complex_commands() -> None:
    spec = ScheduleSpec.parse("daily@06:00")
    service, _ = build_systemd_units(
        name="demo",
        description="d",
        schedule=spec,
        command="echo hello && echo world",
    )
    # shlex.quote should single-quote the whole command.
    assert "'echo hello && echo world'" in service


# ---------------------------------------------------------------------------
# SystemdUserScheduler
# ---------------------------------------------------------------------------


def test_systemd_register_writes_units_and_runs_systemctl(tmp_path: Path) -> None:
    runner = _FakeRunner()
    sched = SystemdUserScheduler(unit_dir=tmp_path, runner=runner)
    sched.register("demo", "daily@06:00", "echo hi")

    service = tmp_path / "cerebro-demo.service"
    timer = tmp_path / "cerebro-demo.timer"
    assert service.exists()
    assert timer.exists()

    args = [c[0] for c in runner.calls]
    assert ["systemctl", "--user", "daemon-reload"] in args
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "cerebro-demo.timer",
    ] in args


def test_systemd_is_registered_checks_timer_file(tmp_path: Path) -> None:
    sched = SystemdUserScheduler(unit_dir=tmp_path, runner=_FakeRunner())
    assert sched.is_registered("demo") is False
    sched.register("demo", "daily@06:00", "echo hi")
    assert sched.is_registered("demo") is True


def test_systemd_unregister_disables_and_removes_units(tmp_path: Path) -> None:
    runner = _FakeRunner()
    sched = SystemdUserScheduler(unit_dir=tmp_path, runner=runner)
    sched.register("demo", "daily@06:00", "echo hi")

    runner.calls.clear()
    sched.unregister("demo")

    args = [c[0] for c in runner.calls]
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "cerebro-demo.timer",
    ] in args
    assert not (tmp_path / "cerebro-demo.timer").exists()
    assert not (tmp_path / "cerebro-demo.service").exists()


def test_systemd_unregister_missing_is_noop(tmp_path: Path) -> None:
    runner = _FakeRunner()
    sched = SystemdUserScheduler(unit_dir=tmp_path, runner=runner)
    sched.unregister("never-registered")
    assert runner.calls == []
