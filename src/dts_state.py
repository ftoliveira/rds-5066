"""DTS State Machine per STANAG 5066 Edition 3, Annex C.6.1.

8 states per peer link (connected / unconnected variants):

  IDLE_UNCONNECTED        — No ARQ link, no connection.
  DATA_UNCONNECTED        — ARQ active, no logical connection.
  EXPEDITED_UNCONNECTED   — Expedited exchange, no connection.
  MANAGEMENT_UNCONNECTED  — Management exchange, no connection.
  IDLE_CONNECTED          — No ARQ link, connection established.
  DATA_CONNECTED          — ARQ active, connected.
  EXPEDITED_CONNECTED     — Expedited exchange, connected.
  MANAGEMENT_CONNECTED    — Management exchange, connected.

Tables C-10 through C-25 define which D_PDU types are valid in each state.
Invalid D_PDUs generate WARNING D_PDUs with reason codes per spec.
"""

from __future__ import annotations

import enum
from typing import Optional

from src.stypes import DPDUType


class DTSState(enum.Enum):
    """DTS peer-link states (Annex C.6.1.1, Edition 3)."""

    IDLE_UNCONNECTED = "IDLE_UNCONNECTED"
    DATA_UNCONNECTED = "DATA_UNCONNECTED"
    EXPEDITED_UNCONNECTED = "EXPEDITED_UNCONNECTED"
    MANAGEMENT_UNCONNECTED = "MANAGEMENT_UNCONNECTED"
    IDLE_CONNECTED = "IDLE_CONNECTED"
    DATA_CONNECTED = "DATA_CONNECTED"
    EXPEDITED_CONNECTED = "EXPEDITED_CONNECTED"
    MANAGEMENT_CONNECTED = "MANAGEMENT_CONNECTED"

    # --- backward-compat aliases for external code using old names ---
    @classmethod
    def _missing_(cls, value: object) -> Optional[DTSState]:
        aliases = {
            "IDLE": cls.IDLE_UNCONNECTED,
            "DATA": cls.DATA_CONNECTED,
            "MANAGEMENT": cls.MANAGEMENT_CONNECTED,
            "EXPEDITED": cls.EXPEDITED_CONNECTED,
        }
        if isinstance(value, str) and value in aliases:
            return aliases[value]
        return None

    @property
    def is_connected(self) -> bool:
        return self in _CONNECTED_STATES

    @property
    def is_idle(self) -> bool:
        return self in (DTSState.IDLE_UNCONNECTED, DTSState.IDLE_CONNECTED)

    @property
    def is_data(self) -> bool:
        return self in (DTSState.DATA_UNCONNECTED, DTSState.DATA_CONNECTED)

    @property
    def is_management(self) -> bool:
        return self in (DTSState.MANAGEMENT_UNCONNECTED, DTSState.MANAGEMENT_CONNECTED)

    @property
    def is_expedited(self) -> bool:
        return self in (DTSState.EXPEDITED_UNCONNECTED, DTSState.EXPEDITED_CONNECTED)


_CONNECTED_STATES = frozenset({
    DTSState.IDLE_CONNECTED,
    DTSState.DATA_CONNECTED,
    DTSState.EXPEDITED_CONNECTED,
    DTSState.MANAGEMENT_CONNECTED,
})


# ---------------------------------------------------------------------------
# WARNING reason codes (C.6.1, Tables C-10 to C-25)
# ---------------------------------------------------------------------------

WARNING_REASON_UNRECOGNIZED_TYPE = 0          # Unrecognised D_PDU type Received
WARNING_REASON_CONN_DPDU_BUT_UNCONNECTED = 1  # Connection-related D_PDU while Unconnected
WARNING_REASON_INVALID_DPDU = 2               # Invalid D_PDU Received (generic)
WARNING_REASON_INVALID_DPDU_FOR_STATE = 3     # Invalid D_PDU Received for Current State

# Backward-compat alias (nome antigo era enganoso — apontava para 2 mas a
# norma define reason 2 como "Invalid D_PDU Received" genérico).
WARNING_REASON_UNCONNECTED_DPDU_REQUIRES_LINK = WARNING_REASON_INVALID_DPDU

