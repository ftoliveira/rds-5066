"""Testes de integração do `Tcp110dModemAdapter` contra o mock-modem (sem rds-hf).

Cobre Fases 1–3 do PLANO: handshake, dispatch de RX, keep-alive, worker de TX
(ARM/pré-fill/START/DRAIN), Receiver Master e reconexão. Usa `MockModem110d` em
loopback (TX keyed-up volta como RX) para round-trip de D_PDUs ponta a ponta.
"""

from __future__ import annotations

import time

import pytest

from src.dpdu_frame import DataHeader, DPDU, NonArqHeader, dpdu_set_address, encode_dpdu
from src.modem.tcp_110d_adapter import Tcp110dConfig, Tcp110dModemAdapter
from src.stypes import DPDUType
from tests.mock_110d_modem import MockModem110d

_ADDR = dpdu_set_address(destination=1, source=2, size=2)


def _data_dpdu(payload: bytes, seq: int = 1) -> bytes:
    return encode_dpdu(DPDU(
        dpdu_type=DPDUType.DATA_ONLY, eow=0, eot=10, address=_ADDR,
        data=DataHeader(True, True, False, False, False, False, len(payload), seq),
        user_data=payload))


def _nonarq_dpdu(payload: bytes, cpdu_id: int = 1) -> bytes:
    return encode_dpdu(DPDU(
        dpdu_type=DPDUType.NON_ARQ, eow=0, eot=4, address=_ADDR,
        non_arq=NonArqHeader(len(payload), 0, 1, False, False, cpdu_id),
        user_data=payload))


