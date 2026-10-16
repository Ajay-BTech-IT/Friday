"""Entry point. Uses the selector event loop explicitly (uvicorn would pick Proactor on Windows,
which psycopg async cannot use)."""
import asyncio
import sys

import uvicorn

from config import settings


def main() -> None:
    config = uvicorn.Config("main:app", host=settings.host, port=settings.port, log_level="info")
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(server.serve())


if __name__ == "__main__":
    main()
