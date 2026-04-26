"""Auth handoff interface used by ``ctx.auth``.

A handoff pauses the install for the user to complete an interactive
authentication step (logging in to a third-party service, copying a
generated token into a CLI, etc.). It is not a recorded operation: the
side effect lives outside Cerebro and there is nothing to roll back.

The protocol is defined here so the platform-abstraction PR can ship the
real implementation. Tests in this package use a no-op double.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AuthHandoff(Protocol):
    def request(self, *, message: str) -> None: ...


class NullAuthHandoff:
    """A handoff that does nothing, useful as a default and in tests."""

    def request(self, *, message: str) -> None:  # noqa: D401 - protocol impl
        del message


__all__ = ["AuthHandoff", "NullAuthHandoff"]
