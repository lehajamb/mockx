"""Command-line interface for the mock exchange."""

import argparse
import logging
import sys
import os
from pathlib import Path
import yaml

from .main import MockExchange


def setup_logging(level: str = "INFO"):
    """Setup logging configuration."""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Suppress some verbose loggers
    logging.getLogger("quickfix").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def load_config(config_path: str) -> dict:
    """Load configuration from YAML file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
        
    with open(path, 'r') as f:
        config = yaml.safe_load(f)
        
    # Validate required fields
    required_fields = ["exchange_name", "symbols", "latency", "execution", "admin_api"]
    for field in required_fields:
        if field not in config:
            raise ValueError(f"Missing required configuration field: {field}")
            
    return config


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="MockX - FIX-speaking mock exchange")
    parser.add_argument(
        "--config",
        type=str,
        default=os.environ.get("CFG_PATH", "/etc/mockx/config.yml"),
        help="Path to configuration file"
    )
    parser.add_argument(
        "--fix-config",
        type=str,
        default=os.environ.get("FIX_CONFIG_FILE"),
        help="Path to FIX configuration file (.cfg)"
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level"
    )
    
    args = parser.parse_args()
    
    # Setup logging
    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)
    
    try:
        # Load configuration
        logger.info(f"Loading configuration from: {args.config}")
        config = load_config(args.config)
        
        # Log FIX config file if provided
        if args.fix_config:
            logger.info(f"Using FIX configuration file: {args.fix_config}")
        
        # Create and start exchange
        logger.info(f"Starting {config['exchange_name']}")
        exchange = MockExchange(config, fix_cfg=args.fix_config)
        exchange.run()
        
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Failed to start exchange: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