# D_PDU types that are connection-related (require a connection to be valid)
_CONNECTION_DPDUS = frozenset({
    DPDUType.DATA_ONLY,
    DPDUType.ACK_ONLY,
    DPDUType.DATA_ACK,
    DPDUType.RESETWIN_RESYNC,
    DPDUType.EXPEDITED_DATA_ONLY,
    DPDUType.EXPEDITED_ACK_ONLY,
    DPDUType.MANAGEMENT,
})


# ---------------------------------------------------------------------------
# Which D_PDU types are accepted in each state (C.6.1.2, Tables C-10..C-25)
# ---------------------------------------------------------------------------

# Common types always accepted in all states
_ALWAYS_ALLOWED = frozenset({
    DPDUType.NON_ARQ,
    DPDUType.EXPEDITED_NON_ARQ,
    DPDUType.WARNING,
})

_ALLOWED: dict[DTSState, frozenset[DPDUType]] = {
    # --- UNCONNECTED states ---
    # Per Tables C-11, C-13, C-15: connection-related D_PDUs (Types 0-5)
    # generate WARNING reason=1 in UNCONNECTED states.
    # Only Types 6 (MANAGEMENT), 7, 8, 15 are allowed.
    DTSState.IDLE_UNCONNECTED: _ALWAYS_ALLOWED | {
        DPDUType.MANAGEMENT,
    },
    DTSState.DATA_UNCONNECTED: _ALWAYS_ALLOWED | {
        DPDUType.MANAGEMENT,
    },
    DTSState.EXPEDITED_UNCONNECTED: _ALWAYS_ALLOWED | {
        DPDUType.MANAGEMENT,
    },
    DTSState.MANAGEMENT_UNCONNECTED: _ALWAYS_ALLOWED | {
        DPDUType.MANAGEMENT,
    },
    # --- CONNECTED states ---
    DTSState.IDLE_CONNECTED: _ALWAYS_ALLOWED | {
        DPDUType.RESETWIN_RESYNC,
    },
    DTSState.DATA_CONNECTED: _ALWAYS_ALLOWED | {
        DPDUType.DATA_ONLY,
        DPDUType.ACK_ONLY,
        DPDUType.DATA_ACK,
        DPDUType.RESETWIN_RESYNC,
        DPDUType.MANAGEMENT,
        DPDUType.EXPEDITED_DATA_ONLY,
        DPDUType.EXPEDITED_ACK_ONLY,
    },
    # C.6.1 / Tabela C-20: durante EXPEDITED data exchange, transmissão de
    # dados regulares (Tipos 0/1/2) deve ser suspensa. Apenas Expedited
    # (4/5), MGMT (6), Non-ARQ (7/8), WARNING (15) e RESET (3) são válidos.
    DTSState.EXPEDITED_CONNECTED: _ALWAYS_ALLOWED | {
        DPDUType.EXPEDITED_DATA_ONLY,
        DPDUType.EXPEDITED_ACK_ONLY,
        DPDUType.MANAGEMENT,
        DPDUType.RESETWIN_RESYNC,
    },
    DTSState.MANAGEMENT_CONNECTED: _ALWAYS_ALLOWED | {
        DPDUType.MANAGEMENT,
    },
}


