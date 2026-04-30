"""
F.3 — COSS (Character-Oriented Serial Stream Client, SAP 1).

Encapsula fluxo de caracteres serial em S_PRIMITIVES para transporte sobre
a sub-rede HF, suportando vários conjuntos de caracteres e modos de
encapsulamento (F.3.4).

Modos de encapsulamento (Tabela F-3):
  OCTET    — dados de 8 bits arbitrários (F.3.4.1)
  ITA5     — ASCII/ITA-5, 7 bits, MSB=0 (F.3.4.2)
  LPI2E    — ITA-2 Loose-Pack, 5 bits no nibble baixo (F.3.4.3.1)
  DPI2E    — ITA-2 Dense-Pack, 3 chars em 2 bytes (F.3.4.3.2)
  SIX_BIT  — 6 bits por caractere, 2 MSBs = 0 (F.3.4.4)

Disciplina de flush do buffer (F.3.4.5):
  1. Threshold de bytes (COSS_BUF_FLUSH_THRESHOLD)
  2. Detecção de CRLF no fluxo de entrada
  3. Timeout (configurável)

Interface serial emulada via `asyncio` (sem hardware físico):
  - CossClient.feed_bytes(data) — injetar bytes no buffer de entrada
  - CossClient.on_serial_output — callback para bytes entregues ao serviço serial

Modos de serviço (F.3.3):
  ARQ (ponto-a-ponto, padrão) ou non-ARQ (multicast).
"""

from __future__ import annotations

import logging
import time
from enum import IntEnum
from typing import Callable

from src.stypes import DeliveryMode, SAP_ID_COSS, SisUnidataIndication

from .base_client import SubnetClient

logger = logging.getLogger(__name__)


# ─── Modos de encapsulamento ──────────────────────────────────────────────────


class CossMode(IntEnum):
    """Modo de encapsulamento de caracteres COSS."""
    OCTET = 0    # 8 bits arbitrários (F.3.4.1)
    ITA5 = 1     # ASCII/ITA-5, 7 bits (F.3.4.2)
    LPI2E = 2    # ITA-2 Loose-Pack (F.3.4.3.1)
    DPI2E = 3    # ITA-2 Dense-Pack (F.3.4.3.2)
    SIX_BIT = 4  # 6 bits (F.3.4.4)


# ─── Codec de caracteres ──────────────────────────────────────────────────────


