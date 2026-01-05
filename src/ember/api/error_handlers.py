"""Global exception handlers for the API."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ember.exceptions import EmberError
from ember.logging import get_logger

logger = get_logger(__name__)


async def ember_exception_handler(request: Request, exc: EmberError) -> JSONResponse:
    """Handle custom Ember exceptions."""
    logger.warning(f"{exc.error_code}: {exc.message}", extra={"details": exc.details})
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_dict(),
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle unexpected exceptions."""
    logger.error(f"Unexpected error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "INTERNAL_ERROR",
            "message": "An unexpected error occurred",
            "details": {},
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """
    Register global exception handlers.

    Args:
        app: FastAPI application instance
    """
    app.add_exception_handler(EmberError, ember_exception_handler)
    app.add_exception_handler(Exception, generic_exception_handler)
