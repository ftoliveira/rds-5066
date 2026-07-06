"""Application bootstrap for the S5066 Subnet Console."""
from __future__ import annotations

import sys
from typing import Optional, Sequence

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication

from . import theme as T
from .model import ConsoleModel
from .window import SubnetConsoleWindow


def _install_default_font(app: QApplication) -> None:
    T.load_fonts()
    f = QFont()
    f.setFamilies(T.SANS_STACK)
    f.setPixelSize(13)
    app.setFont(f)


def run(node: str = "A", accent: str = T.DEFAULT_ACCENT,
        modem_host: Optional[str] = None, modem_port: Optional[str] = None,
        live: bool = False, bitrate: int = 2400, interleaver: str = "long",
        argv: Optional[Sequence[str]] = None) -> int:
    app = QApplication.instance() or QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationName("S5066 Subnet Console")
    _install_default_font(app)

    controller = None
    if live:
        # Fase 2: liga a um nó STANAG 5066 real (110D Appendix A / TCP).
        from .backend.node_controller import NodeController
        is_a = node.upper() == "A"
        local_id, remote_id = (1, 2) if is_a else (2, 1)
        host = modem_host or "127.0.0.1"
        port = int(modem_port) if modem_port else (3000 if is_a else 3001)
        controller = NodeController(local_id, remote_id, host, port,
                                    bitrate=bitrate, interleaver=interleaver)

    model = ConsoleModel(node=node, accent=accent, modem_host=modem_host, modem_port=modem_port,
                         controller=controller)

    if controller is not None:
        controller.status_changed.connect(model.apply_live_status)
        # Fatia 2 — HFCHAT (SAP 5): RX, hard link e rejeições alimentam a thread/feed.
        controller.unidata_received.connect(model.on_rx)
        controller.link_established.connect(model.on_link_up)
        controller.link_terminated.connect(model.on_link_down)
        controller.request_rejected.connect(model.on_rejected)
        controller.node_error.connect(lambda s: print(f"[node] {s}", file=sys.stderr))
        # Reflete o alvo real do modem nos campos do painel Modem Link.
        model.modem.update(ip=controller.host, port=str(controller.port),
                           rate=bitrate, interleaver=interleaver.upper())
        app.aboutToQuit.connect(controller.stop)
        controller.start()   # auto-conecta ao arrancar (como o estado "linked" do demo)

    win = SubnetConsoleWindow(model)
    win.show()
    return app.exec()
