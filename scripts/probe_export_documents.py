"""Build a real plan and export it, so the documents can be read rather than assumed.

Unit tests can assert that a line is present; they cannot tell you whether the document is
readable or whether the caveats ended up somewhere a reader would see them. This runs a real
plan through the real export path and writes both files out for inspection, then checks the
properties that must hold regardless of the plan's contents.

For the calendar file those properties are structural and easy to get wrong invisibly:

  * CRLF line endings and no line longer than 75 octets — clients that reject an over-long
    line drop the event silently rather than reporting anything;
  * every event is a floating time, never a claimed UTC instant, because the itinerary is
    stored naive;
  * the disclaimer travels on the event, not only on the calendar, because events get
    forwarded individually.

Needs TRAVER_LIVE_TESTS=1 for a plan with real sources; otherwise it uses the deterministic
fixture provider, which still exercises the whole export path.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api/src"))

from fastapi.testclient import TestClient  # noqa: E402
from travel_api.config import Settings  # noqa: E402
from travel_api.main import create_app  # noqa: E402

MAX_LINE_OCTETS = 75


def unfold(text: str) -> str:
    """Undo RFC 5545 line folding: CRLF followed by a space continues the previous line.

    Every calendar client does this before parsing, so a probe that reads the raw bytes will
    fail to find text that is genuinely present — which is what this did on the first run
    ("0 of 5 events carry the disclaimer") when the disclaimer was simply split across a fold.
    Unfolding here also checks the folding itself: if it were wrong, unfolding would not
    restore the original content.
    """
    return text.replace("\r\n ", "")


def main() -> int:
    live = os.environ.get("TRAVER_LIVE_TESTS") == "1"
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp / 'export.db'}",
        auto_create_schema=True,
        auth_enabled=False,
        free_providers_enabled=live,
        mock_provider_enabled=not live,
        credential_key_file=str(tmp / "credential.key"),
    )
    problems: list[str] = []

    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/v1/trips",
            json={
                "destination": "杭州",
                "start_date": date(2026, 10, 3).isoformat(),
                "end_date": date(2026, 10, 5).isoformat(),
                "travelers": 2,
                "total_budget": 5000,
            },
        )
        if created.status_code >= 400:
            raise SystemExit(f"trip creation failed: {created.status_code} {created.text[:300]}")
        trip = created.json()

        markdown = client.get(f"/api/v1/trips/{trip['id']}/export.md")
        calendar = client.get(f"/api/v1/trips/{trip['id']}/export.ics")

        missing = client.get("/api/v1/trips/00000000-0000-0000-0000-000000000000/export.md")

    for name, response in (("markdown", markdown), ("calendar", calendar)):
        if response.status_code != 200:
            problems.append(f"{name}: HTTP {response.status_code} {response.text[:200]}")

    if missing.status_code != 404:
        problems.append(f"an unknown trip must be 404, got {missing.status_code}")
    # The filename has to be usable, and must not carry the raw destination into a header.
    disposition = markdown.headers.get("content-disposition", "")
    if "attachment" not in disposition:
        problems.append("the export is not sent as a download")
    if "\n" in disposition or "\r" in disposition:
        problems.append("the Content-Disposition header contains a newline")
    if ".md" not in disposition:
        problems.append(f"the filename does not end in .md: {disposition!r}")
    if markdown.headers.get("cache-control") != "no-store":
        problems.append("an export snapshot must not be cached as if it were the live plan")

    body = markdown.text
    ics = calendar.text

    (tmp / "plan.md").write_text(body, encoding="utf-8")
    (tmp / "plan.ics").write_text(ics, encoding="utf-8")
    print(f"  wrote {tmp / 'plan.md'}")
    print(f"  wrote {tmp / 'plan.ics'}")

    # -- markdown: the disclosures must be present and adjacent to the numbers --
    required = {
        "generation time": "生成",
        "snapshot warning": "不会自动更新",
        "fetch time column": "抓取时间",
        "basis column": "observed_at 的含义",
        "itinerary": "## 行程",
        "freshness section": "## 时效性",
    }
    for label, needle in required.items():
        if needle not in body:
            problems.append(f"markdown is missing the {label} ({needle!r})")

    # -- calendar: structural rules that fail silently in a client --
    if "\r\n" not in ics:
        problems.append("the calendar does not use CRLF line endings")
    if not ics.endswith("\r\n"):
        problems.append("the calendar does not end with a trailing CRLF")
    for index, line in enumerate(ics.split("\r\n")):
        width = len(line.encode("utf-8"))
        if width > MAX_LINE_OCTETS:
            problems.append(f"calendar line {index} is {width} octets, over the 75 limit")
            break
    if ics.count("BEGIN:VEVENT") != ics.count("END:VEVENT"):
        problems.append("unbalanced VEVENT blocks")
    if "BEGIN:VCALENDAR" not in ics or "END:VCALENDAR" not in ics:
        problems.append("the calendar has no VCALENDAR envelope")

    # Every DTSTART must be floating: no trailing Z (which would assert UTC) and no TZID.
    for line in ics.split("\r\n"):
        if line.startswith("DTSTART") or line.startswith("DTEND"):
            if line.rstrip().endswith("Z"):
                problems.append(f"a floating time was written as UTC: {line}")
            if "TZID" in line:
                problems.append(f"a timezone was asserted without knowing one: {line}")

    # And the disclaimer has to be on the event, not only on the calendar. Checked after
    # unfolding, because folding splits it across lines and a client unfolds before reading.
    flat = unfold(ics)
    events = flat.count("BEGIN:VEVENT")
    if events:
        with_footer = flat.count("是快照，不会自动更新")
        if with_footer < events:
            problems.append(
                f"only {with_footer} of {events} events carry the snapshot disclaimer; "
                f"a forwarded single event would lose it"
            )
        if flat.count("STATUS:TENTATIVE") < events:
            problems.append("a plan is not a booking: every event should be TENTATIVE")

    print(f"  markdown           : {len(body):6} chars")
    print(f"  calendar           : {len(ics):6} chars, {events} events")
    print(f"  body lines         : {len(body.splitlines())}")
    print(f"  header             : {disposition}")

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nOK: both documents carry their own times, caveats and structure")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
