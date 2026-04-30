import asyncio
import pytest
from unittest.mock import MagicMock
from src.raw_sis_socket import RawSisSocketServer
from src.s_primitive_codec import (
    encode_bind_request, encode_hard_link_establish,
    encode_hard_link_terminate,
    decode_s_primitive,
)
from src.stypes import SPrimitiveType


def _make_mock_node(prebound_saps=None):
    """Build a mock StanagNode with realistic _callbacks and _saps attributes."""
    node = MagicMock()
    node.bind.return_value = 0
    node._saps = dict(prebound_saps) if prebound_saps else {}
    callbacks = MagicMock()
    callbacks.unidata_indication = None
    node._callbacks = callbacks
    return node


@pytest.fixture
def mock_node():
    return _make_mock_node()

@pytest.mark.asyncio
async def test_hard_link_establish_calls_node(mock_node):
    server = RawSisSocketServer(mock_node, host='127.0.0.1', port=15700)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', 15700)
        writer.write(encode_bind_request(sap_id=0, rank=0))
        await writer.drain()
        await asyncio.sleep(0.1)
        writer.write(encode_hard_link_establish(
            link_type=0, link_priority=5, remote_sap=0, remote_node=0x456
        ))
        await writer.drain()
        await asyncio.sleep(0.1)
        mock_node.hard_link_establish.assert_called_once()
        call_kwargs = mock_node.hard_link_establish.call_args
        # remote_addr should be 0x456
        args = call_kwargs[1] if call_kwargs[1] else {}
        positional = call_kwargs[0] if call_kwargs[0] else ()
        remote_addr = args.get('remote_addr', positional[2] if len(positional) > 2 else None)
        assert remote_addr == 0x456
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