class CharacterEncoder:
    """Encoder/decoder de conjuntos de caracteres COSS (F.3.4)."""

    @staticmethod
    def encode(data: bytes, mode: CossMode) -> bytes:
        """Encapsula dados no modo especificado."""
        if mode == CossMode.OCTET:
            return data  # passthrough (F.3.4.1)

        elif mode == CossMode.ITA5:
            # LSB alinhado, MSB=0 (F.3.4.2)
            return bytes(b & 0x7F for b in data)

        elif mode == CossMode.LPI2E:
            # 5 bits no nibble baixo, 3 MSBs = 0 (F.3.4.3.1)
            return bytes(b & 0x1F for b in data)

        elif mode == CossMode.DPI2E:
            return CharacterEncoder._encode_dpi2e(data)

        elif mode == CossMode.SIX_BIT:
            # 6 bits, 2 MSBs = 0 (F.3.4.4)
            return bytes(b & 0x3F for b in data)

        raise ValueError(f"Modo desconhecido: {mode}")

    @staticmethod
    def decode(data: bytes, mode: CossMode) -> bytes:
        """Desencapsula dados do modo especificado."""
        if mode == CossMode.OCTET:
            return data

        elif mode == CossMode.ITA5:
            return bytes(b & 0x7F for b in data)

        elif mode == CossMode.LPI2E:
            return bytes(b & 0x1F for b in data)

        elif mode == CossMode.DPI2E:
            return CharacterEncoder._decode_dpi2e(data)

        elif mode == CossMode.SIX_BIT:
            return bytes(b & 0x3F for b in data)

        raise ValueError(f"Modo desconhecido: {mode}")

    # ── DPI2E (Dense-Pack ITA-2) ──────────────────────────────────────────────

    @staticmethod
    def _encode_dpi2e(chars: bytes) -> bytes:
        """Encapsulamento Dense-Pack ITA-2 (F.3.4.3.2).

        3 chars de 5 bits em 2 bytes (Encapsulation Pair):
          i=2n:   ITA2j+1[4:2] | ITA2j[4:0]
          i=2n+1: DP_FLG | ITA2j+2[4:0] | ITA2j+1[1:0]

        Se R = B mod 3 == 0: todos em 3-into-2 Pairs (DP_FLG=1).
        Se R == 1: (B-1) em 3-into-2 + último em Loose (1 octet, 3MSBs=0).
        Se R == 2: (B-2) em 3-into-2 + 2-into-2 Pair (DP_FLG=0).
        """
        b = [c & 0x1F for c in chars]
        B = len(b)
        R = B % 3
        result = bytearray()

        # Quantos chars vão em 3-into-2 Pairs
        n_triplets = (B - R) // 3
        idx = 0
        # 3-into-2 Pair layout (F.3.4.3.2):
        #   byte_even: bits[7:5]=ITA2j+1[2:0], bits[4:0]=ITA2j[4:0]
        #   byte_odd:  bit[7]=DP_FLG=1, bits[6:5]=ITA2j+1[4:3], bits[4:0]=ITA2j+2[4:0]
        for _ in range(n_triplets):
            c0, c1, c2 = b[idx], b[idx + 1], b[idx + 2]
            byte_even = ((c1 & 0x07) << 5) | (c0 & 0x1F)
            byte_odd = 0x80 | ((c1 & 0x18) << 2) | (c2 & 0x1F)
            result.append(byte_even)
            result.append(byte_odd)
            idx += 3

        # Remainder
        if R == 1:
            # Último char em Loose-Pack (3 MSBs = 0)
            result.append(b[idx] & 0x1F)
        elif R == 2:
            # 2-into-2 Pair (DP_FLG=0)
            c0, c1 = b[idx], b[idx + 1]
            byte_even = ((c1 & 0x07) << 5) | (c0 & 0x1F)
            byte_odd = 0x00 | ((c1 & 0x18) << 2)  # DP_FLG=0, ITA2j+2 bits=0
            result.append(byte_even)
            result.append(byte_odd)

        return bytes(result)

    @staticmethod
    def _decode_dpi2e(raw: bytes) -> bytes:
        """Desencapsulamento Dense-Pack ITA-2 (F.3.4.3.2.4).

        Per spec, decode based on L (U_PDU length):
          - L = 1: single loosely packed ITA-2 character
          - L > 1, L even: unpack from Encapsulation Pairs in order
          - L > 1, L odd: unpack from Encapsulation Pairs + last loose byte
        """
        L = len(raw)
        if L == 0:
            return b""

        result = bytearray()

        # Case 1: L = 1 — single loose-packed character
        if L == 1:
            if raw[0] & 0xE0:
                logger.debug("DPI2E: 3 MSBs non-zero in single-byte decode")
            result.append(raw[0] & 0x1F)
            return bytes(result)

        # Determine how many pair-bytes to process
        # L odd: last byte is loose-packed, pairs occupy L-1 bytes
        # L even: all bytes are in pairs
        pair_bytes = L if (L % 2 == 0) else (L - 1)

        for i in range(0, pair_bytes, 2):
            byte_even = raw[i]
            byte_odd = raw[i + 1]
            dp_flg = (byte_odd >> 7) & 0x01
            is_last_pair = (i + 2 >= pair_bytes)

            c0 = byte_even & 0x1F                    # ITA2j [4:0]
            c1_low = (byte_even >> 5) & 0x07         # ITA2j+1 [2:0]
            c1 = c1_low | ((byte_odd & 0x60) >> 2)   # ITA2j+1 [4:3]
            c1 &= 0x1F

            result.append(c0)
            result.append(c1)

            if dp_flg:
                # 3-into-2: third character present
                c2 = byte_odd & 0x1F  # ITA2j+2
                result.append(c2)
            else:
                # 2-into-2: only valid for last pair (L even)
                if not is_last_pair:
                    logger.debug("DPI2E: DP_FLG=0 on non-last pair at offset %d", i)

        # Case 3: L odd — last byte is loose-packed
        if L % 2 == 1:
            last = raw[L - 1]
            if last & 0xE0:
                logger.debug("DPI2E: 3 MSBs non-zero in trailing loose byte")
            result.append(last & 0x1F)

        return bytes(result)


# ─── Buffer com disciplina de flush ──────────────────────────────────────────


