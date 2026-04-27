"""Concrete ``Scheduler`` implementations for launchd and systemd-user.

Both schedulers translate a ``ScheduleSpec`` into the platform's native
scheduling primitives plus a wrapped command. The wrapping handles
``monthly:last-business-day``, which neither launchd nor systemd express
natively: we register a daily entry at HH:MM and prefix the command with
a small Python guard that exits non-zero unless today is the last
weekday of the month.

File locations:
- launchd: plist files in ``~/Library/LaunchAgents/`` activated with
  ``launchctl bootstrap`` (falling back to ``launchctl load`` on older
  macOS).
- systemd: ``.service`` + ``.timer`` units in
  ``~/.config/systemd/user/`` activated with
  ``systemctl --user enable --now``.

Tests pass a custom ``unit_dir`` / ``plist_dir`` and a fake ``runner``
to avoid touching the real activation system.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from cerebro.runtime.scheduling import Cadence, ScheduleSpec, Weekday


class _CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        check: bool,
    ) -> _CompletedProcessLike: ...


def _default_runner(argv: Sequence[str], *, check: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), check=check, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Last-business-day guard
# ---------------------------------------------------------------------------

# Inline Python that exits 0 only on the last Mon-Fri of the current month.
# Kept on one logical line so it can be embedded in shell args without
# heredocs or temp files.
_LBD_GUARD = (
    "python3 -c "
    "'import datetime,calendar,sys;"
    "d=datetime.date.today();"
    "last=calendar.monthrange(d.year,d.month)[1];"
    "end=datetime.date(d.year,d.month,last);"
    "end-=datetime.timedelta(days=max(0,end.weekday()-4));"
    "sys.exit(0 if d==end else 1)'"
)


def _wrap_command(spec: ScheduleSpec, command: str) -> str:
    """Wrap ``command`` with any cadence-specific guard."""
    if spec.cadence is Cadence.MONTHLY_LAST_BUSINESS_DAY:
        return f"{_LBD_GUARD} && {command}"
    return command


# ---------------------------------------------------------------------------
# launchd
# ---------------------------------------------------------------------------

# launchd Weekday: 0=Sunday, 1=Monday, ... 6=Saturday (7 also Sunday).
_WEEKDAY_TO_LAUNCHD: dict[Weekday, int] = {
    Weekday.SUN: 0,
    Weekday.MON: 1,
    Weekday.TUE: 2,
    Weekday.WED: 3,
    Weekday.THU: 4,
    Weekday.FRI: 5,
    Weekday.SAT: 6,
}


def _launchd_calendar_interval(spec: ScheduleSpec) -> dict[str, int]:
    """Build the ``StartCalendarInterval`` dict for ``spec``.

    For ``monthly:last-business-day`` we register the job daily at HH:MM
    and rely on the embedded guard to no-op on non-matching days.
    """
    if spec.cadence is Cadence.HOURLY:
        return {"Minute": 0}
    if spec.cadence is Cadence.DAILY:
        assert spec.hour is not None and spec.minute is not None
        return {"Hour": spec.hour, "Minute": spec.minute}
    if spec.cadence is Cadence.WEEKLY:
        assert spec.hour is not None and spec.minute is not None
        assert spec.day_of_week is not None
        return {
            "Hour": spec.hour,
            "Minute": spec.minute,
            "Weekday": _WEEKDAY_TO_LAUNCHD[spec.day_of_week],
        }
    if spec.cadence is Cadence.MONTHLY_LAST_BUSINESS_DAY:
        assert spec.hour is not None and spec.minute is not None
        return {"Hour": spec.hour, "Minute": spec.minute}
    raise AssertionError(f"unhandled cadence: {spec.cadence!r}")  # pragma: no cover


def build_launchd_plist(
    *,
    label: str,
    schedule: ScheduleSpec,
    command: str,
) -> bytes:
    """Return the bytes of the plist for a single scheduled job."""
    plist: dict[str, object] = {
        "Label": label,
        "ProgramArguments": ["/bin/sh", "-c", _wrap_command(schedule, command)],
        "StartCalendarInterval": _launchd_calendar_interval(schedule),
        "RunAtLoad": False,
    }
    return plistlib.dumps(plist)


class LaunchdScheduler:
    """Writes plists to ``~/Library/LaunchAgents/`` and activates via launchctl."""

    def __init__(
        self,
        *,
        plist_dir: Path | None = None,
        label_prefix: str = "com.cerebro.",
        runner: CommandRunner | None = None,
        launchctl_binary: str = "launchctl",
    ) -> None:
        self._plist_dir = (
            Path(plist_dir)
            if plist_dir is not None
            else Path.home() / "Library" / "LaunchAgents"
        )
        self._label_prefix = label_prefix
        self._run = runner or _default_runner
        self._launchctl = launchctl_binary

    @property
    def plist_dir(self) -> Path:
        return self._plist_dir

    def _label(self, name: str) -> str:
        return f"{self._label_prefix}{name}"

    def _plist_path(self, name: str) -> Path:
        return self._plist_dir / f"{self._label(name)}.plist"

    def is_registered(self, name: str) -> bool:
        return self._plist_path(name).exists()

    def register(self, name: str, schedule: str, command: str) -> None:
        spec = ScheduleSpec.parse(schedule)
        plist_bytes = build_launchd_plist(
            label=self._label(name), schedule=spec, command=command
        )
        self._plist_dir.mkdir(parents=True, exist_ok=True)
        path = self._plist_path(name)
        path.write_bytes(plist_bytes)
        self._activate(path)

    def unregister(self, name: str) -> None:
        path = self._plist_path(name)
        if not path.exists():
            return
        self._deactivate(path)
        path.unlink()

    def _activate(self, path: Path) -> None:
        # Prefer modern bootstrap; fall back to legacy load. Some test
        # environments will reject both — that's fine, the file is the
        # primary deliverable.
        domain = self._user_domain()
        if domain is not None:
            result = self._run(
                [self._launchctl, "bootstrap", domain, str(path)], check=False
            )
            if result.returncode == 0:
                return
        self._run([self._launchctl, "load", str(path)], check=False)

    def _deactivate(self, path: Path) -> None:
        domain = self._user_domain()
        if domain is not None:
            label = path.stem
            self._run(
                [self._launchctl, "bootout", f"{domain}/{label}"], check=False
            )
            return
        self._run([self._launchctl, "unload", str(path)], check=False)

    @staticmethod
    def _user_domain() -> str | None:
        if not hasattr(os, "getuid"):
            return None
        return f"gui/{os.getuid()}"


# ---------------------------------------------------------------------------
# systemd-user
# ---------------------------------------------------------------------------

_WEEKDAY_TO_SYSTEMD: dict[Weekday, str] = {
    Weekday.MON: "Mon",
    Weekday.TUE: "Tue",
    Weekday.WED: "Wed",
    Weekday.THU: "Thu",
    Weekday.FRI: "Fri",
    Weekday.SAT: "Sat",
    Weekday.SUN: "Sun",
}


def _systemd_oncalendar(spec: ScheduleSpec) -> str:
    if spec.cadence is Cadence.HOURLY:
        return "*-*-* *:00:00"
    if spec.cadence is Cadence.DAILY:
        assert spec.hour is not None and spec.minute is not None
        return f"*-*-* {spec.hour:02d}:{spec.minute:02d}:00"
    if spec.cadence is Cadence.WEEKLY:
        assert spec.hour is not None and spec.minute is not None
        assert spec.day_of_week is not None
        dow = _WEEKDAY_TO_SYSTEMD[spec.day_of_week]
        return f"{dow} *-*-* {spec.hour:02d}:{spec.minute:02d}:00"
    if spec.cadence is Cadence.MONTHLY_LAST_BUSINESS_DAY:
        assert spec.hour is not None and spec.minute is not None
        # Run daily; the embedded guard rejects non-matching days.
        return f"*-*-* {spec.hour:02d}:{spec.minute:02d}:00"
    raise AssertionError(f"unhandled cadence: {spec.cadence!r}")  # pragma: no cover


def build_systemd_units(
    *,
    name: str,
    description: str,
    schedule: ScheduleSpec,
    command: str,
) -> tuple[str, str]:
    """Return ``(service_unit_text, timer_unit_text)`` for a scheduled job."""
    wrapped = _wrap_command(schedule, command)
    # ExecStart must be absolute; wrap with /bin/sh -c so commands that
    # contain pipelines, redirects, or our guard work as written.
    exec_start = f"/bin/sh -c {shlex.quote(wrapped)}"
    service = (
        "[Unit]\n"
        f"Description={description}\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={exec_start}\n"
    )
    timer = (
        "[Unit]\n"
        f"Description={description} (timer)\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar={_systemd_oncalendar(schedule)}\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


class SystemdUserScheduler:
    """Writes ``.service`` + ``.timer`` units to the user systemd dir."""

    def __init__(
        self,
        *,
        unit_dir: Path | None = None,
        unit_prefix: str = "cerebro-",
        runner: CommandRunner | None = None,
        systemctl_binary: str = "systemctl",
    ) -> None:
        self._unit_dir = (
            Path(unit_dir)
            if unit_dir is not None
            else Path.home() / ".config" / "systemd" / "user"
        )
        self._prefix = unit_prefix
        self._run = runner or _default_runner
        self._systemctl = systemctl_binary

    @property
    def unit_dir(self) -> Path:
        return self._unit_dir

    def _unit_basename(self, name: str) -> str:
        return f"{self._prefix}{name}"

    def _service_path(self, name: str) -> Path:
        return self._unit_dir / f"{self._unit_basename(name)}.service"

    def _timer_path(self, name: str) -> Path:
        return self._unit_dir / f"{self._unit_basename(name)}.timer"

    def is_registered(self, name: str) -> bool:
        return self._timer_path(name).exists()

    def register(self, name: str, schedule: str, command: str) -> None:
        spec = ScheduleSpec.parse(schedule)
        service_text, timer_text = build_systemd_units(
            name=name,
            description=f"Cerebro task: {name}",
            schedule=spec,
            command=command,
        )
        self._unit_dir.mkdir(parents=True, exist_ok=True)
        self._service_path(name).write_text(service_text, encoding="utf-8")
        self._timer_path(name).write_text(timer_text, encoding="utf-8")
        self._run([self._systemctl, "--user", "daemon-reload"], check=False)
        self._run(
            [
                self._systemctl,
                "--user",
                "enable",
                "--now",
                f"{self._unit_basename(name)}.timer",
            ],
            check=False,
        )

    def unregister(self, name: str) -> None:
        timer = self._timer_path(name)
        service = self._service_path(name)
        if not (timer.exists() or service.exists()):
            return
        self._run(
            [
                self._systemctl,
                "--user",
                "disable",
                "--now",
                f"{self._unit_basename(name)}.timer",
            ],
            check=False,
        )
        if timer.exists():
            timer.unlink()
        if service.exists():
            service.unlink()
        self._run([self._systemctl, "--user", "daemon-reload"], check=False)


__all__ = [
    "CommandRunner",
    "LaunchdScheduler",
    "SystemdUserScheduler",
    "build_launchd_plist",
    "build_systemd_units",
]
