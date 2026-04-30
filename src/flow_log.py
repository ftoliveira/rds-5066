"""Logs de fluxo STANAG: prefixo único [FLOW TX]/[FLOW RX] por camada para reconstruir o pipeline só pelo log.

Grep sugerido:  grep FLOW seu_log.txt

Ordem típica TX (Phase3 + ARQ):
  1) [CAS] ENQUEUE / TX LINK_* ou DATA
  2) [FLOW TX] [DTS] CAS CPDU -> NonARQ fila  OU  ENQUEUE ARQ DATA / submit_cpdu
  3) [FLOW TX] [NonARQ] QUEUE -> SEGMENTOU n_segmentos -> (cada segmento) DTS NonARQ -> PHY
  4) [FLOW TX] [DTS] ARQ pending (RESET) ou ARQ frame -> PHY
  5) [FLOW TX] [PHY] inicio burst | D_PDU tipo=... -> chunks UDP + EOB

Ordem típica RX:
  1) [FLOW RX] [PHY] EOB -> receive_streaming
  2) [FLOW RX] [PHY] D_PDU decodificado -> fila RX modem
  3) [FLOW RX] [DTS] tipo -> fila NonARQ | ARQ process_rx_dpdu | RESET
  4) [FLOW RX] [DTS] NonARQ reassembly completo -> CAS
  5) [FLOW RX] [ARQ] C_PDU remontada -> CAS (DATA)
  6) [CAS] RX CPDU ...
"""

from __future__ import annotations

import time

SYNC_BYTES = b"\x90\xEB"

# DPDUType nibble 0..15 (Annex C)
_DPDU_TYPE_NAMES: dict[int, str] = {
    0: "DATA_ONLY",
    1: "ACK_ONLY",
    2: "DATA_ACK",
    3: "RESETWIN_RESYNC",
    4: "EXPEDITED_DATA_ONLY",
    5: "EXPEDITED_ACK_ONLY",
    6: "MANAGEMENT",
    7: "NON_ARQ",
    8: "EXPEDITED_NON_ARQ",
    15: "WARNING",
}

# S_PDU types (Annex A) — nibble alto do byte 0
_SPDU_TYPE_NAMES: dict[int, str] = {
    0: "S_PDU_DATA",
    1: "S_PDU_DATA_DELIVERY_CONFIRM",
    2: "S_PDU_DATA_DELIVERY_FAIL",
    3: "S_PDU_HARD_LINK_REQUEST",
    4: "S_PDU_HARD_LINK_CONFIRM",
    5: "S_PDU_HARD_LINK_REJECTED",
    6: "S_PDU_HARD_LINK_TERMINATE",
    7: "S_PDU_HARD_LINK_TERMINATE_CONFIRM",
}

# CAS CPDU types
_CAS_CPDU_NAMES: dict[int, str] = {
    0: "CAS_DATA",
    1: "CAS_LINK_REQUEST",
    2: "CAS_LINK_ACCEPTED",
    3: "CAS_LINK_REJECTED",
    4: "CAS_LINK_BREAK",
    5: "CAS_LINK_BREAK_CONFIRM",
}


def ts() -> str:
    return time.strftime("%H:%M:%S")


def payload_hint(payload: bytes) -> str:
    """Identifica S_PDU ou CAS CPDU pelo primeiro octeto (para logs FLOW)."""
    if not payload:
        return "empty"
    b0 = payload[0]
    c_pdu_type = (b0 >> 4) & 0x0F
    
    # CAS C_PDU Tipo 0 (DATA) contém um S_PDU a partir do byte 1
    if c_pdu_type == 0:
        if len(payload) > 1:
            s_b0 = payload[1]
            spdu_t = (s_b0 >> 4) & 0x0F
            name = _SPDU_TYPE_NAMES.get(spdu_t, f"S_PDU_type_{spdu_t}")
            return f"{name} (DATA)"
        return "CAS_DATA (vazio)"
        
    # CAS C_PDU Tipos 1-5 (Controle)
    if 1 <= c_pdu_type <= 5:
        return _CAS_CPDU_NAMES.get(c_pdu_type, f"CAS_type_{c_pdu_type}")
        
    return f"UNKNOWN_CPDU_type_{c_pdu_type}"


def dpdu_wire_hint(raw: bytes) -> str:
    """Identifica tipo D_PDU pelos primeiros octetos (sync + nibble tipo), sem decode completo."""
    if not raw:
        return "empty"
    if len(raw) < 3:
        return f"len={len(raw)} (curto)"
    if raw[:2] != SYNC_BYTES:
        return f"len={len(raw)} sync!=90EB"
    t = (raw[2] >> 4) & 0x0F
    name = _DPDU_TYPE_NAMES.get(t, f"type_{t}")
    return f"D_PDU {name} wire_len={len(raw)}"


def flow_tx(layer: str, msg: str) -> None:
    print(f"[{ts()}] [FLOW TX] [{layer}] {msg}")


def flow_rx(layer: str, msg: str) -> None:
    print(f"[{ts()}] [FLOW RX] [{layer}] {msg}")
