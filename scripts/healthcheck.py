"""Container healthcheck: fail unless the API reports itself ready.

Exists as a file rather than an inline ``python -c`` because quoting a JSON check
inside a Dockerfile HEALTHCHECK is unreadable and easy to get subtly wrong. Using
a script also makes the exit-code contract explicit.

Exits 0 when ``/api/v1/health`` returns 200 with ``ready: true``, non-zero
otherwise, so Docker marks the container unhealthy when the API process is alive
but cannot reach its database.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8000/api/v1/health"


def main() -> int:
    url = os.environ.get("HEALTHCHECK_URL", DEFAULT_URL)
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            if response.status != 200:
                print(f"unhealthy: HTTP {response.status}", file=sys.stderr)
                return 1
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        print(f"unhealthy: cannot reach {url}: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"unhealthy: bad response from {url}: {exc}", file=sys.stderr)
        return 1

    # `ready` is the real signal; `status` alone can read "degraded".
    if not payload.get("ready"):
        print(
            f"unhealthy: ready=false ({payload.get('database_error', 'no detail')})",
            file=sys.stderr,
        )
        return 1

    print(f"healthy: database={payload.get('database')} auth={payload.get('authentication')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
