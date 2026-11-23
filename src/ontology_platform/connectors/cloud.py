"""Cloud inventory connectors: AWS (Resource Groups Tagging API), Azure (Resource Graph), GCP (Cloud Asset).

Ownership/topology is read from tags/labels (configurable keys, defaults ``customer``,
``environment``, ``application``, ``database``). Not verified against live accounts.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import jwt
from pydantic import BaseModel, Field

from ontology_platform.connectors.base import (
    CloudResourcePayload,
    Connector,
    RecordKind,
    SourceRecord,
    register_connector,
)
from ontology_platform.connectors.http import RetryingHttpClient
from ontology_platform.errors import ConnectorError


def _record(payload: CloudResourcePayload, url: str | None = None) -> SourceRecord:
    return SourceRecord(
        kind=RecordKind.CLOUD_RESOURCE,
        external_id=f"{payload.provider}:{payload.resource_id}",
        source_system=payload.provider,
        source_url=url,
        payload=payload.model_dump(),
    )


# ----------------------------------------------------------------------------- AWS
class AwsConfig(BaseModel):
    regions: list[str] = Field(min_length=1)
    resource_type_filters: list[str] = Field(default_factory=list, description="e.g. ['eks:cluster','rds:db']")
    profile: str | None = None


def _aws_resource_type(arn: str) -> str:
    # arn:partition:service:region:account:resource-type/name  or  ...:resource-type:name
    parts = arn.split(":", 5)
    service = parts[2] if len(parts) > 2 else "unknown"
    resource = parts[5] if len(parts) > 5 else ""
    rtype = resource.split("/", 1)[0].split(":", 1)[0] if ("/" in resource or ":" in resource) else resource
    return f"aws:{service}:{rtype}"


@register_connector
class AwsConnector(Connector):
    """Uses boto3's default credential chain (env vars, profile, instance/IRSA role); no secret_ref needed."""

    type = "aws"
    source_system = "aws"
    config_model = AwsConfig
    requires_secret = False
    config: AwsConfig

    # Injection point for tests: callable(region) -> client exposing get_paginator("get_resources").
    client_factory: Callable[[str], Any] | None = None

    def _client(self, region: str) -> Any:
        if self.client_factory is not None:
            return self.client_factory(region)
        try:
            import boto3  # optional dependency: pip install .[aws]
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ConnectorError("AWS connector requires boto3 (pip install 'ontology-platform[aws]')") from exc
        session = boto3.Session(profile_name=self.config.profile) if self.config.profile else boto3.Session()
        return session.client("resourcegroupstaggingapi", region_name=region)

    async def fetch(self, since: str | None = None) -> AsyncIterator[SourceRecord]:
        import asyncio

        for region in self.config.regions:
            client = self._client(region)

            def _list() -> list[dict]:
                kwargs: dict[str, Any] = {"ResourcesPerPage": 100}
                if self.config.resource_type_filters:
                    kwargs["ResourceTypeFilters"] = self.config.resource_type_filters
                out: list[dict] = []
                for page in client.get_paginator("get_resources").paginate(**kwargs):
                    out.extend(page.get("ResourceTagMappingList", []))
                return out

            try:
                mappings = await asyncio.to_thread(_list)  # boto3 is blocking
            except Exception as exc:  # botocore raises many types; normalize
                raise ConnectorError(f"AWS get_resources failed in {region}: {exc}") from exc
            for m in mappings:
                arn = m["ResourceARN"]
                tags = {t["Key"]: t["Value"] for t in m.get("Tags", [])}
                parts = arn.split(":")
                name = tags.get("Name") or arn.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
                yield _record(
                    CloudResourcePayload(
                        provider="aws",
                        resource_id=arn,
                        resource_type=_aws_resource_type(arn),
                        name=name,
                        region=parts[3] if len(parts) > 3 and parts[3] else region,
                        account=parts[4] if len(parts) > 4 else None,
                        tags=tags,
                    )
                )


# ----------------------------------------------------------------------------- Azure
class AzureConfig(BaseModel):
    subscriptions: list[str] = Field(min_length=1)
    query: str = "Resources | project id, name, type, location, subscriptionId, tags"
    management_url: str = "https://management.azure.com"
    login_url: str = "https://login.microsoftonline.com"


