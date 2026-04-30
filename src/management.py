"""Management D_PDU (Type 6) stop-and-wait engine per STANAG 5066 Edition 3 Annex C.3.9 / C.6.

Implements:
  - Stop-and-wait protocol for Management D_PDU exchange
  - DRC (Data Rate Change) request/response handshake (Types 1/2)
  - Automatic retransmission with configurable timeout
  - Repetition count per data rate (Table C-4)
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Callable, Optional

from src.arq import repetition_count_for_rate
from src.dpdu_frame import (
    build_management,
    decode_dpdu,
    dpdu_set_address,
    encode_dpdu,
)
from src.eow import (
    DRCResponseCode,
    DRCRefuseReason,
    build_eow_drc,
    build_eow_drc_response,
)
from src.stypes import DPDU, DPDUType, Address


# -------------------------------------------------------------------
# DRC management message codes (backward-compat, Edition 3 Table C-19)
# -------------------------------------------------------------------

class DRCCode(enum.IntEnum):
    DRC_REQUEST = 0x00
    DRC_ACCEPT = 0x01
    DRC_REFUSE = 0x02
    DRC_CANCEL = 0x03


# -------------------------------------------------------------------
# Management engine
# -------------------------------------------------------------------

MGMT_TIMEOUT_MS = 3000
MGMT_MAX_RETRIES = 3


@dataclass
class _PendingMgmt:
    """A management message awaiting ACK."""

    frame_id: int
    dpdu: DPDU
    encoded: bytes
    sent_at_ms: int
    retx_count: int = 0
    reps_remaining: int = 0


class ManagementEngine:
    """Stop-and-wait engine for Type 6 (Management) D_PDUs.

    Usage:
        eng = ManagementEngine(local_addr, remote_addr)
        eng.send(msg_type, msg_contents, data)
        # In the main loop:
        frames = eng.process_tx(current_ms)
        eng.process_rx(dpdu)
    """

    def __init__(
        self,
        local_node_address: int,
        remote_node_address: int,
        *,
        timeout_ms: int = MGMT_TIMEOUT_MS,
        max_retries: int = MGMT_MAX_RETRIES,
        data_rate_bps: int = 1200,
        long_interleave: bool = False,
        on_rx_callback: Optional[Callable[[DPDU], None]] = None,
    ) -> None:
        self.local_node_address = local_node_address
        self.remote_node_address = remote_node_address
        self.timeout_ms = timeout_ms
        self.max_retries = max_retries
        self.data_rate_bps = data_rate_bps
        self.long_interleave = long_interleave
        self.on_rx_callback = on_rx_callback

        self._next_frame_id: int = 0
        self._pending: Optional[_PendingMgmt] = None
        self._tx_queue: list[tuple[int, int, bytes, int]] = []  # (msg_type, msg_contents, data, frame_id)
        # RX duplicate detection per C.3.9§(21-25)
        self._rx_management_frame_id: Optional[int] = None

    # -- public API --

    def send(self, msg_type: int, msg_contents: int, data: bytes = b"") -> int:
        """Enqueue a management message.  Returns the frame_id that will be used."""
        fid = self._next_frame_id
        self._next_frame_id = (self._next_frame_id + 1) & 0xFF
        self._tx_queue.append((msg_type, msg_contents, data, fid))
        return fid

    def send_drc_request(self, data_rate_code: int, long_interleave: bool = False,
                         interleave_mode: Optional[int] = None) -> int:
        """Send a DRC REQUEST management message (EOW Type 1)."""
        eow = build_eow_drc(data_rate_code, long_interleave, interleave_mode)
        msg_type = eow & 0x0F
        msg_contents = (eow >> 4) & 0xFF
        return self.send(msg_type, msg_contents)

    def send_drc_response(self, response: int, reason: int = 0) -> int:
        """Send a DRC RESPONSE management message (EOW Type 2)."""
        eow = build_eow_drc_response(response, reason)
        msg_type = eow & 0x0F
        msg_contents = (eow >> 4) & 0xFF
        return self.send(msg_type, msg_contents)

    @property
    def is_busy(self) -> bool:
        """True if waiting for an ACK."""
        return self._pending is not None

    def process_tx(self, current_time_ms: int) -> list[bytes]:
        """Return management frames to transmit (stop-and-wait)."""
        frames: list[bytes] = []

        # Retransmit pending if timed out
        if self._pending is not None:
            elapsed = current_time_ms - self._pending.sent_at_ms
            if elapsed >= self.timeout_ms:
                self._pending.retx_count += 1
                if self._pending.retx_count > self.max_retries:
                    # Give up
                    self._pending = None
                else:
                    self._pending.sent_at_ms = current_time_ms
                    reps = repetition_count_for_rate(self.data_rate_bps, self.long_interleave)
                    for _ in range(reps):
                        frames.append(self._pending.encoded)
            return frames

        # Start next queued message
        if self._tx_queue:
            msg_type, msg_contents, data, fid = self._tx_queue.pop(0)
            addr = dpdu_set_address(
                destination=self.remote_node_address,
                source=self.local_node_address,
            )
            dpdu = build_management(
                0, 0, addr,
                msg_type=msg_type,
                message_contents=msg_contents,
                data=data,
                message_ack=False,
                management_frame_id=fid,
            )
            enc = encode_dpdu(dpdu)
            reps = repetition_count_for_rate(self.data_rate_bps, self.long_interleave)
            self._pending = _PendingMgmt(
                frame_id=fid,
                dpdu=dpdu,
                encoded=enc,
                sent_at_ms=current_time_ms,
                reps_remaining=reps - 1,
            )
            for _ in range(reps):
                frames.append(enc)

        return frames

    def process_rx(self, dpdu: DPDU) -> list[bytes]:
        """Process a received Management D_PDU.

        If it's an ACK matching our pending frame_id, clear pending.
        If it's a new management message, deliver to callback and send ACK.
        Duplicate frames (same frame_id) are ACKed but not re-delivered (C.3.9§21-25).
        Returns list of response frames (ACKs) to send.
        """
        if dpdu.dpdu_type is not DPDUType.MANAGEMENT or dpdu.management is None:
            return []

        mgmt = dpdu.management
        responses: list[bytes] = []

        # Is this an ACK for our pending?
        if mgmt.message_ack:
            if self._pending and mgmt.management_frame_id == self._pending.frame_id:
                self._pending = None
            return responses

        # Duplicate detection per C.3.9§(21-25)
        is_duplicate = (
            self._rx_management_frame_id is not None
            and mgmt.management_frame_id == self._rx_management_frame_id
        )

        if not is_duplicate:
            # New management message from peer — deliver to callback
            self._rx_management_frame_id = mgmt.management_frame_id
            if self.on_rx_callback:
                self.on_rx_callback(dpdu)

        # Build ACK (always ACK, even duplicates)
        # C.3.9§(7-8): ACK-only has valid_message=False, no extended message
        addr = dpdu_set_address(
            destination=self.remote_node_address,
            source=self.local_node_address,
        )
        ack_dpdu = build_management(
            0, 0, addr,
            msg_type=0,
            message_contents=0,
            data=b"",
            message_ack=True,
            valid_message=False,
            management_frame_id=mgmt.management_frame_id,
        )
        responses.append(encode_dpdu(ack_dpdu))
        return responses
