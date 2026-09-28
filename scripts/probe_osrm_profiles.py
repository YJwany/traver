"""Does a real foot profile exist anywhere free?

Why this matters: the capability matrix currently reports 步行/骑行 as MISS, and the public
OSRM demo server genuinely serves only the car profile (requesting /foot/ returns car
results). That is why OsmRoutingProvider *derives* walking durations from a speed constant
and flags them as estimates rather than presenting a car time as a pedestrian time - correct,
but it leaves two aspects unserved.

FOSSGIS e.V. runs routing.openstreetmap.de with separate foot/bike/car instances. If a real
foot profile exists there, walking becomes a measured fact rather than a derived estimate,
and 步行/骑行 can move from MISS to ok.

The control matters: if the known-good demo server also fails here, the probe is broken and
says nothing about FOSSGIS.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

# 西湖 -> 灵隐寺: a real cross-town Hangzhou leg, ~5.5 km on the road.
COORDS = "120.1550,30.2489;120.1010,30.2410"

CANDIDATES = [
    ("fossgis", "https://routing.openstreetmap.de/routed-foot/route/v1/foot/", "foot"),
    ("fossgis", "https://routing.openstreetmap.de/routed-bike/route/v1/bike/", "bike"),
    ("fossgis", "https://routing.openstreetmap.de/routed-car/route/v1/driving/", "driving"),
    ("demo", "https://router.project-osrm.org/route/v1/driving/", "driving"),
    ("demo", "https://router.project-osrm.org/route/v1/foot/", "foot"),
]


def probe(base: str, profile: str) -> dict:
    url = f"{base}{COORDS}?overview=false"
    request = urllib.request.Request(url, headers={"User-Agent": "traver-capability-probe/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return {"error": f"HTTP {exc.code} {body}"}
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
        return {"error": f"{type(exc).__name__}: {exc}"}

    if payload.get("code") != "Ok":
        return {"error": f"code={payload.get('code')} {payload.get('message', '')}"}
    leg = payload["routes"][0]["legs"][0]
    distance = leg["distance"]
    duration = leg["duration"]
    return {
        "distance_m": round(distance),
        "duration_s": round(duration),
        "speed_kmh": round(distance / duration * 3.6, 1) if duration else None,
    }


def main() -> None:
    print(f"proxy: {os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy') or '(none)'}")
    print(f"leg: {COORDS}  (西湖 -> 灵隐寺)\n")
    print(f"{'service':8} {'profile':9} {'distance':>9} {'duration':>9} {'km/h':>6}   result")
    for service, base, profile in CANDIDATES:
        result = probe(base, profile)
        if "error" in result:
            print(f"{service:8} {profile:9} {'-':>9} {'-':>9} {'-':>6}   {result['error']}")
        else:
            print(
                f"{service:8} {profile:9} {result['distance_m']:>7} m "
                f"{result['duration_s']:>7} s {result['speed_kmh']:>6}   ok"
            )


if __name__ == "__main__":
    main()
