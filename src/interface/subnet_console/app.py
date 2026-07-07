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
        ale_host: Optional[str] = None, ale_port: Optional[str] = None,
        no_ale: bool = False, argv: Optional[Sequence[str]] = None) -> int:
    app = QApplication.instance() or QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationName("S5066 Subnet Console")
    _install_default_font(app)

    controller = None
    ale_controller = None
    if live:
        # Fase 2: liga a um nó STANAG 5066 real (110D Appendix A / TCP).
        from .backend.node_controller import NodeController
        is_a = node.upper() == "A"
        local_id, remote_id = (1, 2) if is_a else (2, 1)
        host = modem_host or "127.0.0.1"
        port = int(modem_port) if modem_port else (3000 if is_a else 3001)
        # Raw SIS Socket Server (F.16): node A listens on 5066, node B on 5067
        # (matches chat_app_110d) so both nodes can run side by side.
        sis_port = 5066 if is_a else 5067
        controller = NodeController(local_id, remote_id, host, port,
                                    bitrate=bitrate, interleaver=interleaver,
                                    sis_port=sis_port)
        # Radio Control (ALE 2G): auto-connect to the radio backend on the same
        # host as the modem (UDP :54001 by default) — see PROTOCOLO-CONTROLE-REMOTO.
        # ``no_ale`` skips it (e.g. when UDP :54001 can't be reached — a plain SSH
        # -L tunnel only forwards TCP, so run_110d_real.sh needs a socat bridge).
        if not no_ale:
            from .backend.ale_controller import AleController, ALEL_PORT_CTRL
            ale_controller = AleController(ale_host or host,
                                           int(ale_port) if ale_port else ALEL_PORT_CTRL)

    model = ConsoleModel(node=node, accent=accent, modem_host=modem_host, modem_port=modem_port,
                         controller=controller, ale_controller=ale_controller)

    if ale_controller is not None:
        ale_controller.state_changed.connect(model.on_ale_state)
        ale_controller.channels_changed.connect(model.on_ale_channels)
        ale_controller.scan_changed.connect(model.on_ale_scan)
        ale_controller.lqa_changed.connect(model.on_ale_lqa)
        ale_controller.sound_hist_changed.connect(model.on_ale_sound_hist)
        ale_controller.log_received.connect(model.on_ale_log)
        ale_controller.amd_received.connect(model.on_ale_amd)
        ale_controller.connection_changed.connect(model.on_ale_conn)
        ale_controller.error.connect(lambda s: print(f"[ale] {s}", file=sys.stderr))
        app.aboutToQuit.connect(ale_controller.stop)
        ale_controller.start()   # UDP: just begins HELLO + telemetry RX

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
        # Arranca DESCONECTADO: o painel Modem mostra OFFLINE e o utilizador liga
        # com "Connect Modem" (model.toggle_modem → controller.start).

    win = SubnetConsoleWindow(model)
    win.show()
    return app.exec()
