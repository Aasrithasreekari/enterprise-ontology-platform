"""Platform error hierarchy. API/MCP layers translate these into transport-specific errors."""

from __future__ import annotations


class PlatformError(Exception):
    code = "platform_error"
    status_code = 500


class ValidationError(PlatformError):
    code = "validation_error"
    status_code = 422


class NotFoundError(PlatformError):
    code = "not_found"
    status_code = 404


class AuthenticationError(PlatformError):
    code = "unauthenticated"
    status_code = 401


class AuthorizationError(PlatformError):
    code = "forbidden"
    status_code = 403


class ConflictError(PlatformError):
    code = "conflict"
    status_code = 409


class ConnectorError(PlatformError):
    code = "connector_error"
    status_code = 502


class TransientConnectorError(ConnectorError):
    """Retryable failure (rate limit, 5xx, network)."""

    code = "connector_transient_error"
