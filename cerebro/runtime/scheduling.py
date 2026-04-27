"""Scheduled-task interface and cadence parser used by ``ctx.tasks``.

The ``Scheduler`` protocol is the cross-platform contract; concrete
launchd / systemd-user implementations live in
``cerebro.runtime.schedulers``.

``ScheduleSpec.parse`` accepts the cadence strings plugins write in
``plugin.yaml``:

- ``hourly`` — every hour at minute 00.
- ``daily@HH:MM`` — once a day at HH:MM local time.
- ``weekly:<dow>@HH:MM`` — once a week on the named day-of-week
  (mon/tue/wed/thu/fri/sat/sun, case-insensitive).
- ``monthly:last-business-day@HH:MM`` — last Mon-Fri of each month.

Parse errors raise ``ScheduleParseError`` with a message that names the
offending input.

``is_registered`` lets the recorder mark a register operation as
``pre_existing`` if the task name was already present, mirroring the
package-manager pattern.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class Cadence(enum.Enum):
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY_LAST_BUSINESS_DAY = "monthly_last_business_day"


class Weekday(enum.Enum):
    """ISO weekday: Monday=0 ... Sunday=6, matching ``datetime.weekday()``."""

    MON = 0
    TUE = 1
    WED = 2
    THU = 3
    FRI = 4
    SAT = 5
    SUN = 6


_DOW_NAMES: dict[str, Weekday] = {
    "mon": Weekday.MON,
    "tue": Weekday.TUE,
    "wed": Weekday.WED,
    "thu": Weekday.THU,
    "fri": Weekday.FRI,
    "sat": Weekday.SAT,
    "sun": Weekday.SUN,
}


class ScheduleParseError(ValueError):
    """Raised when a cadence string cannot be parsed."""


_TIME_RE = re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{2})$")


def _parse_hhmm(value: str, *, raw: str) -> tuple[int, int]:
    m = _TIME_RE.match(value)
    if m is None:
        raise ScheduleParseError(
            f"invalid time {value!r} in cadence {raw!r}; expected HH:MM"
        )
    hour = int(m.group("h"))
    minute = int(m.group("m"))
    if not (0 <= hour <= 23):
        raise ScheduleParseError(
            f"hour out of range in cadence {raw!r}: {hour} (expected 0-23)"
        )
    if not (0 <= minute <= 59):
        raise ScheduleParseError(
            f"minute out of range in cadence {raw!r}: {minute} (expected 0-59)"
        )
    return hour, minute


@dataclass(frozen=True)
class ScheduleSpec:
    """Parsed representation of a cadence string."""

    cadence: Cadence
    raw: str
    hour: int | None = None
    minute: int | None = None
    day_of_week: Weekday | None = None

    @classmethod
    def parse(cls, raw: str) -> ScheduleSpec:
        if not isinstance(raw, str):  # pragma: no cover - defensive
            raise ScheduleParseError(f"cadence must be a string, got {type(raw).__name__}")
        text = raw.strip()
        if not text:
            raise ScheduleParseError("cadence is empty")

        if text == "hourly":
            return cls(cadence=Cadence.HOURLY, raw=raw, minute=0)

        if text.startswith("daily@"):
            time_part = text[len("daily@") :]
            hour, minute = _parse_hhmm(time_part, raw=raw)
            return cls(cadence=Cadence.DAILY, raw=raw, hour=hour, minute=minute)

        if text.startswith("weekly:"):
            rest = text[len("weekly:") :]
            if "@" not in rest:
                raise ScheduleParseError(
                    f"weekly cadence missing time-of-day in {raw!r}; expected weekly:<dow>@HH:MM"
                )
            dow_part, time_part = rest.split("@", 1)
            dow = _DOW_NAMES.get(dow_part.lower())
            if dow is None:
                raise ScheduleParseError(
                    f"unknown day-of-week {dow_part!r} in cadence {raw!r}; "
                    "expected one of mon, tue, wed, thu, fri, sat, sun"
                )
            hour, minute = _parse_hhmm(time_part, raw=raw)
            return cls(
                cadence=Cadence.WEEKLY,
                raw=raw,
                hour=hour,
                minute=minute,
                day_of_week=dow,
            )

        if text.startswith("monthly:"):
            rest = text[len("monthly:") :]
            if "@" not in rest:
                raise ScheduleParseError(
                    f"monthly cadence missing time-of-day in {raw!r}; "
                    "expected monthly:last-business-day@HH:MM"
                )
            kind, time_part = rest.split("@", 1)
            if kind != "last-business-day":
                raise ScheduleParseError(
                    f"unsupported monthly cadence {kind!r} in {raw!r}; "
                    "only monthly:last-business-day is supported"
                )
            hour, minute = _parse_hhmm(time_part, raw=raw)
            return cls(
                cadence=Cadence.MONTHLY_LAST_BUSINESS_DAY,
                raw=raw,
                hour=hour,
                minute=minute,
            )

        raise ScheduleParseError(
            f"unrecognized cadence {raw!r}; "
            "expected hourly | daily@HH:MM | weekly:<dow>@HH:MM | "
            "monthly:last-business-day@HH:MM"
        )


@runtime_checkable
class Scheduler(Protocol):
    def is_registered(self, name: str) -> bool: ...

    def register(self, name: str, schedule: str, command: str) -> None: ...

    def unregister(self, name: str) -> None: ...


__all__ = [
    "Cadence",
    "ScheduleParseError",
    "ScheduleSpec",
    "Scheduler",
    "Weekday",
]
