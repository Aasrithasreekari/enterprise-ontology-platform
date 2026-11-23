"""Label/tag conventions used to derive topology from infrastructure metadata.

Kept in one place so an enterprise can adapt them to its own tagging standard.
"""

from __future__ import annotations

import re

from ontology_platform.ontology.schema import normalize_tier

# Kubernetes labels (first present wins)
K8S_APPLICATION_LABELS = ("ontology.io/application", "app.kubernetes.io/part-of")
K8S_SERVICE_LABELS = ("ontology.io/service", "app.kubernetes.io/name", "app")
K8S_CUSTOMER_LABELS = ("ontology.io/customer", "customer")
K8S_ENVIRONMENT_LABELS = ("ontology.io/environment", "environment", "env")
K8S_USES_ANNOTATION = "ontology.io/uses"
K8S_DEPENDS_ON_ANNOTATION = "ontology.io/depends-on"

# Cloud tags (matched case-insensitively, optional "ontology:" prefix)
TAG_CUSTOMER = ("customer", "ontology:customer", "client")
TAG_ENVIRONMENT = ("environment", "env", "ontology:environment", "stage")
TAG_APPLICATION = ("application", "app", "ontology:application")
TAG_DATABASE = ("database", "ontology:database")
TAG_CLUSTER = ("cluster", "ontology:cluster")

# Confluence label prefixes → entity type value
DOC_LABEL_PREFIXES = {
    "app": "application",
    "application": "application",
    "svc": "service",
    "service": "service",
    "api": "api",
    "repo": "repository",
    "product": "product",
    "customer": "customer",
    "db": "database",
    "cluster": "kubernetes_cluster",
}

KUBERNETES_CLUSTER_RESOURCE_TYPES = {
    "aws:eks:cluster",
    "microsoft.containerservice/managedclusters",
    "container.googleapis.com/cluster",
}
DATABASE_RESOURCE_TYPES = {
    "aws:rds:db",
    "aws:rds:cluster",
    "aws:dynamodb:table",
    "microsoft.sql/servers/databases",
    "microsoft.dbforpostgresql/flexibleservers",
    "microsoft.documentdb/databaseaccounts",
    "sqladmin.googleapis.com/instance",
    "spanner.googleapis.com/instance",
}

_NAMESPACE_PATTERN = re.compile(r"^(?P<customer>[a-z0-9][a-z0-9-]*?)-(?P<tier>[a-z]+)$")


def first_label(labels: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = labels.get(key)
        if value:
            return value
    return None


def first_tag(tags: dict[str, str], keys: tuple[str, ...]) -> str | None:
    lowered = {k.lower(): v for k, v in tags.items()}
    for key in keys:
        value = lowered.get(key)
        if value:
            return value
    return None


def customer_tier_from_namespace(namespace: str) -> tuple[str | None, str | None]:
    """'acme-prod' → ('acme', 'prod'); returns (None, None) if the suffix is not a known tier."""
    match = _NAMESPACE_PATTERN.match(namespace.lower())
    if not match or normalize_tier(match.group("tier")) is None:
        return None, None
    return match.group("customer"), match.group("tier")


def provider_from_resource_id(resource_id: str) -> str | None:
    rid = resource_id.strip()
    if rid.startswith("arn:aws"):
        return "aws"
    if rid.lower().startswith("/subscriptions/"):
        return "azure"
    if rid.startswith("//") and ".googleapis.com/" in rid:
        return "gcp"
    return None
