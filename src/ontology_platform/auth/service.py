"""Authentication (API keys, JWT) and audit logging."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from typing import Any

import jwt
from sqlalchemy import select

from ontology_platform.auth.principal import Principal, normalize_customers, validate_roles
from ontology_platform.config import Settings
from ontology_platform.errors import AuthenticationError, NotFoundError
from ontology_platform.observability import get_logger
from ontology_platform.storage.db import Database
from ontology_platform.storage.orm import ApiKey, AuditLog, Tenant, utcnow

log = get_logger(__name__)

KEY_PREFIX = "op"

REDACTED = "***redacted***"
# Parameter names whose values are credentials (matched case-insensitively as substrings).
_SENSITIVE_KEY = re.compile(r"secret|password|passwd|token|api[_-]?key|authorization|credential|private[_-]?key", re.IGNORECASE)
# A secret_ref is safe to record only when it is a reference, never an inline value.
_SECRET_REFERENCE = re.compile(r"^(env:[A-Za-z_][A-Za-z0-9_]*|file:.+)$")
# Platform API keys and bearer JWTs embedded anywhere in a string value.
_INLINE_CREDENTIAL = re.compile(rf"\b{KEY_PREFIX}_[0-9a-f]+_[A-Za-z0-9_-]{{16,}}|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")


def redact(value: Any, key: str | None = None) -> Any:
    """Return a copy of ``value`` with credentials removed, for audit records and logs."""
    if key is not None and _SENSITIVE_KEY.search(key):
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if key.lower() == "secret_ref" and isinstance(value, str) and _SECRET_REFERENCE.match(value):
            return value
        return REDACTED
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _INLINE_CREDENTIAL.sub(REDACTED, value)
    return value


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class AuthService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    # ------------------------------------------------------------------ API keys
    def create_api_key(
        self, tenant_id: str, name: str, roles: list[str], allowed_customers: list[str] | None = None
    ) -> tuple[str, dict[str, Any]]:
        """Create a key. The plaintext is returned exactly once and never stored."""
        role_set = validate_roles(roles)
        customers = normalize_customers(allowed_customers)
        key_id = secrets.token_hex(6)
        secret = secrets.token_urlsafe(32)
        with self.db.session() as s:
            if s.get(Tenant, tenant_id) is None:
                raise NotFoundError(f"tenant '{tenant_id}' not found")
            s.add(
                ApiKey(
                    id=key_id,
                    tenant_id=tenant_id,
                    name=name,
                    key_hash=_hash(secret),
                    roles=sorted(role_set),
                    allowed_customers=sorted(customers) if customers is not None else None,
                )
            )
        meta = {"id": key_id, "tenant_id": tenant_id, "name": name, "roles": sorted(role_set), "allowed_customers": sorted(customers) if customers is not None else None}
        return f"{KEY_PREFIX}_{key_id}_{secret}", meta

    def list_api_keys(self, tenant_id: str) -> list[dict[str, Any]]:
        with self.db.session() as s:
            rows = s.scalars(select(ApiKey).where(ApiKey.tenant_id == tenant_id).order_by(ApiKey.created_at))
            return [
                {
                    "id": k.id,
                    "name": k.name,
                    "roles": k.roles,
                    "allowed_customers": k.allowed_customers,
                    "revoked": k.revoked,
                    "created_at": k.created_at.isoformat(),
                    "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
                }
                for k in rows
            ]

    def revoke_api_key(self, tenant_id: str, key_id: str) -> None:
        with self.db.session() as s:
            key = s.get(ApiKey, key_id)
            if key is None or key.tenant_id != tenant_id:
                raise NotFoundError(f"api key '{key_id}' not found")
            key.revoked = True

    # ------------------------------------------------------------------ authentication
    def authenticate(self, token: str | None) -> Principal:
        if not token:
            raise AuthenticationError("missing credentials")
        token = token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if token.startswith(f"{KEY_PREFIX}_"):
            return self._authenticate_api_key(token)
        if token.count(".") == 2:
            return self._authenticate_jwt(token)
        raise AuthenticationError("unrecognized credential format")

    def _authenticate_api_key(self, token: str) -> Principal:
        parts = token.split("_", 2)
        if len(parts) != 3:
            raise AuthenticationError("malformed api key")
        _, key_id, secret = parts
        with self.db.session() as s:
            key = s.get(ApiKey, key_id)
            if key is None or key.revoked or not hmac.compare_digest(key.key_hash, _hash(secret)):
                raise AuthenticationError("invalid api key")
            key.last_used_at = utcnow()
            return Principal(
                subject=f"api_key:{key.id}:{key.name}",
                tenant_id=key.tenant_id,
                roles=frozenset(key.roles or []),
                allowed_customers=frozenset(key.allowed_customers) if key.allowed_customers is not None else None,
                auth_method="api_key",
            )

    def _authenticate_jwt(self, token: str) -> Principal:
        if self.settings.jwt_key is None:
            raise AuthenticationError("JWT authentication is not configured")
        options = {"require": ["exp", "sub"]}
        try:
            claims = jwt.decode(
                token,
                self.settings.jwt_key.get_secret_value(),
                algorithms=[self.settings.jwt_algorithm],
                audience=self.settings.jwt_audience,
                issuer=self.settings.jwt_issuer,
                options=options,
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError(f"invalid token: {exc}") from exc
        tenant_id = claims.get("tenant") or claims.get("tid")
        if not tenant_id:
            raise AuthenticationError("token has no tenant claim")
        if not isinstance(tenant_id, str):
            raise AuthenticationError("tenant claim must be a string")
        with self.db.session() as s:
            if s.get(Tenant, tenant_id) is None:
                raise AuthenticationError("unknown tenant")
        roles = claims.get("roles") or []
        if not isinstance(roles, list):
            raise AuthenticationError("roles claim must be a list")
        try:
            role_set = validate_roles([str(r) for r in roles])
        except Exception as exc:
            raise AuthenticationError(str(exc)) from exc
        # A present-but-malformed customers claim must fail closed: treating it as absent would grant
        # access to every customer of the tenant.
        customers = claims.get("customers")
        if customers is not None and (not isinstance(customers, list) or not all(isinstance(c, str) for c in customers)):
            raise AuthenticationError("customers claim must be a list of strings")
        return Principal(
            subject=f"jwt:{claims['sub']}",
            tenant_id=tenant_id,
            roles=role_set,
            allowed_customers=normalize_customers(customers),
            auth_method="jwt",
        )


def audit(
    db: Database, principal: Principal | None, channel: str, action: str, params: dict[str, Any], outcome: str, detail: str | None = None
) -> None:
    """Persist an audit record. Failures to audit are logged, never raised into the request."""
    try:
        encoded = json.dumps(redact(json.loads(json.dumps(params, default=str))), default=str)
        safe = json.loads(encoded) if len(encoded) <= 4000 else {"truncated": True}
        with db.session() as s:
            s.add(
                AuditLog(
                    tenant_id=principal.tenant_id if principal else "-",
                    principal=principal.subject if principal else "anonymous",
                    channel=channel,
                    action=action,
                    params=safe,
                    outcome=outcome,
                    detail=redact(detail or "")[:2000] or None,
                )
            )
    except Exception:  # pragma: no cover - defensive
        log.exception("audit write failed", extra={"action": action})
