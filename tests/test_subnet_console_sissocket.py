"""Fatia 5 (Raw SIS Socket Server, F.16) do Subnet Console — verificação headless.

Exercita o servidor SIS ao vivo: o `NodeController` arranca um
`RawSisSocketServer` (instrumentado) num loop asyncio em thread própria; um
cliente TCP real liga-se, faz `S_BIND_REQUEST` e o console reflete o cliente na
lista de "Connected Clients" e as primitivas no "SIS Wire Log".
"""
from __future__ import annotations

import os
import socket
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.interface.subnet_console.backend.node_controller import NodeController  # noqa: E402
from src.interface.subnet_console.model import ConsoleModel  # noqa: E402
from src.s_primitive_codec import (  # noqa: E402
    decode_s_primitive, encode_bind_request, encode_unbind_request,
)
from src.stypes import SPrimitiveType  # noqa: E402
from tests.mock_110d_modem import MockModem110d  # noqa: E402


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _pump(app, pred, timeout: float = 20.0, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if pred():
            return True
        time.sleep(step)
    app.processEvents()
    return pred()


def _bring_up(app):
    """One node with its SIS server on an ephemeral port (sis_port=0)."""
    modem = MockModem110d().start()
    ctrl = NodeController(1, 2, "127.0.0.1", modem.port, sis_port=0)
    model = ConsoleModel(node="A", controller=ctrl)
    ctrl.status_changed.connect(model.apply_live_status)
    ctrl.unidata_received.connect(model.on_rx)
    ctrl.start()
    # O servidor liga assincronamente; espera o snapshot do model refletir LISTENING
    # (o poll de 500 ms propaga sis_actual_port para _live_status).
    assert _pump(app, lambda: model.sk_status()["listening"], 10.0), \
        "SIS server não abriu porta"
    return modem, ctrl, model


def _read_primitive(sock: socket.socket, timeout: float = 5.0) -> int:
    """Block until one framed S-primitive arrives; return its type."""
    sock.settimeout(timeout)
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        buf.extend(sock.recv(4096))
        try:
            prim_type, _payload, _n = decode_s_primitive(bytes(buf))
            return prim_type
        except ValueError:
            continue
    raise AssertionError("nenhuma S-primitive recebida do servidor")


def test_sis_client_bind_shows_in_console(qapp):
    modem, ctrl, model = _bring_up(qapp)
    sock = None
    try:
        assert model.sk_status()["listening"] is True
        assert f":{ctrl.sis_actual_port}" in model.sk_status()["label"]
        assert model.sk_clients() == []          # ninguém ligado ainda

        # Cliente TCP externo liga-se e faz bind ao SAP 9 (não pré-ligado pelo nó).
        sock = socket.create_connection(("127.0.0.1", ctrl.sis_actual_port), timeout=5.0)
        assert _pump(qapp, lambda: len(model.sk_clients()) == 1, 10.0), \
            "cliente TCP não apareceu na consola"
        assert model.sk_clients()[0]["st"] == "CONNECTED"   # ligado, ainda sem bind

        sock.sendall(encode_bind_request(sap_id=9, rank=8))
        assert _read_primitive(sock) == SPrimitiveType.S_BIND_ACCEPTED

        assert _pump(qapp, lambda: model.sk_clients()
                     and model.sk_clients()[0]["st"] == "BOUND", 10.0), \
            "bind não refletiu na lista de clientes"
        client = model.sk_clients()[0]
        assert client["sap"] == "9"
        assert client["client"] == "IP Client"    # nome Annex F do SAP 9
        assert client["rank"] == "8"

        # KPIs ao vivo agregam a ligação e as primitivas.
        kpis = {k["label"]: k["value"] for k in model.sk_kpis()}
        assert kpis["TCP Connections"] == "1"
        assert kpis["Bound Sockets"] == "1"
        assert kpis["Server"] == "UP"
        assert int(kpis["Primitives"]) >= 2       # bind req (C→S) + accept (S→C)

        # Wire log real regista ambas as direções do handshake.
        wire = model.sk_wire()
        names = {(w["dir"], w["name"]) for w in wire}
        assert ("C → S", "S_BIND_REQUEST") in names
        assert ("S → C", "S_BIND_ACCEPTED") in names
        # SIS Wire Log é o mais recente primeiro.
        assert wire[0]["name"] == "S_BIND_ACCEPTED"

        # Desligar liberta o SAP e drena a lista de clientes.
        sock.sendall(encode_unbind_request())
        sock.close()
        sock = None
        assert _pump(qapp, lambda: model.sk_clients() == [], 10.0), \
            "cliente não saiu da lista após desconexão"
    finally:
        if sock is not None:
            sock.close()
        ctrl.stop()
        modem.stop()
        qapp.processEvents()


def test_sis_server_stops_with_node(qapp):
    modem, ctrl, model = _bring_up(qapp)
    try:
        assert ctrl.sis_server is not None
        ctrl.stop()
        assert _pump(qapp, lambda: not model.sk_status()["listening"], 5.0), \
            "servidor SIS não ficou OFFLINE após stop()"
        assert ctrl.sis_server is None
        assert model.sk_status()["label"] == "SERVER OFFLINE"
    finally:
        modem.stop()
        qapp.processEvents()


def test_demo_sissocket_unaffected(qapp):
    """Sem controller os accessors sk_* devolvem o seed de demonstração."""
    m = ConsoleModel(node="A")
    assert not m.live
    assert m.sk_status()["listening"] is True
    assert len(m.sk_clients()) == 5
    assert len(m.sk_wire()) == 8
    assert m.sk_kpis()[0]["value"] == "5"
