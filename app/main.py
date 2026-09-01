"""Local educational SMS API. Swagger UI is at /docs; no frontend build needed."""
import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import hashlib
import hmac
import logging
import os
import re
from typing import Literal
from urllib.parse import parse_qs

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from app.store import Store

log = logging.getLogger(__name__)


@dataclass
class Settings:
    database: str = os.getenv("DATABASE_PATH", "/data/demo.sqlite3")
    api_key: str = os.getenv("DEMO_API_KEY", "local-demo-key")
    callback_secret: str = os.getenv("CALLBACK_SECRET", "local-callback-secret")
    callback_base: str = os.getenv("CALLBACK_BASE_URL", "http://api:8000")
    gateway_url: str = os.getenv("JASMIN_SEND_URL", "http://jasmin:1401/send")
    username: str = os.getenv("JASMIN_USERNAME", "demo-app")
    password: str = os.getenv("JASMIN_PASSWORD", "local-sms-only")


class MessageInput(BaseModel):
    client_ref: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$", examples=["my-first-sms"])
    to: Literal["256700000001", "256700000002", "256700000003", "256700000004"] = "256700000001"
    content: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9 .,!?():;/'-]+$", examples=["Hello from my Jasmin demo!"])


def receipt_token(settings, mid):
    return hmac.new(settings.callback_secret.encode(), mid.encode(), hashlib.sha256).hexdigest()


async def submit_one(store, settings, client, row):
    mid = row["id"]
    payload = {
        "username": settings.username, "password": settings.password,
        "to": row["destination"], "from": "JasminDemo", "content": row["content"],
        "coding": "0", "dlr": "yes", "dlr-level": "3", "dlr-method": "POST",
        "dlr-url": f"{settings.callback_base}/callbacks/dlr/{mid}?token={receipt_token(settings,mid)}",
    }
    try:
        response = await client.post(settings.gateway_url, data=payload)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        retry = row["attempts"] < 3
        store.submission_result(mid, "PENDING" if retry else "REJECTED",
                                error="Could not connect to gateway", delay=2 ** row["attempts"])
        return
    except httpx.HTTPError:
        store.submission_result(mid,"UNKNOWN",error="Ambiguous HTTP failure; no automatic resend")
        return
    match = re.fullmatch(r'Success "([^"]+)"', response.text.strip())
    if response.status_code == 200 and match:
        store.submission_result(mid, "QUEUED", gateway_id=match.group(1))
    elif response.status_code in {400,403,412}:
        store.submission_result(mid, "REJECTED", error=f"Gateway refused request (HTTP {response.status_code})")
    else:
        store.submission_result(mid,"UNKNOWN",error=f"Unexpected gateway result (HTTP {response.status_code}); inspect before resending")


async def worker(store, settings, client):
    while True:
        row = store.claim()
        if row:
            try:
                await submit_one(store, settings, client, row)
            except Exception:
                # Keep the worker alive without logging SMS contents or secrets.
                log.error("Worker failed for message %s", row["id"])
                store.submission_result(row["id"], "UNKNOWN", error="Unexpected worker error; inspect before resending")
        else:
            await asyncio.sleep(0.25)


def create_app(settings=None, *, start_worker=True, transport=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = Store(settings.database)
        app.state.store.recover()
        async with httpx.AsyncClient(timeout=10, transport=transport) as client:
            task = asyncio.create_task(worker(app.state.store, settings, client)) if start_worker else None
            yield
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="Jasmin SMS Demo", version="1.0.0", lifespan=lifespan,
                  description="Local simulator only. No real SMS is sent. Authorize with X-API-Key: local-demo-key. POST a message, then GET its status. Destinations ending 001: delivered; 002: undeliverable; 003: submission rejected; 004: no final receipt. Use a different client_ref for a new message.")
    api_key = APIKeyHeader(name="X-API-Key")

    def authorized(value: str = Depends(api_key)):
        if not hmac.compare_digest(value, settings.api_key):
            raise HTTPException(403,"Invalid demo API key")

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    @app.get("/health", tags=["Health"])
    def health():
        return {"status":"ok", "mode":"simulated-provider", "real_sms":False,
                "note":"API liveness only; run scripts/smoke_test.py to verify the message path."}

    @app.post("/messages", status_code=202, tags=["Messages"], dependencies=[Depends(authorized)])
    def send_message(message: MessageInput):
        """Durably enqueue once per client_ref. Reusing the same reference does not resend."""
        try:
            return app.state.store.create(message.client_ref, message.to, message.content)
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc

    @app.get("/messages", tags=["Messages"], dependencies=[Depends(authorized)])
    def list_messages():
        return app.state.store.list()

    @app.get("/messages/{message_id}", tags=["Messages"], dependencies=[Depends(authorized)])
    def get_message(message_id: str):
        row = app.state.store.get(message_id)
        if not row:
            raise HTTPException(404,"Message not found")
        return row

    @app.post("/callbacks/dlr/{message_id}", include_in_schema=False)
    async def dlr(message_id: str, token: str, request: Request):
        if not hmac.compare_digest(token,receipt_token(settings,message_id)):
            raise HTTPException(403,"Invalid callback token")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>8192:
                raise HTTPException(413,"Receipt too large")
        try:
            fields = parse_qs(raw.decode("utf-8"),max_num_fields=40)
        except (ValueError,UnicodeDecodeError) as exc:
            raise HTTPException(400,"Invalid receipt form") from exc
        if any(len(v)!=1 for v in fields.values()):
            raise HTTPException(400,"Duplicate receipt fields")
        payload = {k:v[0] for k,v in fields.items()}
        if not all(payload.get(k) for k in ("id","message_status","level")) or payload["level"] not in {"1","2","3"}:
            raise HTTPException(400,"Missing or invalid receipt fields")
        if payload["level"]=="1" and not payload["message_status"].startswith("ESME_"):
            raise HTTPException(400,"Invalid submission status")
        try:
            app.state.store.receipt(message_id,payload)
        except KeyError as exc:
            raise HTTPException(404,"Message not found") from exc
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc
        return PlainTextResponse("ACK/Jasmin")

    return app


app = create_app()
