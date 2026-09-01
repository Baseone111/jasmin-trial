"""Exercise the real Compose path: API -> Jasmin -> simulator -> DLR -> API."""
import json
import os
import time
import urllib.request
import uuid

base = os.getenv("DEMO_URL","http://127.0.0.1:8000")
headers = {"X-API-Key":os.getenv("DEMO_API_KEY","local-demo-key"),"Content-Type":"application/json"}


def request(path,payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base+path,data=data,headers=headers)
    with urllib.request.urlopen(req,timeout=10) as response:
        return json.load(response)


def main():
    for suffix,expected in [("001","DELIVERED"),("002","UNDELIVERABLE"),("003","REJECTED")]:
        body = {"client_ref":f"smoke-{uuid.uuid4().hex}","to":f"256700000{suffix}","content":"Hello from the Jasmin lab!"}
        row = request("/messages",body)
        duplicate = request("/messages",body)
        assert duplicate["id"]==row["id"],"Duplicate request created a second message"
        deadline = time.monotonic()+45
        while time.monotonic()<deadline:
            row = request(f"/messages/{row['id']}")
            if row["status"]==expected:
                assert row["gateway_id"],"Missing Jasmin message ID"
                assert row["events"],"Missing delivery callback"
                print(f"PASS {suffix}: {expected}; callback saved; duplicate request reused the ID")
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(f"Expected {expected}, got {row['status']}: {row.get('error')}")
    print("Full-stack smoke test passed. All SMS destinations were simulated.")


if __name__=="__main__":
    main()
