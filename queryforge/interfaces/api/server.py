"""Run the optional QueryForge FastAPI server with Uvicorn."""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the QueryForge REST API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    try:
        import uvicorn
    except ImportError:
        print(
            "QueryForge API unavailable: install with "
            "pip install -r requirements-server.txt"
        )
        return 1
    uvicorn.run(
        "queryforge.interfaces.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
