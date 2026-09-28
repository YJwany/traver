"""Run the API using the configured host, port and log level.

    python -m travel_api

This exists so ``API_HOST`` / ``API_PORT`` / ``LOG_LEVEL`` in ``Settings`` are
actually honoured. Previously the only documented way to start the server was a
hand-written ``uvicorn --host ... --port ...`` command, which meant those three
settings were dead configuration: changing them had no effect.
"""

from __future__ import annotations

import uvicorn

from travel_api.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "travel_api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.app_env == "development",
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
