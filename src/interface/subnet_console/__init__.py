"""S5066 Subnet Console — PyQt6 desktop console for a STANAG 5066 node.

A single-window "Annex F Client Manager" that unifies every SIS client of a
STANAG 5066 node (HFCHAT, HF Mail, IP Client, File Transfer, Raw SIS Socket)
plus a subnet dashboard, traffic monitor, modem-link setup and configuration.

Phase 1 (this package) is a faithful, fully navigable UI shell seeded with the
same demo data as the ``S5066 Subnet Console`` design mockup, structured around
:class:`~src.interface.subnet_console.model.ConsoleModel` so that wiring it to
the live :class:`~src.stanag_node.StanagNode` backend is an incremental Phase 2.
"""

__all__ = ["ConsoleModel", "Theme", "SubnetConsoleWindow", "run"]

__version__ = "3.0"
