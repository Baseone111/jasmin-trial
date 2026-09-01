"""Local lab experiment: stop only this project's simulator, then restore it.

Run from the host with Docker Compose available, not inside the API container.
The simulator is restarted in finally, even when the test fails.
"""
import subprocess
from pathlib import Path
import time
import uuid

from smoke_test import request

ROOT = Path(__file__).resolve().parents[1]


def compose(*args):
    subprocess.run(["docker","compose",*args],cwd=ROOT,check=True)


def main():
    row = None
    try:
        compose("stop","simulator")
        time.sleep(2)
        row=request("/messages",{"client_ref":f"outage-{uuid.uuid4().hex}",
                                 "to":"256700000001","content":"Queued while the simulated provider is offline."})
        time.sleep(3)
        row=request(f"/messages/{row['id']}")
        assert row["status"] not in {"DELIVERED","UNDELIVERABLE"},"Unexpected terminal delivery while provider was offline"
        print(f"Provider offline: application status={row['status']}")
    finally:
        compose("start","simulator")
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        row=request(f"/messages/{row['id']}")
        if row["status"]=="DELIVERED":
            print("PASS: Jasmin delivered the queued SMS after the simulated provider reconnected.")
            return
        time.sleep(.5)
    raise RuntimeError(f"Message did not recover: {row['status']}. Inspect Jasmin and simulator logs.")


if __name__=="__main__":
    main()