class _FlushBuffer:
    """Buffer de entrada COSS com disciplina de flush (F.3.4.5).

    Flush é disparado por:
      1. threshold: número de bytes acumulados atinge COSS_BUF_FLUSH_THRESHOLD.
      2. CRLF: par \\r\\n detectado no fluxo.
      3. timeout: intervalo desde último byte excede flush_timeout_s.
    """

    def __init__(
        self,
        threshold: int = 512,
        flush_on_crlf: bool = True,
        flush_timeout_s: float = 2.0,
        on_flush: Callable[[bytes], None] | None = None,
    ):
        self.threshold = threshold
        self.flush_on_crlf = flush_on_crlf
        self.flush_timeout_s = flush_timeout_s
        self.on_flush = on_flush
        self._buf = bytearray()
        self._last_rx = time.monotonic()

    def feed(self, data: bytes):
        """Injeta bytes no buffer; dispara flush conforme disciplina."""
        for byte in data:
            self._buf.append(byte)
            self._last_rx = time.monotonic()

            # 1. Threshold
            if len(self._buf) >= self.threshold:
                self._flush()
                continue

            # 2. CRLF
            if self.flush_on_crlf and len(self._buf) >= 2:
                if self._buf[-2] == 0x0D and self._buf[-1] == 0x0A:
                    self._flush()

    def tick(self):
        """Verifica timeout; deve ser chamado periodicamente."""
        if (
            self._buf
            and (time.monotonic() - self._last_rx) >= self.flush_timeout_s
        ):
            self._flush()

    def flush_now(self):
        """Força flush imediato."""
        if self._buf:
            self._flush()

    def _flush(self):
        if not self._buf:
            return
        chunk = bytes(self._buf)
        self._buf.clear()
        if self.on_flush:
            self.on_flush(chunk)


# ─── CossClient ───────────────────────────────────────────────────────────────


class CossClient(SubnetClient):
    """F.3 — COSS Client, SAP 1 (Character-Oriented Serial Stream).

    Envio (injetar dados na interface serial de entrada):
        client.feed_bytes(data)  — envia bytes pelo HF com flush automático

    Recepção:
        client.on_serial_output = callback(src_addr, data: bytes)

    Configuração:
        client.mode = CossMode.ITA5           # modo de encapsulamento
        client.arq = True                     # ARQ (P2P) ou non-ARQ (multicast)
        client.dest_addr = 0x01               # endereço de destino
        client.flush_threshold = 512
        client.flush_on_crlf = True
        client.flush_timeout_s = 2.0
    """

    SAP_ID = SAP_ID_COSS  # 1

    def __init__(
        self,
        node,
        dest_addr: int = 0,
        mode: CossMode = CossMode.ITA5,
        arq: bool = True,
        connection_id: int = 0,
        flush_threshold: int = 512,
        flush_on_crlf: bool = True,
        flush_timeout_s: float = 2.0,
    ):
        super().__init__(node, connection_id)
        self.dest_addr = dest_addr
        self.mode = mode
        self.arq = arq
        self.on_serial_output: Callable[[int, bytes], None] | None = None

        self._flush_buf = _FlushBuffer(
            threshold=flush_threshold,
            flush_on_crlf=flush_on_crlf,
            flush_timeout_s=flush_timeout_s,
            on_flush=self._on_flush,
        )

    # ── Envio ─────────────────────────────────────────────────────────────────

    def feed_bytes(self, data: bytes, priority: int = 5, ttl_seconds: float = 120.0):
        """Injeta bytes no buffer de entrada; flush automático conforme disciplina."""
        self._pending_priority = priority
        self._pending_ttl = ttl_seconds
        self._flush_buf.feed(data)

    def flush(self):
        """Força transmissão imediata dos bytes pendentes."""
        self._flush_buf.flush_now()

    def tick(self):
        """Verifica timeout de flush; chame periodicamente (ex.: a cada 0,5 s)."""
        self._flush_buf.tick()

    def _on_flush(self, chunk: bytes):
        """Callback interno: encapsula e envia bloco de dados."""
        encoded = CharacterEncoder.encode(chunk, self.mode)
        priority = getattr(self, "_pending_priority", 5)
        ttl = getattr(self, "_pending_ttl", 120.0)
        self._send_data(
            dest_addr=self.dest_addr,
            dest_sap=self.SAP_ID,
            data=encoded,
            priority=priority,
            ttl_seconds=ttl,
            mode=DeliveryMode(arq_mode=self.arq),
        )
        logger.debug(
            "COSS → addr=%d modo=%s %d bytes (arq=%s)",
            self.dest_addr, self.mode.name, len(encoded), self.arq,
        )

    # ── Recepção ──────────────────────────────────────────────────────────────

    def _on_data_received(self, src_addr: int, data: bytes):
        """Decodifica dados recebidos e entrega ao fluxo serial de saída."""
        try:
            decoded = CharacterEncoder.decode(data, self.mode)
        except ValueError as exc:
            logger.warning("COSS ← addr=%d: erro ao decodificar: %s", src_addr, exc)
            return

        logger.debug("COSS ← addr=%d %d bytes (modo=%s)", src_addr, len(decoded), self.mode.name)
        if self.on_serial_output:
            self.on_serial_output(src_addr, decoded)
