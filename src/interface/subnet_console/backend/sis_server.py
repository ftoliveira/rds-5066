"""InstrumentedSisServer — a Raw SIS Socket Server (Annex F.16) that keeps a
live wire log and client roster for the console to display.

The console runs one :class:`~src.raw_sis_socket.RawSisSocketServer` per node so
external SIS clients can bind SAPs over TCP, exactly as ``chat_app_110d`` does.
This subclass adds *observability* on top of the production server without
changing its behaviour: every S-primitive that crosses the socket (both
directions) is recorded, and per-connection metadata (remote socket, bound SAP,
rank, connect time) is exposed for the "Connected Clients" table.

Threading: the server runs inside an asyncio loop on its own thread (see
:class:`~.node_controller.NodeController`). The instrumentation hooks
(``_dispatch_primitive`` / ``_send_raw``) therefore run on that asyncio thread;
the console reads the snapshots (:meth:`wire_rows` / :meth:`client_rows`) from
the GUI thread. We avoid locks by relying on CPython atomics:

* the wire log is a ``deque`` — ``append`` (asyncio thread) and ``list(dq)``
  (GUI thread) are each single, GIL-atomic operations;
* ``list(self._connections.items())`` snapshots the live-connection dict in one
  C-level call that never releases the GIL mid-iteration;
* ``prim_count`` is a plain ``int`` (atomic read/write).

``_since`` (our connect-time map) is only ever touched from the GUI thread — it
is populated lazily inside :meth:`client_rows`, so the recorded time is accurate
to one status poll (~0.5 s), which is plenty for a "connected since HH:MM:SS".
"""
from __future__ import annotations

import time
from collections import deque

from src.raw_sis_socket import RawSisSocketServer
from src.s_primitive_codec import HEADER_SIZE, decode_s_primitive
from src.stypes import SPrimitiveType

WIRE_CAP = 200          # ring-buffer depth of the SIS wire log


def _prim_name(prim_type: int) -> str:
    try:
        return SPrimitiveType(prim_type).name
    except ValueError:
        return f"TYPE {prim_type}"


class InstrumentedSisServer(RawSisSocketServer):
    """:class:`RawSisSocketServer` plus a wire log and client roster."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._wire: deque = deque(maxlen=WIRE_CAP)   # newest appended at the end
        self.prim_count = 0                          # total primitives observed
        self._since: dict[int, str] = {}             # conn_id -> connect time (GUI thread)

    # ------------------------------------------------------------- wire capture
    def _record(self, direction: str, prim_type: int, sap, size: int) -> None:
        self._wire.append({
            "time": time.strftime("%H:%M:%S"),
            "dir": direction,
            "name": _prim_name(prim_type),
            "sap": "—" if sap is None else str(sap),
            "size": int(size),
        })
        self.prim_count += 1

    def _dispatch_primitive(self, conn, prim_type, payload):
        # Inbound (client → server). The framed size is header + type byte +
        # payload; conn.bound_sap is still None during the bind handshake.
        self._record("C → S", prim_type, conn.bound_sap, HEADER_SIZE + 1 + len(payload))
        super()._dispatch_primitive(conn, prim_type, payload)

    def _send_raw(self, conn, data):
        # Outbound (server → client). ``data`` is an already-framed S-primitive;
        # decode just its type for the log (best effort).
        try:
            prim_type, _payload, _n = decode_s_primitive(data)
        except ValueError:
            prim_type = -1
        self._record("S → C", prim_type, conn.bound_sap, len(data))
        super()._send_raw(conn, data)

    # ---------------------------------------------------------------- snapshots
    def wire_rows(self) -> list:
        """The wire log, newest first (GUI thread)."""
        return list(reversed(list(self._wire)))

    def client_rows(self) -> list:
        """One plain dict per connected TCP client (GUI thread).

        ``sap``/``rank`` are ``None`` until the client binds a SAP; the console
        maps ``sap`` to an Annex F client name.
        """
        rows = []
        live_ids = set()
        for conn_id, conn in list(self._connections.items()):
            live_ids.add(conn_id)
            since = self._since.get(conn_id)
            if since is None:
                since = self._since[conn_id] = time.strftime("%H:%M:%S")
            peer = conn.peername
            if isinstance(peer, tuple) and len(peer) >= 2:
                remote = f"{peer[0]}:{peer[1]}"
            else:
                remote = str(peer)
            bound = conn.bound_sap is not None
            rows.append({
                "conn_id": conn_id, "remote": remote,
                "sap": conn.bound_sap, "rank": conn.rank if bound else None,
                "state": "BOUND" if bound else "CONNECTED", "since": since,
            })
        # Drop connect-times for connections that have since closed.
        for cid in [c for c in self._since if c not in live_ids]:
            del self._since[cid]
        rows.sort(key=lambda r: r["conn_id"])
        return rows