class DTSStateMachine:
    """Tracks the DTS state for one peer link per Edition 3 C.6.1.

    The node should consult this before dispatching incoming D_PDUs
    and use the transition helpers when CAS link events occur.
    """

    def __init__(self) -> None:
        self._state = DTSState.IDLE_UNCONNECTED
        self._prev_state: Optional[DTSState] = None

    # -- queries --

    @property
    def state(self) -> DTSState:
        return self._state

    def is_allowed(self, dpdu_type: DPDUType) -> bool:
        """Return True if *dpdu_type* is accepted in the current state."""
        return dpdu_type in _ALLOWED.get(self._state, frozenset())

    def warning_reason(self, dpdu_type: DPDUType | int) -> Optional[int]:
        """Return the WARNING reason code if *dpdu_type* is invalid, else None.

        Tabela C-3:
          0 — Unrecognised D_PDU type Received (tipo numérico desconhecido)
          1 — Connection-related D_PDU Received While Not Currently Connected
          2 — Invalid D_PDU Received (genérico)
          3 — Invalid D_PDU Received for Current State
        """
        # Type não reconhecido (fora do enum DPDUType) → reason 0.
        try:
            dpdu_enum = DPDUType(int(dpdu_type))
        except ValueError:
            return WARNING_REASON_UNRECOGNIZED_TYPE
        if self.is_allowed(dpdu_enum):
            return None
        # Connection-related D_PDU mas estamos desconectados → reason 1.
        if not self._state.is_connected and dpdu_enum in _CONNECTION_DPDUS:
            return WARNING_REASON_CONN_DPDU_BUT_UNCONNECTED
        return WARNING_REASON_INVALID_DPDU_FOR_STATE

    # -- transitions --

    def on_link_made(self) -> None:
        """CAS reports link established → enter IDLE_CONNECTED (Table C-10)."""
        self._state = DTSState.IDLE_CONNECTED

    def enter_data(self) -> None:
        """Transition IDLE_CONNECTED → DATA_CONNECTED when ARQ data is queued."""
        if self._state == DTSState.IDLE_CONNECTED:
            self._state = DTSState.DATA_CONNECTED

    def on_connection_made(self) -> None:
        """Logical connection established → move to connected variant."""
        _map = {
            DTSState.IDLE_UNCONNECTED: DTSState.IDLE_CONNECTED,
            DTSState.DATA_UNCONNECTED: DTSState.DATA_CONNECTED,
            DTSState.EXPEDITED_UNCONNECTED: DTSState.EXPEDITED_CONNECTED,
            DTSState.MANAGEMENT_UNCONNECTED: DTSState.MANAGEMENT_CONNECTED,
        }
        self._state = _map.get(self._state, self._state)

    def on_connection_lost(self) -> None:
        """Logical connection lost → move to unconnected variant."""
        _map = {
            DTSState.IDLE_CONNECTED: DTSState.IDLE_UNCONNECTED,
            DTSState.DATA_CONNECTED: DTSState.DATA_UNCONNECTED,
            DTSState.EXPEDITED_CONNECTED: DTSState.EXPEDITED_UNCONNECTED,
            DTSState.MANAGEMENT_CONNECTED: DTSState.MANAGEMENT_UNCONNECTED,
        }
        self._state = _map.get(self._state, self._state)

    def on_link_broken(self) -> None:
        """CAS reports link broken → return to IDLE_UNCONNECTED."""
        self._state = DTSState.IDLE_UNCONNECTED
        self._prev_state = None

    def enter_management(self) -> None:
        """Suspend current state, enter MANAGEMENT variant."""
        if self._state.is_data:
            self._prev_state = self._state
        if self._state.is_connected:
            self._state = DTSState.MANAGEMENT_CONNECTED
        else:
            self._state = DTSState.MANAGEMENT_UNCONNECTED

    def exit_management(self) -> None:
        """Management exchange finished → return to previous state."""
        if self._prev_state is not None:
            self._state = self._prev_state
            self._prev_state = None
        elif self._state.is_connected:
            self._state = DTSState.DATA_CONNECTED
        else:
            self._state = DTSState.DATA_UNCONNECTED

    def enter_expedited(self) -> None:
        """Enter EXPEDITED variant for types 4/5."""
        if self._state.is_data:
            self._prev_state = self._state
        if self._state.is_connected:
            self._state = DTSState.EXPEDITED_CONNECTED
        else:
            self._state = DTSState.EXPEDITED_UNCONNECTED

    def exit_expedited(self) -> None:
        """Expedited exchange finished → return to previous state."""
        if self._prev_state is not None:
            self._state = self._prev_state
            self._prev_state = None
        elif self._state.is_connected:
            self._state = DTSState.DATA_CONNECTED
        else:
            self._state = DTSState.DATA_UNCONNECTED
