"""A deliberately small SMPP 3.4 provider simulator; never contacts a carrier.

Supports a transceiver bind, enquire_link, submit_sm, deliver_sm receipts and
unbind. It is not a general-purpose SMSC and does not simulate handset radios.
"""
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from io import BytesIO
import logging
import struct
import uuid

from smpp.pdu.operations import BindTransceiver, DeliverSM, DeliverSMResp, EnquireLink, SubmitSM, Unbind
from smpp.pdu.pdu_encoding import PDUEncoder
from smpp.pdu.pdu_types import CommandStatus, EsmClass, EsmClassMode, EsmClassType, MessageState

log = logging.getLogger("simulator")
ENCODER = PDUEncoder()


async def read_pdu(reader):
    header = await reader.readexactly(16)
    length = struct.unpack("!I",header[:4])[0]
    if not 16 <= length <= 65536:
        raise ValueError("Invalid PDU size")
    body = await reader.readexactly(length-16)
    return ENCODER.decode(BytesIO(header+body))


class Session:
    def __init__(self, reader, writer, receipt_delay=2):
        self.reader, self.writer = reader, writer
        self.receipt_delay = receipt_delay
        self.bound = False
        self.sequence = 10000
        self.pending = {}
        self.tasks = set()

    async def send(self,pdu):
        self.writer.write(ENCODER.encode(pdu))
        await self.writer.drain()

    async def acknowledge(self,pdu,**params):
        await self.send(pdu.requireAck(seqNum=pdu.seqNum,**params))

    async def deliver_receipt(self,submitted,message_id,failed):
        try:
            await asyncio.sleep(self.receipt_delay)
            now = datetime.now(timezone.utc).strftime("%y%m%d%H%M")
            status = "UNDELIV" if failed else "DELIVRD"
            self.sequence += 1
            seq = self.sequence
            text = (f"id:{message_id} sub:001 dlvrd:{'000' if failed else '001'} "
                    f"submit date:{now} done date:{now} stat:{status} "
                    f"err:{'001' if failed else '000'} text:demo")
            receipt = DeliverSM(seqNum=seq,source_addr=submitted.params["destination_addr"],
                                destination_addr=submitted.params["source_addr"],
                                esm_class=EsmClass(EsmClassMode.DEFAULT,EsmClassType.SMSC_DELIVERY_RECEIPT),
                                short_message=text.encode(),receipted_message_id=message_id.encode(),
                                message_state=MessageState.UNDELIVERABLE if failed else MessageState.DELIVERED)
            ack = asyncio.get_running_loop().create_future()
            self.pending[seq] = ack
            for attempt in range(3):
                await self.send(receipt)
                try:
                    await asyncio.wait_for(asyncio.shield(ack),2)
                    break
                except asyncio.TimeoutError:
                    if attempt==2:
                        log.warning("Receipt unacknowledged: %s",message_id)
            self.pending.pop(seq,None)
        except (ConnectionError,asyncio.CancelledError):
            pass

    async def run(self):
        try:
            while True:
                pdu = await read_pdu(self.reader)
                if isinstance(pdu,BindTransceiver):
                    valid = pdu.params["system_id"]==b"sim-user" and pdu.params["password"]==b"sim-pass"
                    self.bound = valid
                    await self.acknowledge(pdu,system_id=b"demo-smsc",
                                           status=CommandStatus.ESME_ROK if valid else CommandStatus.ESME_RINVPASWD)
                    if not valid:
                        break
                    log.info("Jasmin transceiver connected to simulated provider")
                elif isinstance(pdu,EnquireLink):
                    await self.acknowledge(pdu)
                elif isinstance(pdu,Unbind):
                    await self.acknowledge(pdu)
                    break
                elif isinstance(pdu,DeliverSMResp):
                    ack = self.pending.get(pdu.seqNum)
                    if ack and not ack.done() and pdu.status==CommandStatus.ESME_ROK:
                        ack.set_result(True)
                elif isinstance(pdu,SubmitSM):
                    dest = pdu.params["destination_addr"].decode("ascii")
                    if not self.bound:
                        await self.acknowledge(pdu,status=CommandStatus.ESME_RINVBNDSTS)
                    elif dest not in {"256700000001","256700000002","256700000004"}:
                        await self.acknowledge(pdu,status=CommandStatus.ESME_RINVDSTADR)
                    else:
                        message_id = str(uuid.uuid4().int)
                        await self.acknowledge(pdu,message_id=message_id.encode())
                        log.info("Simulated submission accepted: %s",message_id)
                        if not dest.endswith("004"):
                            task = asyncio.create_task(self.deliver_receipt(pdu,message_id,dest.endswith("002")))
                            self.tasks.add(task)
                            task.add_done_callback(self.tasks.discard)
                elif getattr(pdu,"requireAck",None):
                    await self.acknowledge(pdu,status=CommandStatus.ESME_RINVCMDID)
        except (asyncio.IncompleteReadError,ConnectionError,ValueError):
            pass
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks,return_exceptions=True)
            self.writer.close()
            with suppress(ConnectionError):
                await self.writer.wait_closed()


async def main():
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(message)s")
    async def connected(reader,writer):
        await Session(reader,writer).run()
    server = await asyncio.start_server(connected,"0.0.0.0",2775)
    log.info("Simulator listening on port 2775. No real SMS will be sent.")
    async with server:
        await server.serve_forever()


if __name__=="__main__":
    asyncio.run(main())