def _wait(pred, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def _make_loopback_modem(**kw) -> MockModem110d:
    m = MockModem110d(keepalive_period=30.0, **kw)
    m.on_air_tx = m.deliver_air_rx       # loopback: TX keyed-up volta como RX
    return m.start()


def _read_frame(adapter: Tcp110dModemAdapter, timeout: float = 5.0) -> bytes | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = adapter.modem_rx_read_frame()
        if frame is not None:
            return frame
        time.sleep(0.01)
    return None


# ── Fase 1: handshake + dispatch ─────────────────────────────────────────────

def test_handshake_completes_and_reports_rate():
    modem = _make_loopback_modem(data_rate=4800, blocking_factor=1200)
    adapter = Tcp110dModemAdapter(Tcp110dConfig(port=modem.port))
    try:
        adapter.modem_rx_start()
        assert _wait(lambda: adapter._connected.is_set(), 5.0), "não conectou"
        # Transmit Setup do modem deve refletir a taxa em config.data_rate_bps
        assert _wait(lambda: adapter.config.data_rate_bps == 4800, 2.0)
        assert adapter._tx_blocking_factor == 1200
        assert modem.connections_made == 1
    finally:
        adapter.stop()
        modem.stop()


def test_carrier_status_false_until_connected():
    modem = _make_loopback_modem()
    adapter = Tcp110dModemAdapter(Tcp110dConfig(port=modem.port))
    try:
        assert adapter.modem_get_carrier_status() is False     # antes de conectar
        adapter.modem_rx_start()
        assert _wait(lambda: adapter._connected.is_set(), 5.0)
        # sem RX pendente e sem portadora → False
        assert adapter.modem_get_carrier_status() is False
    finally:
        adapter.stop()
        modem.stop()


# ── Fase 2: TX worker + round-trip por loopback ──────────────────────────────

def test_single_dpdu_roundtrip():
    modem = _make_loopback_modem()
    adapter = Tcp110dModemAdapter(Tcp110dConfig(port=modem.port))
    try:
        adapter.modem_rx_start()
        assert _wait(lambda: adapter._connected.is_set(), 5.0)
        frame = _data_dpdu(b"hello-110d")
        assert adapter.modem_tx_burst([frame]) == len(frame)
        got = _read_frame(adapter)
        assert got == frame
        assert _wait(lambda: modem.bursts_received >= 1, 2.0)
    finally:
        adapter.stop()
        modem.stop()


def test_multi_dpdu_burst_roundtrip():
    modem = _make_loopback_modem()
    adapter = Tcp110dModemAdapter(Tcp110dConfig(port=modem.port))
    try:
        adapter.modem_rx_start()
        assert _wait(lambda: adapter._connected.is_set(), 5.0)
        f1 = _data_dpdu(b"frame-one", seq=1)
        f2 = _nonarq_dpdu(b"frame-two", cpdu_id=3)
        f3 = _data_dpdu(b"frame-three", seq=2)
        adapter.modem_tx_burst([f1, f2, f3])
        # os 3 D_PDUs devem voltar re-split corretamente, na ordem
        got = [_read_frame(adapter) for _ in range(3)]
        assert got == [f1, f2, f3]
    finally:
        adapter.stop()
        modem.stop()


def test_tx_dpdu_single_helper():
    modem = _make_loopback_modem()
    adapter = Tcp110dModemAdapter(Tcp110dConfig(port=modem.port))
    try:
        adapter.modem_rx_start()
        assert _wait(lambda: adapter._connected.is_set(), 5.0)
        frame = _data_dpdu(b"via-tx-dpdu")
        assert adapter.modem_tx_dpdu(frame) == len(frame)
        assert _read_frame(adapter) == frame
    finally:
        adapter.stop()
        modem.stop()


def test_large_payload_fragmented_roundtrip():
    # max_data_bytes pequeno força o stream da janela a fragmentar em vários
    # pacotes DATA_TRANSFER (FIRST_ONLY/CONTINUATION/LAST); deve re-split intacto.
    modem = _make_loopback_modem()
    cfg = Tcp110dConfig(port=modem.port, max_data_bytes=30, prefill_blocking_factors=0)
    adapter = Tcp110dModemAdapter(cfg)
    try:
        adapter.modem_rx_start()
        assert _wait(lambda: adapter._connected.is_set(), 5.0)
        frames = [_data_dpdu(bytes((i % 251 for i in range(200))), seq=k) for k in range(4)]
        total = sum(len(f) for f in frames)
        assert total > cfg.max_data_bytes      # garante fragmentação
        adapter.modem_tx_burst(frames)
        got = [_read_frame(adapter, timeout=8.0) for _ in range(len(frames))]
        assert got == frames
    finally:
        adapter.stop()
        modem.stop()


def test_receiver_master_not_ready_then_ready():
    # arm_ready_delay simula recepção em curso: ARM responde NOT_READY e só
    # depois PORT_READY; o worker deve aguardar e completar a janela.
    modem = _make_loopback_modem(arm_ready_delay=0.3)
    adapter = Tcp110dModemAdapter(Tcp110dConfig(port=modem.port))
    try:
        adapter.modem_rx_start()
        assert _wait(lambda: adapter._connected.is_set(), 5.0)
        frame = _data_dpdu(b"after-rx-master")
        adapter.modem_tx_burst([frame])
        assert _read_frame(adapter, timeout=8.0) == frame
    finally:
        adapter.stop()
        modem.stop()


# ── Fase 3: reconexão ────────────────────────────────────────────────────────

def test_reconnects_after_drop():
    modem = _make_loopback_modem()
    cfg = Tcp110dConfig(port=modem.port, reconnect_backoff_initial=0.2)
    adapter = Tcp110dModemAdapter(cfg)
    try:
        adapter.modem_rx_start()
        assert _wait(lambda: adapter._connected.is_set(), 5.0)
        # round-trip inicial
        f1 = _data_dpdu(b"before-drop")
        adapter.modem_tx_burst([f1])
        assert _read_frame(adapter) == f1

        # derruba a conexão; servidor segue escutando
        modem.drop_connections()
        assert _wait(lambda: not adapter._connected.is_set(), 5.0), "não detectou queda"
        assert adapter.modem_get_carrier_status() is False

        # reconecta e volta a transmitir
        assert _wait(lambda: adapter._connected.is_set(), 8.0), "não reconectou"
        f2 = _data_dpdu(b"after-reconnect")
        adapter.modem_tx_burst([f2])
        assert _read_frame(adapter, timeout=8.0) == f2
        assert modem.connections_made >= 2
    finally:
        adapter.stop()
        modem.stop()


def test_tick_never_blocks_when_disconnected():
    # modem_tx_burst deve retornar imediatamente mesmo sem conexão estabelecida.
    modem = _make_loopback_modem()
    adapter = Tcp110dModemAdapter(Tcp110dConfig(port=modem.port))
    try:
        frame = _data_dpdu(b"queued-early")
        t0 = time.monotonic()
        # antes de qualquer start explícito; tx_burst inicia as threads e enfileira
        assert adapter.modem_tx_burst([frame]) == len(frame)
        assert time.monotonic() - t0 < 0.5, "modem_tx_burst bloqueou"
        # ainda assim entrega após o handshake
        assert _read_frame(adapter, timeout=8.0) == frame
    finally:
        adapter.stop()
        modem.stop()
