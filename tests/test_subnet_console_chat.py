"""Fatia 2 (HFCHAT ao vivo) do Subnet Console — verificação headless ponta-a-ponta.

Dois `NodeController` reais cruzados por `MockAir`; um `ConsoleModel(live)` por
nó, ligado exatamente como em ``app.run()``. Estabelece o hard link SAP 5, envia
uma mensagem de chat de A→B e confirma que ela chega à *thread* ao vivo do modelo
B, e que os *feeds* de S-primitives dos dois lados registam os eventos.

Corre offscreen (``QT_QPA_PLATFORM=offscreen``), sem display — é a mesma malha
que o ``run_110d_real.sh`` exercita, mas com o mock-modem em vez de rádio real.
"""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402 (após definir a plataforma)

from src.interface.subnet_console.backend.node_controller import NodeController  # noqa: E402
from src.interface.subnet_console.model import ConsoleModel  # noqa: E402
from tests.mock_110d_modem import MockAir  # noqa: E402


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _wire(controller: NodeController, model: ConsoleModel) -> None:
    """Liga os sinais do controller ao modelo, como faz ``app.run()``."""
    controller.status_changed.connect(model.apply_live_status)
    controller.unidata_received.connect(model.on_rx)
    controller.link_established.connect(model.on_link_up)
    controller.link_terminated.connect(model.on_link_down)
    controller.request_rejected.connect(model.on_rejected)


def _pump(app: QApplication, pred, timeout: float = 20.0, step: float = 0.01) -> bool:
    """Bombeia o event loop Qt até ``pred()`` ou até esgotar ``timeout``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if pred():
            return True
        time.sleep(step)
    app.processEvents()
    return pred()


def test_hfchat_live_end_to_end(qapp):
    air = MockAir(keepalive_period=2.0).start()
    ctrl_a = NodeController(1, 2, "127.0.0.1", air.modem_a.port)
    ctrl_b = NodeController(2, 1, "127.0.0.1", air.modem_b.port)
    model_a = ConsoleModel(node="A", controller=ctrl_a)
    model_b = ConsoleModel(node="B", controller=ctrl_b)
    _wire(ctrl_a, model_a)
    _wire(ctrl_b, model_b)
    try:
        ctrl_a.start()
        ctrl_b.start()
        assert _pump(qapp, lambda: model_a._live_status.get("connected")
                     and model_b._live_status.get("connected"), 10.0), \
            "adaptadores não conectaram ao mock-modem"

        # Hard link SAP 5 (A inicia; link_type 0 → B auto-aceita).
        model_a.toggle_chat_link()
        assert _pump(qapp, lambda: model_a.chat_link_up and model_b.chat_link_up, 20.0), \
            "hard link SAP 5 não estabeleceu nos dois nós"

        # Envia uma mensagem de chat A→B.
        text = "FALCON to NODE 2, live chat check."
        model_a.set_draft(text)
        model_a.send_msg()
        assert model_a.draft == "" and model_a.live_messages[-1]["text"] == text
        assert _pump(qapp, lambda: any(m["dir"] == "in" for m in model_b.live_messages), 20.0), \
            "mensagem HFCHAT não chegou ao nó B"

        rx = next(m for m in model_b.live_messages if m["dir"] == "in")
        assert rx["text"] == text          # ASCII + CRLF removido
        assert rx["from"] == "NODE 1"      # src_addr do nó A
        assert rx["conf"] == "RECEIVED"

        # Os dois feeds registaram o caminho SIS.
        assert any(n == "S_UNIDATA_INDICATION" for _, n, _ in model_b.live_prims)
        assert any(n == "S_UNIDATA_REQUEST" for _, n, _ in model_a.live_prims)
        assert any("ESTABLISH_CONFIRM" in n for _, n, _ in model_a.live_prims)

        # A thread ao vivo do B alimenta o accessor que o ecrã desenha.
        drawn = model_b.chat_messages()
        assert drawn[-1]["text"] == text and drawn[-1]["align"] == "l"
    finally:
        ctrl_a.stop()
        ctrl_b.stop()
        air.stop()
        qapp.processEvents()


def test_demo_chat_unaffected(qapp):
    """Sem controller o modelo continua em modo demo (seam intacto)."""
    model = ConsoleModel(node="A")
    assert not model.live
    assert len(model.chat_messages()) == 6                 # seed de demonstração
    assert model.chat_header()["name"] == "CORVUS-06"
    assert model.chat_prims()[-1]["name"] == "S_BIND_REQUEST"

    model.set_draft("hello demo")
    model.send_msg()
    assert model.chat_messages()[-1]["text"] == "hello demo"
    assert model.live_messages == []                       # nada foi para o caminho live
