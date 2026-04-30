"""DRC (Data Rate Change) Protocol per STANAG 5066 Edition 3 Annex C.6.4.

Implements the stateful DRC negotiation protocol with roles (MASTER/SLAVE),
per Figure C-50 and Table C-27.

DRC Flow (normal accept):
  MASTER →  DRC Request (Type 1)   [msg 1]
  SLAVE  →  DRC Response (Type 2)  [msg 3, accept]
  MASTER →  DT_ACK                 [msg 4]
  MASTER →  DRC Confirm (Type 2)   [msg 5]
  SLAVE  →  DT_ACK                 [msg 6]

Per C.6.4.1: DRC connections initiate at 300 bps, short interleave.
"""

from __future__ import annotations

import enum
from typing import Callable, Optional

from src.eow import (
    DRCResponseCode,
    DRCRefuseReason,
    InterleaveMode,
    build_eow_drc,
    build_eow_drc_response,
    parse_eow,
    EOWType,
)


class DRCRole(enum.Enum):
    MASTER = "MASTER"
    SLAVE = "SLAVE"


class DRCState(enum.Enum):
    IDLE = "IDLE"
    DRC_INITIATE = "DRC_INITIATE"
    DRC_WAIT_RESPONSE = "DRC_WAIT_RESPONSE"
    DRC_WAIT_ACK = "DRC_WAIT_ACK"
    DRC_CONFIRM = "DRC_CONFIRM"
    DRC_WAIT_CONFIRM_ACK = "DRC_WAIT_CONFIRM_ACK"


# Default timeout for DRC exchanges (ms)
DRC_TIMEOUT_MS = 10_000
# Initial DRC rate per C.6.4.1
DRC_INITIAL_RATE_BPS = 300
DRC_INITIAL_INTERLEAVE = InterleaveMode.SHORT


class DRCProtocol:
    """DRC negotiation state machine per C.6.4.2.

    This class manages the DRC handshake. It is driven by the ManagementEngine
    for actual frame transmission (stop-and-wait transport).

    Callbacks:
      on_drc_complete(rate_code, interleave_mode): called when DRC completes successfully
      on_drc_failed(reason): called when DRC fails or times out
    """

    def __init__(
        self,
        *,
        on_drc_complete: Optional[Callable[[int, int], None]] = None,
        on_drc_failed: Optional[Callable[[str], None]] = None,
        timeout_ms: int = DRC_TIMEOUT_MS,
    ) -> None:
        self.on_drc_complete = on_drc_complete
        self.on_drc_failed = on_drc_failed
        self.timeout_ms = timeout_ms

        self._state = DRCState.IDLE
        self._role: Optional[DRCRole] = None
        self._requested_rate: int = 0
        self._requested_interleave: int = InterleaveMode.SHORT
        self._start_time_ms: int = 0

    @property
    def state(self) -> DRCState:
        return self._state

    @property
    def role(self) -> Optional[DRCRole]:
        return self._role

    def initiate(self, rate_code: int, interleave_mode: int = InterleaveMode.SHORT) -> int:
        """MASTER initiates DRC. Returns the EOW field to embed in MGMT D_PDU."""
        self._state = DRCState.DRC_INITIATE
        self._role = DRCRole.MASTER
        self._requested_rate = rate_code
        self._requested_interleave = interleave_mode
        return build_eow_drc(rate_code, interleave_mode=interleave_mode)

    def on_rx_eow(self, eow: int, current_time_ms: int) -> Optional[int]:
        """Process a received EOW field from a Management D_PDU.

        Returns an EOW field to send as response, or None if no response needed.
        """
        msg = parse_eow(eow)

        if msg.msg_type == EOWType.DRC_REQUEST:
            if self._state == DRCState.IDLE:
                # We become SLAVE
                self._role = DRCRole.SLAVE
                self._requested_rate = msg.drc_request.data_rate_code
                self._requested_interleave = msg.drc_request.interleave_mode
                self._start_time_ms = current_time_ms
                self._state = DRCState.DRC_WAIT_ACK
                # Auto-accept (implementation policy)
                return build_eow_drc_response(DRCResponseCode.ACCEPT)
            else:
                # Already in DRC, refuse
                return build_eow_drc_response(DRCResponseCode.REFUSE, DRCRefuseReason.BUSY)

        elif msg.msg_type == EOWType.DRC_RESPONSE:
            if self._role == DRCRole.MASTER and self._state == DRCState.DRC_INITIATE:
                if msg.drc_response.response == DRCResponseCode.ACCEPT:
                    self._state = DRCState.DRC_CONFIRM
                    # Send confirm
                    return build_eow_drc_response(DRCResponseCode.CONFIRM)
                elif msg.drc_response.response == DRCResponseCode.REFUSE:
                    self._state = DRCState.IDLE
                    self._role = None
                    if self.on_drc_failed:
                        self.on_drc_failed(f"refused: reason={msg.drc_response.reason}")
                    return None
                elif msg.drc_response.response == DRCResponseCode.CANCEL:
                    self._state = DRCState.IDLE
                    self._role = None
                    if self.on_drc_failed:
                        self.on_drc_failed("cancelled")
                    return None
                elif msg.drc_response.response == DRCResponseCode.CONFIRM:
                    # Confirm from slave after our confirm
                    self._complete()
                    return None

            elif self._role == DRCRole.SLAVE and self._state == DRCState.DRC_WAIT_ACK:
                if msg.drc_response.response == DRCResponseCode.CONFIRM:
                    self._complete()
                    return None

        return None

    def check_timeout(self, current_time_ms: int) -> None:
        """Check if the DRC exchange has timed out."""
        if self._state != DRCState.IDLE and self._start_time_ms > 0:
            if current_time_ms - self._start_time_ms > self.timeout_ms:
                self._state = DRCState.IDLE
                self._role = None
                if self.on_drc_failed:
                    self.on_drc_failed("timeout")

    def _complete(self) -> None:
        """DRC negotiation completed successfully."""
        rate = self._requested_rate
        interleave = self._requested_interleave
        self._state = DRCState.IDLE
        self._role = None
        if self.on_drc_complete:
            self.on_drc_complete(rate, interleave)
