"""Scheduled-task interface used by ``ctx.tasks``.

The recorder targets this protocol so that the launchd / systemd
implementations defined by SPEC-05 can be slotted in without changing
helpers or plugin code.

``is_registered`` lets the recorder mark a register operation as
``pre_existing`` if the task name was already present, mirroring the
package-manager pattern.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Scheduler(Protocol):
    def is_registered(self, name: str) -> bool: ...

    def register(self, name: str, schedule: str, command: str) -> None: ...

    def unregister(self, name: str) -> None: ...


__all__ = ["Scheduler"]
