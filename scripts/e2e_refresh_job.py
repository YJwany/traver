"""End-to-end check of the refresh job against a real uvicorn process.

Unit and integration tests use TestClient, which shares the event loop with the app. This
runs the real server over a real socket with real free providers, because the thing being
checked is a property of a *background* task: that a refresh reports progress while it is
still running, rather than the whole thing happening inside one request.

Run with the API's src on PYTHONPATH. Exits non-zero on the first failed expectation.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_health(base: str, timeout: float = 90.0) -> None:
    # The API is mounted under /api/v1, so liveness is /api/v1/health — not /health.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/api/v1/health", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:  # noqa: BLE001 - the server is simply not up yet
            time.sleep(0.4)
    raise SystemExit("server never became healthy")


def call(base: str, path: str, key: str, *, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{base}{path}", data=data, method=method)
    request.add_header("Authorization", f"Bearer {key}")
    if data:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, json.loads(response.read() or b"null")


def main() -> None:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "apps/api/src"),
            "PYTHONUTF8": "1",
            # No env_prefix on Settings, so these are the bare field names. A prefixed name
            # like TRAVEL_API_DATABASE_URL is silently ignored, which would leave the server
            # pointed at whatever .env says — a test that writes to the real database.
            "DATABASE_URL": f"sqlite:///{Path(tmp) / 'e2e.db'}",
            "AUTO_CREATE_SCHEMA": "true",
            "AUTH_ENABLED": "true",
            "AUTH_BOOTSTRAP_KEY": "trv_e2e_bootstrap_key",
            "AUTH_BOOTSTRAP_OWNER": "e2e",
            "FREE_PROVIDERS_ENABLED": "true",
            "MOCK_PROVIDER_ENABLED": "false",
            "CREDENTIAL_KEY_FILE": str(Path(tmp) / "credential.key"),
            # The log stream is handy here: it is where a startup failure would show.
            "LOG_LEVEL": "WARNING",
        }
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "travel_api.main:app", "--port", str(port)],
            cwd=ROOT,
            env=env,
            stdout=None,
            stderr=None,
        )
        try:
            wait_for_health(base)
            key = "trv_e2e_bootstrap_key"

            # 1. A plan first, so there is something to refresh.
            print("planning 杭州 3 天 (real free providers, ~70 s)...")
            status, job = call(
                base,
                "/api/v1/planning/jobs",
                key,
                method="POST",
                body={
                    "destination": "杭州",
                    "start_date": "2026-10-03",
                    "end_date": "2026-10-05",
                    "travelers": 2,
                    "total_budget": 5000,
                },
            )
            assert status == 202, status
            assert job["kind"] == "plan", job["kind"]
            assert job["target_trip_id"] is None, "a first plan has no target trip"

            while job["status"] not in {"succeeded", "failed", "cancelled"}:
                time.sleep(1.0)
                _, job = call(base, f"/api/v1/planning/jobs/{job['id']}", key)
            assert job["status"] == "succeeded", job["error"]
            trip_id = job["trip_id"]
            print(f"  trip {trip_id} planned in {job['elapsed_ms']} ms")

            _, before = call(base, f"/api/v1/trips/{trip_id}", key)
            version_before = before["current_version"]

            # 2. Refresh it as a job.
            print("re-planning as a job...")
            t0 = time.monotonic()
            status, refresh = call(
                base, f"/api/v1/trips/{trip_id}/refresh/jobs", key, method="POST"
            )
            returned_after = time.monotonic() - t0
            assert status == 202, status
            assert refresh["kind"] == "refresh", refresh["kind"]
            assert refresh["target_trip_id"] == trip_id, refresh["target_trip_id"]
            # The whole point: the request returns immediately.
            assert returned_after < 5.0, f"the refresh endpoint blocked for {returned_after:.1f}s"
            print(f"  accepted in {returned_after * 1000:.0f} ms")

            # 3. Watch it run, and require progress to arrive *before* it finishes.
            saw_running = False
            seen_steps: set[str] = set()
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                _, current = call(base, f"/api/v1/planning/jobs/{refresh['id']}", key)
                if current["status"] == "running":
                    saw_running = True
                for step in current["steps"]:
                    if step["finished_at"] is None:
                        assert step["duration_ms"] is None, "an unfinished step claimed a duration"
                    else:
                        assert step["duration_ms"] is not None, "a finished step has no duration"
                    seen_steps.add(step["name"])
                if current["status"] in {"succeeded", "failed", "cancelled"}:
                    break
                time.sleep(0.5)
            else:
                raise SystemExit("the refresh job never settled")

            assert saw_running, "the job finished before it could be observed running"
            assert current["status"] == "succeeded", current["error"]
            assert "reload_trip" in seen_steps, f"refresh did not report reload_trip: {seen_steps}"
            assert current["trip_id"] == trip_id, "a refresh must not create a second trip"
            print(f"  refreshed in {current['elapsed_ms']} ms; steps={sorted(seen_steps)}")

            # 4. The trip really was re-planned.
            _, after = call(base, f"/api/v1/trips/{trip_id}", key)
            assert after["current_version"] > version_before, "the trip version did not advance"
            print(f"  version {version_before} -> {after['current_version']}")

            # 5. A missing trip is refused up front, and creates no job.
            _, jobs_before = call(base, "/api/v1/planning/jobs", key)
            try:
                call(
                    base,
                    "/api/v1/trips/00000000-0000-4000-8000-000000000000/refresh/jobs",
                    key,
                    method="POST",
                )
                raise SystemExit("refreshing a missing trip did not fail")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404, exc.code
            _, jobs_after = call(base, "/api/v1/planning/jobs", key)
            assert len(jobs_after) == len(jobs_before), "a job was created for a missing trip"
            print("  missing trip -> 404, no job created")

            print("\nOK")
        finally:
            server.terminate()
            server.wait(timeout=20)


if __name__ == "__main__":
    main()
