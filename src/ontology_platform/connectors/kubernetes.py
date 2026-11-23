"""Kubernetes connector: lists Deployments and StatefulSets through the API server."""

from __future__ import annotations

from collections.abc import AsyncIterator

from pydantic import BaseModel, Field

from ontology_platform.connectors.base import Connector, K8sWorkloadPayload, RecordKind, SourceRecord, register_connector
from ontology_platform.connectors.http import RetryingHttpClient


class KubernetesConfig(BaseModel):
    cluster_name: str = Field(min_length=1)
    api_server: str = Field(description="https://<api-server>")
    cluster_provider: str | None = Field(default=None, description="eks | aks | gke | on-prem ...")
    namespaces: list[str] = Field(default_factory=list, description="Empty → all namespaces")
    ca_cert_path: str | None = None
    workload_kinds: list[str] = Field(default_factory=lambda: ["deployments", "statefulsets"])


@register_connector
class KubernetesConnector(Connector):
    """Secret: a service-account bearer token with list access to apps/v1 workloads."""

    type = "kubernetes"
    source_system = "kubernetes"
    config_model = KubernetesConfig
    config: KubernetesConfig

    async def fetch(self, since: str | None = None) -> AsyncIterator[SourceRecord]:
        http = RetryingHttpClient(
            self.config.api_server,
            self.settings,
            headers={"Authorization": f"Bearer {self.secret}"},
            transport=self._transport,
            verify=self.config.ca_cert_path or True,
        )
        try:
            scopes = self.config.namespaces or [None]
            for ns in scopes:
                for kind in self.config.workload_kinds:
                    path = f"/apis/apps/v1/namespaces/{ns}/{kind}" if ns else f"/apis/apps/v1/{kind}"
                    cont: str | None = ""
                    while cont is not None:
                        params = {"limit": 500}
                        if cont:
                            params["continue"] = cont
                        data = await http.get_json(path, params=params)
                        for item in data.get("items", []):
                            yield self._to_record(item, kind)
                        cont = (data.get("metadata") or {}).get("continue") or None
        finally:
            await http.aclose()

    def _to_record(self, item: dict, kind: str) -> SourceRecord:
        meta = item.get("metadata") or {}
        spec = item.get("spec") or {}
        containers = ((spec.get("template") or {}).get("spec") or {}).get("containers") or []
        payload = K8sWorkloadPayload(
            cluster=self.config.cluster_name,
            cluster_provider=self.config.cluster_provider,
            namespace=meta.get("namespace", "default"),
            name=meta["name"],
            kind={"deployments": "Deployment", "statefulsets": "StatefulSet"}.get(kind, kind),
            labels=meta.get("labels") or {},
            annotations={k: v for k, v in (meta.get("annotations") or {}).items() if not k.startswith("kubectl.")},
            images=[c.get("image", "") for c in containers if c.get("image")],
            replicas=spec.get("replicas"),
        )
        ext = f"{self.config.cluster_name}/{payload.namespace}/{payload.kind}/{payload.name}"
        return SourceRecord(
            kind=RecordKind.K8S_WORKLOAD,
            external_id=f"k8s:{ext}",
            source_system="kubernetes",
            source_url=f"{self.config.api_server}{meta.get('selfLink', '')}" if meta.get("selfLink") else None,
            payload=payload.model_dump(),
        )
