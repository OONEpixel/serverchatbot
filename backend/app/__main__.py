import asyncio
import logging

from .config import Settings
from .service import MatrixBridgeService


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    settings = Settings.from_environment()
    asyncio.run(MatrixBridgeService(settings).run())


if __name__ == "__main__":
    main()
