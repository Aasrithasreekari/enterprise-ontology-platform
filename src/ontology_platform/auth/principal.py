"""Principals, roles and permissions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ontology_platform.errors import AuthorizationError
from ontology_platform.ontology.keys import normalize_name
from ontology_platform.storage.repository import Scope


class Permission(StrEnum):
    GRAPH_READ = "graph:read"
    CONNECTORS_READ = "connectors:read"
    CONNECTORS_WRITE = "connectors:write"
    INGEST_RUN = "ingest:run"
    KEYS_MANAGE = "keys:manage"
    AUDIT_READ = "audit:read"


ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "viewer": frozenset({Permission.GRAPH_READ}),
    "ingestor": frozenset({Permission.GRAPH_READ, Permission.CONNECTORS_READ, Permission.INGEST_RUN}),
    "admin": frozenset(Permission),
}


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    roles: frozenset[str]
    allowed_customers: frozenset[str] | None = None  # None → all customers of the tenant
    auth_method: str = "api_key"

    @property
    def permissions(self) -> frozenset[Permission]:
        perms: set[Permission] = set()
        for role in self.roles:
            perms |= ROLE_PERMISSIONS.get(role, frozenset())
        return frozenset(perms)

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        if not self.has(permission):
            raise AuthorizationError(f"'{self.subject}' lacks permission '{permission.value}'")

    @property
    def scope(self) -> Scope:
        return Scope(self.tenant_id, self.allowed_customers)


def validate_roles(roles: list[str]) -> frozenset[str]:
    unknown = set(roles) - set(ROLE_PERMISSIONS)
    if unknown or not roles:
        from ontology_platform.errors import ValidationError

        raise ValidationError(f"roles must be a non-empty subset of {sorted(ROLE_PERMISSIONS)}; unknown: {sorted(unknown)}")
    return frozenset(roles)


def normalize_customers(customers: list[str] | None) -> frozenset[str] | None:
    if customers is None:
        return None
    return frozenset(normalize_name(c) for c in customers if c.strip())
