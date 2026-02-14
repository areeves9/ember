"""
Centralized logging configuration for Ember.

Provides:
- Structured logging with JSON support
- Consistent formatting across all modules
- Colorized output for development mode

Usage:
    from ember.logging import get_logger

    logger = get_logger(__name__)
    logger.info("Hello from my module")
"""

import logging
import sys
from typing import Any

# ───────────────────────────────
# ANSI Color Codes
# ───────────────────────────────
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
MAGENTA = "\033[95m"

BOLD_CYAN = "\033[1;36m"
BOLD_GREEN = "\033[1;32m"
BOLD_YELLOW = "\033[1;33m"
BOLD_RED = "\033[1;31m"
BOLD_MAGENTA = "\033[1;35m"


# ───────────────────────────────
# DEVELOPMENT FORMATTER
# ───────────────────────────────
class DevFormatter(logging.Formatter):
    """
    Human-friendly colorized formatter for local development.

    Format:
        ember   | 2025-12-06 07:00:23,750 | INFO | module_name | Message...
    """

    LEVEL_COLORS = {
        "DEBUG": CYAN,
        "INFO": GREEN,
        "WARNING": YELLOW,
        "ERROR": RED,
        "CRITICAL": MAGENTA,
    }

    def format(self, record: logging.LogRecord) -> str:
        """Format log record with colors."""
        ts = self.formatTime(record, "%Y-%m-%d %H:%M:%S,%f")[:-3]
        level = record.levelname
        color = self.LEVEL_COLORS.get(level, "")
        name = record.name
        msg = record.getMessage()

        # Emphasize errors
        if record.levelno >= logging.ERROR:
            msg = f"{BOLD}{msg}{RESET}"

        log_line = (
            f"ember   | {DIM}{ts}{RESET} | "
            f"{color}{level:<8}{RESET} | "
            f"{name} | {msg}"
        )

        if record.exc_info:
            log_line += "\n" + self.formatException(record.exc_info)

        return log_line


class StructuredFormatter(logging.Formatter):
    """
    Custom formatter for structured logging.

    Supports both JSON and text formats based on configuration.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        import json

        log_data = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data)


def configure_logging(config: Any) -> None:
    """
    Configure logging for the entire application.

    This should be called once at application startup.
    Sets up handlers, formatters, and log levels.

    Args:
        config: Settings instance
    """
    # Get root logger
    root_logger = logging.getLogger()

    # Clear existing handlers
    root_logger.handlers.clear()

    # Set log level from config
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)
    root_logger.setLevel(log_level)

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)

    # Create formatter based on environment and log format
    if config.log_format == "json":
        formatter = StructuredFormatter()
    elif config.is_development:
        # Use colorized formatter in development
        formatter = DevFormatter()
    else:
        # Plain text formatter for production
        formatter = logging.Formatter(
            fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    console_handler.setFormatter(formatter)

    # Add handler to root logger
    root_logger.addHandler(console_handler)

    # Suppress noisy loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # Log configuration
    logger = logging.getLogger(__name__)
    logger.debug(
        f"Logging configured: level={config.log_level}, format={config.log_format}"
    )


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for the given name.

    This is the primary way modules should get loggers.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Logger instance

    Example:
        ```python
        from ember.logging import get_logger

        logger = get_logger(__name__)
        logger.info("Hello!")
        ```
    """
    return logging.getLogger(name)
