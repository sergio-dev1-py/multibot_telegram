"""Hermes Agent platform plugin: several Telegram bots, one Hermes agent.

The public entry point is :func:`register` in :mod:`registration`; this module
re-exports it so the plugin loader and ``hermes plugins validate`` find a
conventional ``register(ctx)``.
"""

from __future__ import annotations

from .registration import (  # noqa: F401
    adapter_factory,
    check_requirements,
    ensure_dependencies,
    is_connected,
    register,
)

__all__ = [
    "register",
    "check_requirements",
    "ensure_dependencies",
    "is_connected",
    "adapter_factory",
]
