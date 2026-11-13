"""Entry point: ``uv run mimcp`` / ``python -m mimcp``."""

from __future__ import annotations

import uvicorn

from .app import create_app
from .config import Settings


def main() -> None:
    settings = Settings.from_env()
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
