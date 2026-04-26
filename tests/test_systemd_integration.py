"""Integration test for ``SystemdUserScheduler`` on Linux.

Marked ``linux`` so CI can run only on the Linux runner. Skips when the
user systemd instance is not reachable (no ``XDG_RUNTIME_DIR``, no DBus
session). The workflow is responsible for setting up linger and
``XDG_RUNTIME_DIR`` if it wants this test to run.

This test writes its units to the real ``~/.config/systemd/user/``
directory because ``systemctl --user`` only looks at well-known paths.
The unit name embeds the test PID and is removed in ``finally``; we
guard the prefix with ``cerebro-it-`` so a leak is obviously us.
"""

from __future__ import annotations

import os
import platform as _platform
import shutil
import subprocess

import pytest

from cerebro.runtime.schedulers import SystemdUserScheduler


def _systemd_user_available() -> bool:
    if shutil.which("systemctl") is None:
        return False
    if not os.environ.get("XDG_RUNTIME_DIR"):
        return False
    result = subprocess.run(
        ["systemctl", "--user", "is-system-running"],
        capture_output=True,
        text=True,
        check=False,
    )
    # 0 = running, 1 = degraded, 4 = offline. 0/1 mean reachable.
    return result.returncode in (0, 1)


pytestmark = [
    pytest.mark.linux,
    pytest.mark.skipif(
        _platform.system() != "Linux",
        reason="systemd-user integration is Linux only",
    ),
    pytest.mark.skipif(
        not _systemd_user_available(),
        reason="systemd user instance not reachable",
    ),
]


def test_systemd_register_and_unregister_noop_task() -> None:
    sched = SystemdUserScheduler(unit_prefix="cerebro-it-")
    name = f"noop-{os.getpid()}"
    timer_unit = f"cerebro-it-{name}.timer"
    service_unit = f"cerebro-it-{name}.service"
    timer_path = sched.unit_dir / timer_unit
    service_path = sched.unit_dir / service_unit

    try:
        sched.register(name, "daily@06:00", "/bin/true")
        assert timer_path.exists()
        assert service_path.exists()

        result = subprocess.run(
            ["systemctl", "--user", "is-enabled", timer_unit],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.stdout.strip() == "enabled", (
            f"timer not enabled: stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    finally:
        sched.unregister(name)
        assert not timer_path.exists()
        assert not service_path.exists()
