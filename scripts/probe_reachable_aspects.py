"""Is every advertised aspect of a capability actually reachable by some request?

Three defects of one shape have been found in this codebase:

  1. 公交/地铁 — implemented, tested, advertised as the reason to connect 高德, and
     unreachable because ``mode_for_pace`` could never return ``TRANSIT``.
  2. 骑行 — advertised in the capability matrix as a gap a source could fill, when
     ``TravelMode`` has no cycling member at all, so no configuration could fill it.
  3. 紧凑节奏 — offered by the web form as ``packed`` while the API's enum says ``full``,
     so the button returned HTTP 422.

All three are the same failure: a vocabulary the rest of the program cannot produce or
accept. Each was individually invisible — every unit test passed because every part was
correct — and each was found by accident or by a user.

This probe is the general form of the question, so finding the next one does not depend on
noticing it.

The check that makes this possible is ``ASPECT_TRAVEL_MODES``: the manifest's aspect
vocabulary ("driving", "walking", "transit") and ``TravelMode``'s member vocabulary
("drive", "walk", "transit") are two names for the same thing, and before that table
existed nothing in the program said which corresponded to which. Comparing the two lists
was therefore impossible, which is exactly why an aspect with no producer went unnoticed.
Note that a name-similarity check would have *failed* here — "driving" is not "drive" — so
the correspondence has to be declared, not inferred.

Run it whenever a capability gains an aspect. It is a probe and not a test because one
branch needs a 高德 key to answer honestly, and a test that skips is a test that lies by
omission.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api/src"))

from travel_api.domain.models import Pace, TravelMode, TripProfile  # noqa: E402
from travel_api.providers.base import CAPABILITY_BY_METHOD, resolve_travel_mode  # noqa: E402
from travel_api.providers.manifest import (  # noqa: E402
    ASPECT_TRAVEL_MODES,
    ASPECTS_NEEDING_TOOL_SUPPORT,
    CAPABILITY_ASPECTS,
    PROVIDER_CATALOG,
    aspect_closable_by,
    aspect_is_requestable,
)


def main() -> int:
    problems: list[str] = []
    modes = {mode.value for mode in TravelMode}

    # -- 1. the declared correspondence must be real ----------------------
    for aspect, mode in ASPECT_TRAVEL_MODES.items():
        if mode not in modes:
            problems.append(
                f"ASPECT_TRAVEL_MODES maps {aspect} to {mode!r}, which is not a TravelMode member"
            )

    # -- 2. every declared aspect has a verdict ---------------------------
    # An aspect of a decomposable capability must be either expressible by some request or
    # explicitly declared as needing tool support. Nothing may fall between.
    for capability, aspects in CAPABILITY_ASPECTS.items():
        print(f"{capability}:")
        for aspect in sorted(aspects):
            requestable = aspect_is_requestable(aspect)
            closable = aspect_closable_by(aspect)
            if requestable:
                verdict = f"request-able as {ASPECT_TRAVEL_MODES[aspect]!r}"
            elif aspect in ASPECTS_NEEDING_TOOL_SUPPORT:
                verdict = "NOT request-able, declared as needing tool support"
            else:
                verdict = "NOT request-able  <-- undeclared dead end"
                problems.append(
                    f"{capability}.{aspect}: no TravelMode member expresses it and it is not in "
                    f"ASPECTS_NEEDING_TOOL_SUPPORT, so the matrix will offer it as a gap a "
                    f"source could fill — and no source can"
                )
            # The two facts must agree: an aspect needing tool support is by definition not
            # requestable, and a requestable one needs nothing from the tool.
            if requestable != (closable == "source"):
                problems.append(
                    f"{capability}.{aspect}: requestable={requestable} but closable_by={closable}"
                )
            print(f"  {aspect:9} {verdict}")

    # -- 3. no aspect is advertised that no manifest serves ---------------
    # An aspect nobody claims to serve cannot be unlocked by configuring anything, so it
    # must be a declared tool limit rather than silently rendered as a source gap.
    print("\naspects no manifest claims to serve:")
    for capability, aspects in CAPABILITY_ASPECTS.items():
        for aspect in sorted(aspects):
            if aspect in ASPECTS_NEEDING_TOOL_SUPPORT:
                continue
            declared = {
                served_aspect
                for manifest in PROVIDER_CATALOG
                for served_aspect in dict(manifest.aspects).get(capability, ())
            }
            if aspect not in declared:
                print(f"  {capability}.{aspect} — nobody serves it, and it is not a tool limit")
                problems.append(
                    f"{capability}.{aspect}: no manifest claims it, so it can never be covered, "
                    f"yet it is not declared in ASPECTS_NEEDING_TOOL_SUPPORT"
                )
            else:
                print(f"  {capability}.{aspect} — served by at least one manifest")

    # -- 4. every mode is reachable by some request -----------------------
    print("\nresolve_travel_mode reaches:")
    reached: set[str] = set()
    for pace in Pace:
        profile = TripProfile(
            destination="杭州",
            start_date=date(2026, 10, 3),
            end_date=date(2026, 10, 5),
            pace=pace,
        )
        resolved = resolve_travel_mode(profile)
        reached.add(resolved.value)
        print(f"  pace={pace.value:9} -> {resolved.value}")
    for mode in TravelMode:
        profile = TripProfile(
            destination="杭州",
            start_date=date(2026, 10, 3),
            end_date=date(2026, 10, 5),
            travel_mode=mode,
        )
        resolved = resolve_travel_mode(profile)
        reached.add(resolved.value)
        if resolved is not mode:
            problems.append(f"explicit travel_mode={mode.value} resolved to {resolved.value}")

    # A mode the user can pick in the form but the resolver never returns would be a dead
    # option. Every mode the client offers must be reproducible here from *some* request.
    unreachable = modes - reached
    if unreachable:
        problems.append(
            f"TravelMode member(s) no request can select: {sorted(unreachable)} — offered as a "
            f"choice with no path to it"
        )
    print(f"  explicitly requestable: {sorted(reached)}")

    # -- 5. the routing methods the pipeline calls ------------------------
    print(f"\ncapability methods routed: {sorted(CAPABILITY_BY_METHOD)}")

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nOK: every advertised aspect is request-able, or declared unreachable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
