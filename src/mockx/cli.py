"""Command-line interface for the mock exchange."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .configuration import ConfigurationError, load_config
from .main import MockExchange


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("quickfix").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MockX - FIX-speaking mock exchange"
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("CFG_PATH", "/etc/mockx/config.yml"),
        help="Path to configuration YAML",
    )
    parser.add_argument(
        "--fix-config",
        default=os.environ.get("FIX_CONFIG_FILE"),
        help="Path to QuickFIX settings",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()
    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    try:
        config = load_config(args.config)
        logger.info("Starting %s", config.exchange_name)
        MockExchange(config, fix_cfg=args.fix_config).run()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down")
    except ConfigurationError as exc:
        for issue in exc.issues:
            logger.error(
                "Configuration error [%s] %s: %s",
                issue.code,
                issue.path,
                issue.message,
            )
        sys.exit(1)
    except Exception:
        logger.exception("Failed to start exchange")
        sys.exit(1)


if __name__ == "__main__":
    main()
