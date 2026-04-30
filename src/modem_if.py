"""Simulated synchronous modem interface for STANAG 5066 phase 1."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(slots=True)
class ModemConfig:
    data_rate_bps: int = 1200
    tx_enable: bool = False
    rx_carrier_detect: bool = False
    max_buffer_bytes: int = 8192


@dataclass(slots=True)
class ModemInterface:
    """Byte-oriented stand-in for the Annex D synchronous modem interface."""

    config: ModemConfig = field(default_factory=ModemConfig)
    _tx_enabled: bool = field(init=False, repr=False)
    _rx_started: bool = field(init=False, repr=False)
    _peer: "ModemInterface | None" = field(init=False, default=None, repr=False)
    _rx_bytes: bytearray = field(init=False, repr=False)
    _rx_frames: deque[bytes] = field(init=False, repr=False)
    _tx_frames: deque[bytes] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._tx_enabled = self.config.tx_enable
        self._rx_started = False
        self._peer: ModemInterface | None = None
        self._rx_bytes = bytearray()
        self._rx_frames: deque[bytes] = deque()
        self._tx_frames: deque[bytes] = deque()

    @classmethod
    def loopback(cls, config: ModemConfig | None = None) -> "ModemInterface":
        modem = cls(config=config or ModemConfig())
        modem.connect(modem)
        return modem

    def connect(self, peer: "ModemInterface") -> None:
        self._peer = peer

    def modem_init(self, config: ModemConfig) -> None:
        self.config = config
        self._tx_enabled = config.tx_enable

    def modem_set_tx_enable(self, enabled: bool) -> None:
        self._tx_enabled = bool(enabled)

    def modem_get_carrier_status(self) -> bool:
        if not self._rx_started:
            return False
        return bool(self._rx_bytes or self._rx_frames)

    def modem_rx_start(self) -> None:
        self._rx_started = True

    def modem_rx_stop(self) -> None:
        self._rx_started = False

    def modem_tx_dpdu(self, dpdu_buffer: bytes, length: int | None = None) -> int:
        if not self._tx_enabled:
            self.modem_set_tx_enable(True)
        if self._peer is None:
            raise RuntimeError("Modem interface is not connected")

        payload = bytes(dpdu_buffer[:length] if length is not None else dpdu_buffer)
        if len(payload) > self.config.max_buffer_bytes:
            raise ValueError("Frame exceeds configured modem buffer")

        self._tx_frames.append(payload)
        self._peer._receive_frame(payload)
        return len(payload)

    def modem_tx_burst(self, frames: list[bytes]) -> int:
        """Transmite múltiplos D_PDUs em um único 'transmission interval' (Annex C.3).

        Na simulação, cada frame é entregue individualmente ao peer, mas todos
        pertencem ao mesmo burst — o EOT do primeiro D_PDU cobre toda a duração.
        """
        total = 0
        for frame in frames:
            total += self.modem_tx_dpdu(frame)
        return total

    def modem_rx_read(self, max_len: int) -> bytes:
        if max_len <= 0:
            return b""
        data = bytes(self._rx_bytes[:max_len])
        del self._rx_bytes[:max_len]
        return data

    def modem_rx_read_frame(self) -> bytes | None:
        if not self._rx_frames:
            return None
        frame = self._rx_frames.popleft()
        size = len(frame)
        del self._rx_bytes[:size]
        return frame

    def clear(self) -> None:
        self._rx_bytes.clear()
        self._rx_frames.clear()
        self._tx_frames.clear()

    def _receive_frame(self, frame: bytes) -> None:
        if not self._rx_started:
            return
        if len(self._rx_bytes) + len(frame) > self.config.max_buffer_bytes:
            raise BufferError("RX buffer overflow in simulated modem interface")
        self._rx_frames.append(frame)
        self._rx_bytes.extend(frame)
