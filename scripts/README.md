# `scripts/` — operator tooling

Repeatable helpers that are not part of the request path.

| Script | Purpose |
|---|---|
| `healthcheck.py` | Container health probe. Exits non-zero unless `/api/v1/health` reports `ready: true`, so an API that cannot reach its database is marked unhealthy instead of merely "running". Also usable from a shell: `python scripts/healthcheck.py`. |

Credential management lives in the application package rather than here, so it
reuses the exact hashing and validation the API uses:

```powershell
python -m travel_api.cli create-key --owner alice --label laptop
python -m travel_api.cli list-keys
python -m travel_api.cli revoke-key <key-id>
```

Also absent by design: fixture loading and seed scripts. Deterministic test data
is supplied by `providers/fixture.py` and the mock provider, and the live tests
exercise real endpoints — so a separate seed step would only add a way for local
data to diverge from what the tests actually verify.
