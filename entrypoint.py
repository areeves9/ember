"""
Ember Entrypoint

Main entry point for running the Ember API proxy.
Configures logging before importing the app to ensure proper initialization order.
"""

import uvicorn
from dotenv import load_dotenv

# Load environment first
load_dotenv()

# Import config first
from ember.config import settings
from ember.logging import (BOLD_CYAN, BOLD_YELLOW, RESET, YELLOW,
                           configure_logging, get_logger)
# Import the app
from ember.main import app

# Configure logging BEFORE starting server
configure_logging(settings)

# Get logger
logger = get_logger(__name__)

# Display EMBER ASCII art banner
EMBER_BANNER = f"""
{BOLD_CYAN}███████╗███╗   ███╗██████╗ ███████╗██████╗ {RESET}
{BOLD_CYAN}██╔════╝████╗ ████║██╔══██╗██╔════╝██╔══██╗{RESET}
{BOLD_CYAN}█████╗  ██╔████╔██║██████╔╝█████╗  ██████╔╝{RESET}
{BOLD_CYAN}██╔══╝  ██║╚██╔╝██║██╔══██╗██╔══╝  ██╔══██╗{RESET}
{BOLD_CYAN}███████╗██║ ╚═╝ ██║██████╔╝███████╗██║  ██║{RESET}
{BOLD_CYAN}╚══════╝╚═╝     ╚═╝╚═════╝ ╚══════╝╚═╝  ╚═╝{RESET}

{YELLOW}v{settings.app_version} | {settings.environment.upper()} | Wildfire Data Proxy{RESET}
"""

print(EMBER_BANNER)

# Log startup info if in development
if settings.is_development:
    logger.info(f"{BOLD_YELLOW}HOT RELOAD ACTIVE (development mode){RESET}")
    logger.info(f"{YELLOW}Environment: {settings.environment}{RESET}")
    logger.info(f"{YELLOW}Log Level: {settings.log_level}{RESET}")
    logger.info(f"{YELLOW}Log Format: {settings.log_format}{RESET}")

if __name__ == "__main__":
    logger.info(
        f"Starting Uvicorn on {BOLD_CYAN}http://{settings.host}:{settings.port}{RESET}"
    )

    try:
        uvicorn.run(
            "ember.main:app",
            host=settings.host,
            port=settings.port,
            reload=settings.is_development,
            log_level=settings.log_level.lower(),
            access_log=False,
        )
    except Exception as e:
        logger.error(f"Failed to launch Uvicorn: {e}", exc_info=True)
        raise