@pytest.mark.asyncio
async def test_hard_link_terminate_calls_node(mock_node):
    server = RawSisSocketServer(mock_node, host='127.0.0.1', port=15701)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', 15701)
        writer.write(encode_bind_request(sap_id=0, rank=0))
        await writer.drain()
        await asyncio.sleep(0.1)
        writer.write(encode_hard_link_terminate(remote_node=0x456))
        await writer.drain()
        await asyncio.sleep(0.1)
        mock_node.hard_link_terminate.assert_called_once()
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_hard_link_established_sends_primitive_to_client(mock_node):
    """When node fires hard_link_established callback, client receives S_HARD_LINK_ESTABLISHED."""
    server = RawSisSocketServer(mock_node, host='127.0.0.1', port=15702)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', 15702)
        # Bind first
        writer.write(encode_bind_request(sap_id=0, rank=0))
        await writer.drain()
        await asyncio.sleep(0.1)
        # Drain S_BIND_ACCEPTED
        await asyncio.wait_for(reader.read(64), timeout=0.5)

        # Get the registered callback from mock_node.register_callbacks
        hl_established_cb = None
        for call in mock_node.register_callbacks.call_args_list:
            if 'hard_link_established' in call.kwargs:
                hl_established_cb = call.kwargs['hard_link_established']
        assert hl_established_cb is not None, "hard_link_established callback not registered"

        # Fire the callback as the node would
        hl_established_cb(0x456, 0)  # remote_addr=0x456, remote_sap=0

        # Read response from server
        data = await asyncio.wait_for(reader.read(64), timeout=0.5)
        prim_type, payload, _ = decode_s_primitive(data)
        assert prim_type == SPrimitiveType.S_HARD_LINK_ESTABLISHED

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_hard_link_terminated_sends_primitive_to_client(mock_node):
    """When node fires hard_link_terminated callback, client receives S_HARD_LINK_TERMINATED."""
    server = RawSisSocketServer(mock_node, host='127.0.0.1', port=15703)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', 15703)
        # Bind first
        writer.write(encode_bind_request(sap_id=0, rank=0))
        await writer.drain()
        await asyncio.sleep(0.1)
        # Drain S_BIND_ACCEPTED
        await asyncio.wait_for(reader.read(64), timeout=0.5)

        # Get the registered callback from mock_node.register_callbacks
        hl_terminated_cb = None
        for call in mock_node.register_callbacks.call_args_list:
            if 'hard_link_terminated' in call.kwargs:
                hl_terminated_cb = call.kwargs['hard_link_terminated']
        assert hl_terminated_cb is not None, "hard_link_terminated callback not registered"

        # Fire the callback as the node would (signature: remote_addr, initiator_received_confirm)
        hl_terminated_cb(0x456, False)  # remote_addr=0x456, initiator_received_confirm=False

        # Read response from server
        data = await asyncio.wait_for(reader.read(64), timeout=0.5)
        prim_type, payload, _ = decode_s_primitive(data)
        assert prim_type == SPrimitiveType.S_HARD_LINK_TERMINATED

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_hard_link_rejected_sends_primitive_to_client(mock_node):
    server = RawSisSocketServer(mock_node, host='127.0.0.1', port=15705)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', 15705)
        writer.write(encode_bind_request(sap_id=0, rank=0))
        await writer.drain()
        await asyncio.sleep(0.1)
        await asyncio.wait_for(reader.read(64), timeout=0.5)

        on_rejected_cb = None
        for call in mock_node.register_callbacks.call_args_list:
            if 'hard_link_rejected' in call.kwargs:
                on_rejected_cb = call.kwargs['hard_link_rejected']
        assert on_rejected_cb is not None

        on_rejected_cb(0x456, 0, 1)  # remote_addr, remote_sap, reason=1
        data = await asyncio.wait_for(reader.read(64), timeout=0.5)
        from src.s_primitive_codec import decode_s_primitive
        prim_type, _, _ = decode_s_primitive(data)
        assert prim_type == SPrimitiveType.S_HARD_LINK_REJECTED

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_hard_link_indication_sends_primitive_to_client(mock_node):
    server = RawSisSocketServer(mock_node, host='127.0.0.1', port=15706)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', 15706)
        writer.write(encode_bind_request(sap_id=0, rank=0))
        await writer.drain()
        await asyncio.sleep(0.1)
        await asyncio.wait_for(reader.read(64), timeout=0.5)

        on_indication_cb = None
        for call in mock_node.register_callbacks.call_args_list:
            if 'hard_link_indication' in call.kwargs:
                on_indication_cb = call.kwargs['hard_link_indication']
        assert on_indication_cb is not None

        # node fires: (remote_addr, remote_sap, link_priority, link_type)
        on_indication_cb(0x456, 0, 5, 0)
        data = await asyncio.wait_for(reader.read(64), timeout=0.5)
        from src.s_primitive_codec import decode_s_primitive
        prim_type, _, _ = decode_s_primitive(data)
        assert prim_type == SPrimitiveType.S_HARD_LINK_INDICATION

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Compatibility tests: HFCHAT via Raw SIS socket
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bind_accepted_when_sap_prebound_by_host():
    """Bug fix: external HFCHAT client gets S_BIND_ACCEPTED even if the host app
    already pre-bound SAP 5 directly on the StanagNode (chat_app_sis pattern)."""
    node = _make_mock_node(prebound_saps={5: object()})
    node.bind.side_effect = ValueError("SAP 5 já está vinculado")

    server = RawSisSocketServer(node, host='127.0.0.1', port=15710)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', 15710)
        writer.write(encode_bind_request(sap_id=5, rank=0))
        await writer.drain()
        await asyncio.sleep(0.1)

        data = await asyncio.wait_for(reader.read(64), timeout=0.5)
        prim_type, _, _ = decode_s_primitive(data)
        assert prim_type == SPrimitiveType.S_BIND_ACCEPTED, (
            "External client must receive S_BIND_ACCEPTED when SAP is already "
            "bound by the host app — not BIND_REJECTED."
        )
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_host_unidata_callback_preserved_after_external_bind():
    """Bug fix: the host app's unidata_indication callback must still be called
    for indications on SAPs not owned by the external socket client."""
    node = _make_mock_node()

    host_calls = []

    def host_unidata(indication):
        host_calls.append(indication)

    # Simulate host app having registered its own callback on the node
    node._callbacks.unidata_indication = host_unidata

    server = RawSisSocketServer(node, host='127.0.0.1', port=15711)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', 15711)
        # External client binds SAP 0
        writer.write(encode_bind_request(sap_id=0, rank=0))
        await writer.drain()
        await asyncio.sleep(0.1)
        await asyncio.wait_for(reader.read(64), timeout=0.5)  # drain BIND_ACCEPTED

        # Retrieve the installed unidata callback
        unidata_cb = None
        for call in node.register_callbacks.call_args_list:
            if 'unidata_indication' in call.kwargs:
                unidata_cb = call.kwargs['unidata_indication']
        assert unidata_cb is not None, "unidata_indication callback was not installed"

        # Indication for SAP 5 (not owned by external client) must reach host callback
        fake_indication = MagicMock()
        fake_indication.dest_sap = 5
        unidata_cb(fake_indication)

        assert len(host_calls) == 1, (
            "Host app callback was not called — original_unidata chaining is broken."
        )
        assert host_calls[0] is fake_indication

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_unidata_request_node_delivery_confirm_mapped_correctly():
    """Bug fix: S_UNIDATA_REQUEST with delivery_confirm=1 (NODE DELIVERY) must set
    node_delivery_confirm=True on DeliveryMode — not client_delivery_confirm.
    Required for HFCHAT point-to-point (Abordagem 1, SAP 5, ARQ + NODE DELIVERY)."""
    from src.s_primitive_codec import encode_unidata_request, encode_delivery_mode

    node = _make_mock_node()

    server = RawSisSocketServer(node, host='127.0.0.1', port=15712)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', 15712)
        writer.write(encode_bind_request(sap_id=5, rank=0))
        await writer.drain()
        await asyncio.sleep(0.1)
        await asyncio.wait_for(reader.read(64), timeout=0.5)  # drain BIND_ACCEPTED

        # delivery_confirm=1 → NODE DELIVERY (A.2.2.28.2 bits [3:2] = 01)
        dm_byte = encode_delivery_mode(tx_mode=0, delivery_confirm=1)[0]
        writer.write(encode_unidata_request(
            priority=10, dest_sap=5, dest_addr=2,
            delivery_mode_byte=dm_byte, ttl=120,
            updu=b"hello\r\n",
        ))
        await writer.drain()
        await asyncio.sleep(0.1)

        assert node.unidata_request.called, "node.unidata_request was not called"
        call_kwargs = node.unidata_request.call_args.kwargs
        mode = call_kwargs['mode']
        assert mode.node_delivery_confirm is True, (
            "node_delivery_confirm must be True for delivery_confirm=1 (NODE DELIVERY) — "
            "S_UNIDATA_REQUEST_CONFIRM will never reach HFCHAT client otherwise."
        )
        assert mode.client_delivery_confirm is False

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
