"""Phase4Node — alias deprecated, use StanagNode."""

from __future__ import annotations


def __getattr__(name):
    if name == "Phase4Node":
        from src.stanag_node import StanagNode
        return StanagNode
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class Phase4Node:
    """Deprecated: use StanagNode."""
    def __new__(cls, *args, **kwargs):
        from src.stanag_node import StanagNode
        return StanagNode(*args, **kwargs)
