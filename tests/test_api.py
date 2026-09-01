import asyncio
import uuid

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import Settings, create_app, receipt_token, submit_one
from app.store import Store

AUTH = {"X-API-Key":"test-api-key"}
BODY = {"client_ref":"first-message","to":"256700000001","content":"Hello from Jasmin!"}


@pytest.fixture
def lab(tmp_path):
    settings = Settings(database=str(tmp_path/"test.sqlite3"),api_key="test-api-key",callback_secret="test-secret")
    app = create_app(settings,start_worker=False,
                     transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    with TestClient(app) as client:
        yield client,app.state.store,settings


def add(client):
    response = client.post("/messages",json=BODY,headers=AUTH)
    assert response.status_code==202
    return response.json()


def receipt(client,settings,mid,status="DELIVRD",level="2",gateway_id="gateway-123"):
    return client.post(f"/callbacks/dlr/{mid}",params={"token":receipt_token(settings,mid)},
                       data={"id":gateway_id,"message_status":status,"level":level})


def test_idempotency_and_conflicting_reference(lab):
    client,store,_ = lab
    first = add(client)
    assert add(client)["id"]==first["id"]
    conflict = client.post("/messages",json={**BODY,"content":"Different content"},headers=AUTH)
    assert conflict.status_code==409
    assert len(store.list())==1


@pytest.mark.parametrize("bad_to",["256777777777","+256700000001","not-a-number"])
def test_only_synthetic_destinations_allowed(lab,bad_to):
    client,_,_ = lab
    assert client.post("/messages",json={**BODY,"to":bad_to},headers=AUTH).status_code==422


@pytest.mark.parametrize("content",["Hello 😀","x"*161,"", "Text with [extension] characters"])
def test_single_basic_text_segment(lab,content):
    client,_,_ = lab
    assert client.post("/messages",json={**BODY,"content":content},headers=AUTH).status_code==422


def test_authentication(lab):
    client,_,settings = lab
    assert client.get("/messages").status_code==403
    assert client.post("/messages",json=BODY,headers={"X-API-Key":"wrong"}).status_code==403
    row = add(client)
    assert client.post(f"/callbacks/dlr/{row['id']}?token=wrong",data={}).status_code==403


def test_duplicate_and_out_of_order_callbacks(lab):
    client,store,settings = lab
    mid = add(client)["id"]
    for _ in range(2):
        response = receipt(client,settings,mid)
        assert response.status_code==200
        assert response.content==b"ACK/Jasmin"
    assert len(store.get(mid)["events"])==1
    receipt(client,settings,mid,"ESME_ROK","1")
    assert store.get(mid)["status"]=="DELIVERED"
    assert len(store.get(mid)["events"])==2


def test_callback_can_arrive_before_submit_response(lab):
    client,store,settings = lab
    mid = add(client)["id"]
    store.claim()
    receipt(client,settings,mid)
    store.submission_result(mid,"QUEUED",gateway_id="gateway-123")
    assert store.get(mid)["status"]=="DELIVERED"
    assert store.get(mid)["gateway_id"]=="gateway-123"


@pytest.mark.parametrize("raw,level,expected",[
    ("ESME_ROK","1","ACCEPTED"),("ESME_RINVDSTADR","1","REJECTED"),
    ("UNDELIV","2","UNDELIVERABLE"),("EXPIRED","2","EXPIRED"),
    ("REJECTD","2","REJECTED"),("UNKNOWN","2","UNKNOWN"),
])
def test_receipt_outcomes(lab,raw,level,expected):
    client,store,settings = lab
    mid = add(client)["id"]
    assert receipt(client,settings,mid,raw,level).status_code==200
    assert store.get(mid)["status"]==expected


def test_mismatched_gateway_id_and_malformed_receipt(lab):
    client,store,settings = lab
    mid = add(client)["id"]
    store.submission_result(mid,"QUEUED",gateway_id="expected-id")
    assert receipt(client,settings,mid,gateway_id="wrong-id").status_code==409
    url = f"/callbacks/dlr/{mid}?token={receipt_token(settings,mid)}"
    assert client.post(url,data={"id":"expected-id"}).status_code==400
    assert client.post(url,content=b"x"*9000).status_code==413
    assert store.get(mid)["status"]=="QUEUED"


def test_terminal_failure_survives_late_acceptance(lab):
    client,store,settings = lab
    mid = add(client)["id"]
    receipt(client,settings,mid,"UNDELIV")
    receipt(client,settings,mid,"ESME_ROK","1")
    assert store.get(mid)["status"]=="UNDELIVERABLE"


@pytest.mark.parametrize("scenario,expected",[
    ("success","QUEUED"),("read-timeout","UNKNOWN"),("write-timeout","UNKNOWN"),
    ("connection-refused","PENDING"),("invalid-route","REJECTED"),
    ("server-error","UNKNOWN"),("malformed-success","UNKNOWN"),
])
def test_gateway_response_handling(lab,scenario,expected):
    client,store,settings = lab
    mid = add(client)["id"]
    row = store.claim()
    calls=[]
    def gateway(request):
        calls.append(request)
        assert request.method=="POST"
        assert request.headers["content-type"]=="application/x-www-form-urlencoded"
        assert b"dlr-level=3" in request.content
        if scenario=="read-timeout":
            raise httpx.ReadTimeout("test",request=request)
        if scenario=="write-timeout":
            raise httpx.WriteTimeout("test",request=request)
        if scenario=="connection-refused":
            raise httpx.ConnectError("test",request=request)
        if scenario=="invalid-route":
            return httpx.Response(412,text='Error "No route found"')
        if scenario=="server-error":
            return httpx.Response(500,text="error")
        if scenario=="malformed-success":
            return httpx.Response(200,text="not-a-gateway-response")
        return httpx.Response(200,text='Success "test-gateway-id"')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as http:
            await submit_one(store,settings,http,row)
    asyncio.run(run())
    assert store.get(mid)["status"]==expected
    assert len(calls)==1
    if expected=="QUEUED":
        assert store.get(mid)["gateway_id"]=="test-gateway-id"


def test_restart_preserves_data_without_resending_uncertain_submission(tmp_path):
    path = tmp_path/"durable.sqlite3"
    store = Store(path)
    first = store.create("first",BODY["to"],BODY["content"])
    store.claim()
    second = store.create("second",BODY["to"],BODY["content"])
    reopened = Store(path)
    reopened.recover()
    assert reopened.get(first["id"])["status"]=="UNKNOWN"
    assert reopened.get(second["id"])["status"]=="PENDING"
    assert reopened.claim()["id"]==second["id"]


def test_background_worker_submits_and_records_gateway_id(tmp_path):
    import time
    settings = Settings(database=str(tmp_path/"worker.sqlite3"),api_key="test-api-key")
    requests=[]
    def gateway(request):
        requests.append(request)
        return httpx.Response(200,text='Success "worker-gateway-id"')
    app=create_app(settings,transport=httpx.MockTransport(gateway))
    with TestClient(app) as client:
        mid=add(client)["id"]
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            data=client.get(f"/messages/{mid}",headers=AUTH).json()
            if data["status"]=="QUEUED":
                break
            time.sleep(.05)
        assert data["status"]=="QUEUED"
        assert data["gateway_id"]=="worker-gateway-id"
        assert len(requests)==1
