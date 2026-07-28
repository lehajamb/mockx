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

    latency = config["latency"]
    if "engine_ms" not in latency:
        raise ValueError("Missing required configuration field: latency.engine_ms")
    if (
        "network_ms" not in latency
        and "inbound_ms" not in latency
        and "outbound_ms" not in latency
    ):
        raise ValueError(
            "Configure latency.network_ms or explicit inbound_ms/outbound_ms"
        )
    symbols = config["symbols"]
    if not isinstance(symbols, list) or not symbols:
        raise ValueError("symbols must be a non-empty list")
    normalized_symbols = [str(symbol).strip().upper() for symbol in symbols]
    if any(not symbol for symbol in normalized_symbols):
        raise ValueError("symbols cannot contain empty values")
    if len(normalized_symbols) != len(set(normalized_symbols)):
        raise ValueError("symbols must not contain duplicates")
    config["symbols"] = normalized_symbols

    execution = config["execution"]
    if "reject_prob" not in execution:
        raise ValueError(
            "Missing required configuration field: execution.reject_prob"
        )
    if "random_seed" not in execution:
        raise ValueError(
            "Missing required configuration field: execution.random_seed"
        )
    random_seed = execution["random_seed"]
    if not isinstance(random_seed, int) or isinstance(random_seed, bool):
        raise ValueError("execution.random_seed must be an integer")

    admin_api = config["admin_api"]
    admin_api.setdefault("host", "127.0.0.1")
    admin_api.setdefault("enabled", True)
    if not isinstance(admin_api["host"], str) or not admin_api["host"].strip():
        raise ValueError("admin_api.host must be a non-empty string")
    port = admin_api.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("admin_api.port must be between 1 and 65535")
    if not isinstance(admin_api["enabled"], bool):
        raise ValueError("admin_api.enabled must be true or false")

    fix_config = config.setdefault("fix", {})
    fix_config.setdefault("delivery_retry_interval_ms", 1_000)
    fix_config.setdefault("delivery_max_attempts_per_cycle", 3)
    for field in (
        "delivery_retry_interval_ms",
        "delivery_max_attempts_per_cycle",
    ):
        value = fix_config[field]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
        ):
            raise ValueError(f"fix.{field} must be a positive integer")

    scheduler = config.setdefault("scheduler", {})
    scheduler.setdefault("mode", "external")
    scheduler.setdefault("interval_ms", 10)
    scheduler.setdefault("max_advance_ms", 86_400_000)
    scheduler.setdefault("max_events_per_advance", 100_000)
    if scheduler["mode"] not in {"external", "realtime"}:
        raise ValueError("scheduler.mode must be 'external' or 'realtime'")
    if (
        not isinstance(scheduler["interval_ms"], int)
        or scheduler["interval_ms"] <= 0
    ):
        raise ValueError("scheduler.interval_ms must be a positive integer")
    if (
        not isinstance(scheduler["max_advance_ms"], int)
        or isinstance(scheduler["max_advance_ms"], bool)
        or scheduler["max_advance_ms"] <= 0
    ):
        raise ValueError(
            "scheduler.max_advance_ms must be a positive integer"
        )
    if (
        not isinstance(scheduler["max_events_per_advance"], int)
        or isinstance(scheduler["max_events_per_advance"], bool)
        or scheduler["max_events_per_advance"] <= 0
    ):
        raise ValueError(
            "scheduler.max_events_per_advance must be a positive integer"
        )

    websocket = config.setdefault("websocket", {})
    websocket.setdefault("heartbeat_interval_seconds", 10)
    websocket.setdefault("outbound_queue_capacity", 8)
    websocket.setdefault("send_timeout_seconds", 5)
    websocket.setdefault("heartbeat_stale_seconds", 30)
    websocket.setdefault("market_data_stale_seconds", 30)
    websocket.setdefault("fallback_poll_seconds", 2)
    websocket.setdefault("reconnect_initial_seconds", 1)
    websocket.setdefault("reconnect_max_seconds", 30)
    websocket.setdefault("staleness_check_seconds", 1)
    heartbeat = websocket["heartbeat_interval_seconds"]
    queue_capacity = websocket["outbound_queue_capacity"]
    send_timeout = websocket["send_timeout_seconds"]
    heartbeat_stale = websocket["heartbeat_stale_seconds"]
    stale = websocket["market_data_stale_seconds"]
    fallback = websocket["fallback_poll_seconds"]
    reconnect_initial = websocket["reconnect_initial_seconds"]
    reconnect_max = websocket["reconnect_max_seconds"]
    staleness_check = websocket["staleness_check_seconds"]
    if (
        not isinstance(heartbeat, (int, float))
        or isinstance(heartbeat, bool)
        or heartbeat <= 0
    ):
        raise ValueError(
            "websocket.heartbeat_interval_seconds must be positive"
        )
    if (
        not isinstance(queue_capacity, int)
        or isinstance(queue_capacity, bool)
        or queue_capacity <= 0
    ):
        raise ValueError(
            "websocket.outbound_queue_capacity must be a positive integer"
        )
    if (
        not isinstance(send_timeout, (int, float))
        or isinstance(send_timeout, bool)
        or send_timeout <= 0
    ):
        raise ValueError("websocket.send_timeout_seconds must be positive")
    if (
        not isinstance(heartbeat_stale, (int, float))
        or isinstance(heartbeat_stale, bool)
        or heartbeat_stale <= heartbeat
    ):
        raise ValueError(
            "websocket.heartbeat_stale_seconds must exceed heartbeat cadence"
        )
    if (
        not isinstance(stale, (int, float))
        or isinstance(stale, bool)
        or stale <= heartbeat
    ):
        raise ValueError(
            "websocket.market_data_stale_seconds must exceed heartbeat cadence"
        )
    if (
        not isinstance(fallback, (int, float))
        or isinstance(fallback, bool)
        or fallback <= 0
    ):
        raise ValueError("websocket.fallback_poll_seconds must be positive")
    if (
        not isinstance(reconnect_initial, (int, float))
        or isinstance(reconnect_initial, bool)
        or reconnect_initial <= 0
    ):
        raise ValueError(
            "websocket.reconnect_initial_seconds must be positive"
        )
    if (
        not isinstance(reconnect_max, (int, float))
        or isinstance(reconnect_max, bool)
        or reconnect_max < reconnect_initial
    ):
        raise ValueError(
            "websocket.reconnect_max_seconds must be at least "
            "reconnect_initial_seconds"
        )
    if (
        not isinstance(staleness_check, (int, float))
        or isinstance(staleness_check, bool)
        or staleness_check <= 0
    ):
        raise ValueError(
            "websocket.staleness_check_seconds must be positive"
        )

    cors_origins = config.setdefault("cors_origins", [])
    if not isinstance(cors_origins, list):
        raise ValueError("cors_origins must be a list")
    if "*" in cors_origins:
        raise ValueError("Permissive '*' CORS is not enabled by default")

    config.setdefault("prometheus", {"enabled": True})
    config["prometheus"].setdefault("enabled", True)
    if not isinstance(config["prometheus"]["enabled"], bool):
        raise ValueError("prometheus.enabled must be true or false")
    if config.get("tsdb"):
        config["tsdb"].setdefault("batch_size", 1000)
        if config["tsdb"].get("enabled", True):
            missing = [
                field
                for field in ("url", "token", "org", "bucket")
                if not config["tsdb"].get(field)
            ]
            if missing:
                raise ValueError(
                    "Missing TSDB configuration fields: "
                    + ", ".join(f"tsdb.{field}" for field in missing)
                )

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
