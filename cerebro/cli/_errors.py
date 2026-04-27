"""Exit-code policy for the Cerebro CLI.

We keep the set small but distinct enough that scripts can branch on
common failure modes (plugin missing vs. dependency in use) without
hard-coding messages. ``USAGE`` matches Click's default for argument
errors so we do not fight the framework.
"""

from __future__ import annotations

import logging
import sys
import traceback
from collections.abc import Callable
from functools import wraps
from typing import Any

import click

from cerebro.plugins.loader import PluginConflictError, PluginLoadError
from cerebro.plugins.resolver import DependencyResolutionError
from cerebro.runtime.engine import (
    DependencyInUseError,
    EngineError,
    ReconfigureError,
    TransactionRollbackError,
)
from cerebro.runtime.lifecycle import LifecycleError
from cerebro.runtime.taps import TapError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_PRECONDITION = 4
EXIT_TRANSACTION_ROLLED_BACK = 5

def _exit_code_for(exc: BaseException) -> int:
    if isinstance(exc, TransactionRollbackError):
        return EXIT_TRANSACTION_ROLLED_BACK
    if isinstance(exc, DependencyInUseError):
        return EXIT_PRECONDITION
    if isinstance(exc, DependencyResolutionError | PluginLoadError):
        return EXIT_NOT_FOUND
    if isinstance(exc, LifecycleError | TapError | PluginConflictError):
        return EXIT_PRECONDITION
    if isinstance(exc, ReconfigureError | EngineError):
        return EXIT_ERROR
    return EXIT_ERROR


def handle_errors[F: Callable[..., Any]](func: F) -> F:
    """Convert known engine/tap exceptions into clean CLI failures.

    A friendly one-liner goes to stderr; the full traceback goes to the
    Python logger (which the engine writes to its log file, and which
    ``--verbose`` mirrors to stderr).
    """

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except click.ClickException:
            raise
        except KeyboardInterrupt:
            click.echo("aborted.", err=True)
            sys.exit(EXIT_ERROR)
        except (
            DependencyInUseError,
            DependencyResolutionError,
            EngineError,
            LifecycleError,
            PluginConflictError,
            PluginLoadError,
            ReconfigureError,
            TapError,
            TransactionRollbackError,
        ) as exc:
            logging.getLogger("cerebro").debug(
                "command failed:\n%s", traceback.format_exc()
            )
            click.echo(f"error: {exc}", err=True)
            sys.exit(_exit_code_for(exc))

    return wrapper  # type: ignore[return-value]


__all__ = [
    "EXIT_ERROR",
    "EXIT_NOT_FOUND",
    "EXIT_OK",
    "EXIT_PRECONDITION",
    "EXIT_TRANSACTION_ROLLED_BACK",
    "EXIT_USAGE",
    "handle_errors",
]
