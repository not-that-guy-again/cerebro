"""Integration test for ``LaunchdScheduler`` on macOS.

Marked ``macos`` so CI can run only on the macOS runner. Tests actually
invoke ``launchctl`` against a no-op task; failures here mean our plist
shape is wrong, not that mocked test scaffolding is wrong.
"""

from __future__ import annotations

import platform as _platform
import shutil
import subprocess
from pathlib import Path

import pytest

from cerebro.runtime.schedulers import LaunchdScheduler

pytestmark = [
    pytest.mark.macos,
    pytest.mark.skipif(
        _platform.system() != "Darwin", reason="launchd integration is macOS only"
    ),
    pytest.mark.skipif(
        shutil.which("launchctl") is None, reason="launchctl not available"
    ),
]


def test_launchd_register_and_unregister_noop_task(tmp_path: Path) -> None:
    plist_dir = tmp_path / "LaunchAgents"
    sched = LaunchdScheduler(
        plist_dir=plist_dir,
        label_prefix="com.cerebro.it.",
    )

    name = "noop-task"
    label = f"com.cerebro.it.{name}"
    plist_path = plist_dir / f"{label}.plist"

    try:
        sched.register(name, "daily@06:00", "/usr/bin/true")
        assert plist_path.exists()

        # Confirm launchctl sees the label. ``launchctl list <label>`` exits 0
        # when registered; non-zero otherwise.
        result = subprocess.run(
            ["launchctl", "list", label], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, (
            f"launchctl could not see {label}; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    finally:
        sched.unregister(name)
        # File and registration both gone.
        assert not plist_path.exists()
        result = subprocess.run(
            ["launchctl", "list", label], capture_output=True, text=True, check=False
        )
        assert result.returncode != 0
