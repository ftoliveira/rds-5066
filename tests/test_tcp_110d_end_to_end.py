"""End-to-end: dois `StanagNode` reais sobre o adaptador TCP/110D + mock-modem.

Topologia (PLANO §11): StanagNode A ↔ Tcp110dModemAdapter ↔ MockAir ↔
Tcp110dModemAdapter ↔ StanagNode B. O `MockAir` cruza os dois modems (TX de um
vira RX do outro), modelando o canal OTA compartilhado entre dois modems 110D.

Exercita o caminho de dados completo (DTS/ARQ/Non-ARQ/CAS/SIS inalterados) sobre
o protocolo TCP do Anexo A:
  - UNIDATA Non-ARQ ponta a ponta;
  - hard link (CAS MADE → hard link request/confirm) + UNIDATA ARQ ponta a ponta.
"""

from __future__ import annotations

import time

import pytest

from src.cas import CasConfig
from src.modem.tcp_110d_adapter import Tcp110dConfig, Tcp110dModemAdapter
from src.stanag_node import StanagNode
from src.stypes import DeliveryMode
from tests.mock_110d_modem import MockAir


def _make_node(addr: int, port: int) -> StanagNode:
    adapter = Tcp110dModemAdapter(Tcp110dConfig(port=port, keepalive_period=2.0))
    node = StanagNode(
        addr, adapter,
        cas_config=CasConfig(call_timeout_seconds=5.0, break_timeout_seconds=5.0, max_retries=5),
        max_user_data_bytes=128,
        use_arq_data=True,
        soft_link_idle_timeout_ms=60_000,
        arq_reset_retransmit_ms=3000,
        arq_retx_timeout_ms=3000,
        arq_max_retries=5,
    )
    node.arq.data_rate_bps = 2400
    node.arq.long_interleave = False
    return node


def _wait(pred, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _drive_until(nodes, cond, timeout: float = 20.0, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        now_ms = int(time.monotonic() * 1000)
        for n in nodes:
            n.tick(now_ms)
        if cond():
            return True
        time.sleep(step)
    now_ms = int(time.monotonic() * 1000)
    for n in nodes:
        n.tick(now_ms)
    return cond()


@pytest.fixture
def air_and_nodes():
    air = MockAir(keepalive_period=2.0).start()
    node_a = _make_node(1, air.modem_a.port)
    node_b = _make_node(2, air.modem_b.port)
    # aguarda ambos os adaptadores completarem o handshake
    ok = _wait(lambda: node_a.modem._connected.is_set() and node_b.modem._connected.is_set(), 6.0)
    assert ok, "adaptadores não conectaram ao mock-modem"
    try:
        yield node_a, node_b
    finally:
        node_a.modem.stop()
        node_b.modem.stop()
        air.stop()


def test_non_arq_unidata_end_to_end(air_and_nodes):
    node_a, node_b = air_and_nodes
    node_a.bind(sap_id=3, rank=0)
    node_b.bind(sap_id=3, rank=0)

    received: list = []
    node_b.register_callbacks(unidata_indication=lambda ind: received.append(ind))

    payload = b"NON-ARQ via 110D/TCP"
    node_a.unidata_request(
        sap_id=3, dest_addr=2, dest_sap=3, priority=0, ttl_seconds=30,
        mode=DeliveryMode(arq_mode=False), updu=payload,
    )

    assert _drive_until([node_a, node_b], lambda: bool(received), timeout=20.0), \
        "UNIDATA Non-ARQ não foi entregue ao nó B"
    ind = received[0]
    assert ind.updu == payload
    assert ind.src_addr == 1
    assert ind.dest_sap == 3


def test_hard_link_and_arq_unidata_end_to_end(air_and_nodes):
    node_a, node_b = air_and_nodes
    node_a.bind(sap_id=3, rank=0)
    node_b.bind(sap_id=3, rank=0)

    established_a: list = []
    established_b: list = []
    received: list = []
    node_a.register_callbacks(hard_link_established=lambda addr, sap: established_a.append((addr, sap)))
    node_b.register_callbacks(
        hard_link_established=lambda addr, sap: established_b.append((addr, sap)),
        unidata_indication=lambda ind: received.append(ind),
    )

    # A inicia hard link para B (link_type 0 → B auto-aceita)
    node_a.hard_link_establish(sap_id=3, link_priority=1, remote_addr=2, remote_sap=3, link_type=0)
    assert _drive_until([node_a, node_b],
                        lambda: bool(established_a) and bool(established_b), timeout=20.0), \
        "hard link não foi estabelecido nos dois nós"
    assert established_a[0][0] == 2          # A vê o remoto = 2
    assert established_b[0][0] == 1          # B vê o remoto = 1

    # UNIDATA ARQ sobre o hard link
    payload = b"ARQ payload over hard link"
    node_a.unidata_request(
        sap_id=3, dest_addr=2, dest_sap=3, priority=0, ttl_seconds=30,
        mode=DeliveryMode(arq_mode=True), updu=payload,
    )
    assert _drive_until([node_a, node_b], lambda: bool(received), timeout=20.0), \
        "UNIDATA ARQ não foi entregue ao nó B"
    assert received[0].updu == payload
    assert received[0].src_addr == 1
