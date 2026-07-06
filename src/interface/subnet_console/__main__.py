"""Entry point: ``python -m src.interface.subnet_console``.

Also runnable directly (``python src/interface/subnet_console/__main__.py``); the
sys.path shim below lets it find the ``src`` package either way, matching the
other apps in ``src/interface``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure the repository root is importable as ``src.*`` when run as a file.
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.interface.subnet_console import theme as T
from src.interface.subnet_console.app import run


def main() -> int:
    p = argparse.ArgumentParser(description="STANAG 5066 Subnet Console (PyQt6)")
    p.add_argument("--node", default="A", choices=["A", "B"], help="local node identity/profile")
    p.add_argument("--accent", default="blue", choices=list(T.ACCENT_OPTIONS),
                   help="accent colour theme")
    p.add_argument("--modem-host", default=None, help="modem IP for the Modem Link pane")
    p.add_argument("--modem-port", default=None, help="modem TCP port")
    p.add_argument("--live", action="store_true",
                   help="drive a real STANAG 5066 node (110D Appendix A/TCP) instead of demo data")
    p.add_argument("--bitrate", type=int, default=2400, help="initial data rate (bps) in --live mode")
    p.add_argument("--interleaver", default="long", choices=["short", "long"],
                   help="initial interleaver in --live mode")
    args = p.parse_args()
    return run(node=args.node, accent=T.ACCENT_OPTIONS[args.accent],
               modem_host=args.modem_host, modem_port=args.modem_port,
               live=args.live, bitrate=args.bitrate, interleaver=args.interleaver)


if __name__ == "__main__":
    raise SystemExit(main())