@register_connector
class AzureConnector(Connector):
    """Secret: JSON service principal ``{"tenant_id","client_id","client_secret"}`` or a raw bearer token."""

    type = "azure"
    source_system = "azure"
    config_model = AzureConfig
    config: AzureConfig

    async def _token(self) -> str:
        secret = (self.secret or "").strip()
        if not secret.startswith("{"):
            return secret
        creds = json.loads(secret)
        http = RetryingHttpClient(self.config.login_url, self.settings, transport=self._transport)
        try:
            resp = await http.request(
                "POST",
                f"/{creds['tenant_id']}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": creds["client_id"],
                    "client_secret": creds["client_secret"],
                    "scope": f"{self.config.management_url}/.default",
                },
            )
            return resp.json()["access_token"]
        finally:
            await http.aclose()

    async def fetch(self, since: str | None = None) -> AsyncIterator[SourceRecord]:
        token = await self._token()
        http = RetryingHttpClient(
            self.config.management_url, self.settings, headers={"Authorization": f"Bearer {token}"}, transport=self._transport
        )
        try:
            skip: str | None = None
            while True:
                body: dict[str, Any] = {"subscriptions": self.config.subscriptions, "query": self.config.query, "options": {"$top": 1000}}
                if skip:
                    body["options"]["$skipToken"] = skip
                data = await http.post_json(
                    "/providers/Microsoft.ResourceGraph/resources", json=body, params={"api-version": "2022-10-01"}
                )
                for row in data.get("data", []):
                    yield _record(
                        CloudResourcePayload(
                            provider="azure",
                            resource_id=row["id"],
                            resource_type=str(row.get("type", "")).lower(),
                            name=row.get("name") or row["id"].rsplit("/", 1)[-1],
                            region=row.get("location"),
                            account=row.get("subscriptionId"),
                            tags=row.get("tags") or {},
                        ),
                        url=f"https://portal.azure.com/#resource{row['id']}",
                    )
                skip = data.get("$skipToken")
                if not skip:
                    break
        finally:
            await http.aclose()


# ----------------------------------------------------------------------------- GCP
class GcpConfig(BaseModel):
    scope: str = Field(description="projects/<id> | folders/<id> | organizations/<id>")
    asset_types: list[str] = Field(default_factory=list)
    api_url: str = "https://cloudasset.googleapis.com"
    token_url: str = "https://oauth2.googleapis.com/token"


@register_connector
class GcpConnector(Connector):
    """Secret: service-account key JSON (JWT bearer flow) or a raw OAuth access token."""

    type = "gcp"
    source_system = "gcp"
    config_model = GcpConfig
    config: GcpConfig

    async def _token(self) -> str:
        secret = (self.secret or "").strip()
        if not secret.startswith("{"):
            return secret
        sa = json.loads(secret)
        now = int(time.time())
        assertion = jwt.encode(
            {
                "iss": sa["client_email"],
                "scope": "https://www.googleapis.com/auth/cloud-platform.read-only",
                "aud": self.config.token_url,
                "iat": now,
                "exp": now + 3600,
            },
            sa["private_key"],
            algorithm="RS256",
        )
        http = RetryingHttpClient("", self.settings, transport=self._transport)
        try:
            resp = await http.request(
                "POST",
                self.config.token_url,
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
            )
            return resp.json()["access_token"]
        finally:
            await http.aclose()

    async def fetch(self, since: str | None = None) -> AsyncIterator[SourceRecord]:
        token = await self._token()
        http = RetryingHttpClient(
            self.config.api_url, self.settings, headers={"Authorization": f"Bearer {token}"}, transport=self._transport
        )
        try:
            page_token: str | None = None
            while True:
                params: dict[str, Any] = {"contentType": "RESOURCE", "pageSize": 500}
                if self.config.asset_types:
                    params["assetTypes"] = self.config.asset_types
                if page_token:
                    params["pageToken"] = page_token
                data = await http.get_json(f"/v1/{self.config.scope}/assets", params=params)
                for asset in data.get("assets", []):
                    resource = asset.get("resource") or {}
                    rdata = resource.get("data") or {}
                    name = rdata.get("name") or asset["name"].rsplit("/", 1)[-1]
                    yield _record(
                        CloudResourcePayload(
                            provider="gcp",
                            resource_id=asset["name"],
                            resource_type=asset.get("assetType", ""),
                            name=str(name).rsplit("/", 1)[-1],
                            region=resource.get("location"),
                            account=(resource.get("parent") or "").rsplit("/", 1)[-1] or None,
                            tags=rdata.get("labels") or (rdata.get("settings") or {}).get("userLabels") or {},
                        )
                    )
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
        finally:
            await http.aclose()
