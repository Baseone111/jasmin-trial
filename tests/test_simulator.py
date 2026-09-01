import asyncio
from contextlib import suppress

import pytest
from smpp.pdu.operations import BindTransceiver, DeliverSM, EnquireLink, SubmitSM
from smpp.pdu.pdu_types import CommandStatus, MessageState

from simulator.server import ENCODER, Session, read_pdu


async def exercise(suffix,password=b"sim-pass",bind=True):
    tasks=set()
    async def connected(reader,writer):
        task=asyncio.current_task()
        tasks.add(task)
        try:
            await Session(reader,writer,receipt_delay=.03).run()
        finally:
            tasks.discard(task)
    server=await asyncio.start_server(connected,"127.0.0.1",0)
    port=server.sockets[0].getsockname()[1]
    reader,writer=await asyncio.open_connection("127.0.0.1",port)
    async def exchange(pdu):
        writer.write(ENCODER.encode(pdu))
        await writer.drain()
        return await asyncio.wait_for(read_pdu(reader),1)
    try:
        if bind:
            response=await exchange(BindTransceiver(seqNum=1,system_id=b"sim-user",password=password,interface_version=0x34))
            if password!=b"sim-pass":
                assert response.status==CommandStatus.ESME_RINVPASWD
                return
            assert response.status==CommandStatus.ESME_ROK
            assert (await exchange(EnquireLink(seqNum=2))).status==CommandStatus.ESME_ROK
        response=await exchange(SubmitSM(seqNum=3,source_addr=b"JasminDemo",destination_addr=f"256700000{suffix}".encode(),short_message=b"Hello"))
        if not bind:
            assert response.status==CommandStatus.ESME_RINVBNDSTS
        elif suffix=="003":
            assert response.status==CommandStatus.ESME_RINVDSTADR
        elif suffix=="004":
            assert response.status==CommandStatus.ESME_ROK
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(read_pdu(reader),.12)
        else:
            assert response.status==CommandStatus.ESME_ROK
            receipt=await asyncio.wait_for(read_pdu(reader),1)
            assert isinstance(receipt,DeliverSM)
            assert receipt.params["receipted_message_id"]==response.params["message_id"]
            assert receipt.params["message_state"]==(MessageState.DELIVERED if suffix=="001" else MessageState.UNDELIVERABLE)
            assert (b"stat:DELIVRD" if suffix=="001" else b"stat:UNDELIV") in receipt.params["short_message"]
            writer.write(ENCODER.encode(receipt.requireAck(seqNum=receipt.seqNum)))
            await writer.drain()
    finally:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()
        server.close()
        await server.wait_closed()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks),2)


@pytest.mark.parametrize("suffix",["001","002","003","004"])
def test_real_smpp_socket_exchange(suffix):
    asyncio.run(exercise(suffix))


def test_smpp_bad_password():
    asyncio.run(exercise("001",password=b"wrong"))


def test_submission_requires_bind():
    asyncio.run(exercise("001",bind=False))
