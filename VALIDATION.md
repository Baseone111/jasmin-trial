# Validation record

Prepared: 31 August 2026.

## Executed successfully

- Python 3.12.13: `python -m pytest -q -x --tb=short` — **35 passed**.
- API tests: authentication, input restrictions, idempotency and conflicting
  references, worker submission, response parsing, delivery callbacks, duplicate
  receipts, out-of-order events, gateway ID mismatch, malformed receipts,
  terminal failure states, uncertain HTTP failures, and persistence on restart.
- Simulator tests: real local TCP connections carrying encoded SMPP PDUs for
  transceiver bind, enquire_link, successful/failed delivery receipts,
  submission rejection, missing final receipt, invalid password, and unbound
  submission rejection.
- Gateway configuration test: explicit credential alignment, preservation of
  logging interpolation syntax and file mode, and repeated configuration.
- Swagger UI HTML and generated OpenAPI schema: successful responses, expected
  title, API-key security scheme, public message endpoints, private callback
  omitted from the interactive documentation.
- Compose YAML parsing and structural checks: all dependencies refer to defined
  services; all services use the internal lab network; only API port 8000 is
  published, bound to `127.0.0.1`.
- Python source syntax checks.

## Not executed here

**The complete Docker stack was not run. This environment has no Docker or
Podman engine, RabbitMQ server, or Redis server.**

Consequently, the following remain unverified in an actual running Jasmin stack:

- Image pulling and Docker builds.
- The official image's entrypoint combined with the configuration wrapper.
- The automatic jCli bootstrap against the real gateway.
- Real Jasmin routing through RabbitMQ/Redis to the SMPP simulator and back.
- The full-stack smoke test and simulator-outage recovery script.

The passing HTTP gateway tests use `httpx.MockTransport`. They validate the
application's behavior against the documented HTTP contract; they do not prove
that an upstream Jasmin image works with this Compose configuration.

Run these checks on a Docker-equipped machine before claiming the full demo is
working end to end:

```bash
docker compose up --build -d
docker compose logs bootstrap
docker compose exec -T api python scripts/smoke_test.py
python3 scripts/outage_test.py
```

The final command requires Docker Compose on the host and temporarily stops only
this lab's simulator. It restores the simulator in a `finally` block.

No SMS was sent to a real phone, and no real provider credentials were used.
