"""Verify the real RollingGo MCP response shape against a real key.

Why this exists
---------------
``RollingGoHotelProvider`` locates the hotel and room arrays among the keys a payload of this
kind plausibly uses, and records which one matched. That tolerance is deliberate — but it has
a blind spot: a unit test whose fixture was written from the same assumption as the parser
cannot detect a wrong assumption. Both are consistently wrong together.

The consequence is the failure this project keeps meeting. If the envelope is not what the
parser expects, ``locate_list`` matches nothing, no hotel is produced, and the planner falls
back to an estimated accommodation line — which looks exactly like "the source returned no
hotels", and exactly like "no source is configured". Nothing reports an error, and the whole
point of wiring this source in was to stop estimating.

So this runs real requests and reports, factually, what came back: the raw JSON-RPC envelope,
every key path, which key the parser matched, and whether anything was produced. It asserts
only what must hold regardless of shape — that the request succeeded and that a hotel list was
located somewhere.

Needs a key:

    $env:TRAVER_ROLLINGGO_KEY = '...'   # 个人 Key, from rollinggo.store
    python scripts/probe_rollinggo_shape.py

Optional: TRAVER_ROLLINGGO_PLACE (default 杭州) and TRAVER_ROLLINGGO_DATES as
``YYYY-MM-DD,YYYY-MM-DD``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api/src"))

from travel_api.domain.models import TripProfile  # noqa: E402
from travel_api.providers.free.http import FreeHttpClient  # noqa: E402
from travel_api.providers.rollinggo import (  # noqa: E402
    RollingGoHotelProvider,
    RollingGoMcpClient,
)

DEFAULT_PLACE = "杭州"


def _keys(payload: object, prefix: str = "", depth: int = 0) -> list[str]:
    """Every key path in a JSON object, so the shape can be read at a glance."""
    if depth > 3 or not isinstance(payload, dict):
        return []
    found: list[str] = []
    for key, value in payload.items():
        path = f"{prefix}{key}"
        found.append(path)
        if isinstance(value, dict):
            found.extend(_keys(value, f"{path}.", depth + 1))
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            found.extend(_keys(value[0], f"{path}[].", depth + 1))
    return found


class _Capturing:
    """Records each raw response so the envelope can be printed even when parsing fails."""

    def __init__(self, inner: FreeHttpClient) -> None:
        self.inner = inner
        self.calls: list[dict] = []

    async def request_raw(
        self, method, url, *, params=None, data=None, json_body=None, headers=None
    ):
        response = await self.inner.request_raw(
            method, url, params=params, data=data, json_body=json_body, headers=headers
        )
        self.calls.append(
            {"request": json_body, "status": response.status_code, "text": response.text}
        )
        return response

    def __getattr__(self, item):
        return getattr(self.inner, item)


def _report(label: str, captured: dict, rows_label: str, rows: list) -> list[str]:
    """Print one call's shape and return the problems it revealed."""
    problems: list[str] = []
    print(f"\n== {label} ==")
    print(
        f"  request arguments  : {json.dumps((captured.get('request') or {}).get('params', {}).get('arguments', {}), ensure_ascii=False)}"
    )
    text = captured.get("text") or ""
    print(f"  raw body (first 600): {text[:600]}")

    try:
        envelope = json.loads(text)
    except ValueError:
        problems.append(
            f"{label}: the body is not plain JSON (it may be SSE-framed; the parser handles that)"
        )
        return problems

    print(f"  envelope keys      : {sorted(envelope.keys())}")
    result = envelope.get("result")
    if isinstance(result, dict):
        print(f"  result keys        : {sorted(result.keys())}")
        content = result.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            print(
                f"  content[0] type    : {first.get('type') if isinstance(first, dict) else type(first).__name__}"
            )
            if isinstance(first, dict) and isinstance(first.get("text"), str):
                try:
                    tool_payload = json.loads(first["text"])
                except ValueError:
                    print("  content[0].text    : (not JSON)")
                else:
                    print(
                        f"  tool payload keys  : {sorted(tool_payload.keys()) if isinstance(tool_payload, dict) else type(tool_payload).__name__}"
                    )
                    print(f"  tool key paths     : {sorted(_keys(tool_payload))[:40]}")
    if envelope.get("error"):
        problems.append(f"{label}: JSON-RPC error {envelope['error']}")
    print(f"  {rows_label:<18} : {len(rows)}")
    if not rows:
        problems.append(
            f"{label}: nothing was located — either the source returned none for this query, or "
            "the envelope is not the shape the parser expects (compare the key paths above with "
            "HOTEL_LIST_KEYS / ROOM_LIST_KEYS in providers/rollinggo/hotels.py)"
        )
    return problems


async def main() -> int:
    key = os.environ.get("TRAVER_ROLLINGGO_KEY", "").strip()
    if not key:
        print("SKIP: set TRAVER_ROLLINGGO_KEY to run this against the real service")
        return 0

    place = os.environ.get("TRAVER_ROLLINGGO_PLACE", DEFAULT_PLACE)
    dates = os.environ.get("TRAVER_ROLLINGGO_DATES", "")
    if dates and "," in dates:
        start_raw, end_raw = (part.strip() for part in dates.split(",", 1))
        start, end = date.fromisoformat(start_raw), date.fromisoformat(end_raw)
    else:
        start = date.today() + timedelta(days=14)
        end = start + timedelta(days=2)

    profile = TripProfile(
        destination=place,
        start_date=start,
        end_date=end,
        travelers=2,
    )
    print(f"probing RollingGo for {place}, {start} → {end}")

    capturing = _Capturing(
        FreeHttpClient(
            user_agent="traver-shape-probe/1.0",
            timeout_seconds=30.0,
            host_intervals={"mcp.rollinggo.cn": 1.0},
        )
    )
    client = RollingGoMcpClient(http=capturing, key=key)
    provider = RollingGoHotelProvider(client)

    problems: list[str] = []

    hotels = await provider.search_hotels(profile)
    if capturing.calls:
        problems += _report("searchHotels", capturing.calls[-1], "hotels produced", hotels.data)
    else:
        problems.append("searchHotels: no request was made")
        return 1

    if hotels.data:
        first = hotels.data[0]
        print(f"\n  first hotel        : {first.canonical_name}")
        print(f"  supplier id        : {first.source_references[0].source_id}")
        print(
            f"  payload key used   : {first.source_references[0].request_context.get('payload_key')}"
        )

        offers = await provider.search_room_offers(hotels.data[:1], profile)
        problems += _report("getHotelDetail", capturing.calls[-1], "offers produced", offers.data)
        if offers.data:
            offer = offers.data[0]
            print(
                f"  price              : {offer.price.value} {offer.price.currency} ({offer.price.price_type})"
            )
            print(f"  availability       : {offer.availability}")
            print(f"  cancellation       : {offer.cancellation_policy}")
            print(f"  observed_at_basis  : {offer.source_reference.observed_at_basis}")

    await capturing.aclose()

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nOK: the real MCP envelope is one the parser reads")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
