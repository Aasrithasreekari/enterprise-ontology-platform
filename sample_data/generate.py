"""Generates the sample enterprise dataset (fixture records + catalog) under sample_data/.

The output files are committed; re-run only after editing this script:

    python sample_data/generate.py

Fictional enterprise "Northwind Technologies" builds two products (PayHub, ClaimsPro) for three
customers (Acme Bank, Globex Insurance, Initech Retail), with code on GitHub/GitLab/Bitbucket,
docs in Confluence, workloads on EKS/AKS/GKE and infrastructure in AWS/Azure/GCP.
A second tenant "contoso" exists to exercise tenant isolation.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
TS = "2026-11-01T10:00:00+00:00"


def catalog_info(*docs: dict) -> str:
    return yaml.safe_dump_all(list(docs), sort_keys=False)


def system(name, domain, desc, owner="team-payments"):
    return {"apiVersion": "backstage.io/v1alpha1", "kind": "System", "metadata": {"name": name, "description": desc}, "spec": {"owner": owner, "domain": domain}}


def component(name, system_name, desc, depends=(), provides=(), consumes=(), ctype="service", owner="team"):
    spec = {"type": ctype, "lifecycle": "production", "owner": owner, "system": system_name}
    if depends:
        spec["dependsOn"] = list(depends)
    if provides:
        spec["providesApis"] = list(provides)
    if consumes:
        spec["consumesApis"] = list(consumes)
    return {"apiVersion": "backstage.io/v1alpha1", "kind": "Component", "metadata": {"name": name, "description": desc}, "spec": spec}


def api(name, system_name, desc):
    return {"apiVersion": "backstage.io/v1alpha1", "kind": "API", "metadata": {"name": name, "description": desc}, "spec": {"type": "openapi", "system": system_name, "owner": "team", "lifecycle": "production"}}


def resource(name, system_name, desc):
    return {"apiVersion": "backstage.io/v1alpha1", "kind": "Resource", "metadata": {"name": name, "description": desc}, "spec": {"type": "database", "system": system_name, "owner": "team"}}


def openapi(title, version, paths, api_name=None):
    info = {"title": title, "version": version, "description": f"{title} specification"}
    if api_name:
        info["x-api-name"] = api_name
    return yaml.safe_dump({"openapi": "3.0.3", "info": info, "servers": [{"url": f"https://api.northwind.example/{title.lower().split()[0]}"}], "paths": {p: {m: {"summary": f"{m} {p}"} for m in ms} for p, ms in paths.items()}}, sort_keys=False)


def repo_record(provider, path, desc, files, topics=()):
    name = path.rsplit("/", 1)[-1]
    url = {"github": f"https://github.com/{path}", "gitlab": f"https://gitlab.com/{path}", "bitbucket": f"https://bitbucket.org/{path}"}[provider]
    return {
        "kind": "repository",
        "external_id": f"{provider}:{path}",
        "source_system": provider,
        "source_url": url,
        "updated_at": TS,
        "payload": {"provider": provider, "path": path, "name": name, "url": url, "description": desc, "topics": list(topics), "default_branch": "main", "files": files},
    }


# ----------------------------------------------------------------------------- repositories
REPOS = {
    "github": [
        repo_record(
            "github", "northwind/payments-gateway", "Edge gateway for PayHub card and wallet payments",
            {
                "catalog-info.yaml": catalog_info(
                    system("payments-platform", "payhub", "PayHub payments platform"),
                    component("payment-gateway", "payments-platform", "Public payment gateway", depends=["component:payment-processor", "component:auth-service"], provides=["payments-api"], consumes=["auth-api"]),
                    api("payments-api", "payments-platform", "Public REST API for creating and querying payments"),
                ),
                "openapi.yaml": openapi("Payments API", "2.3.0", {"/v2/payments": ["get", "post"], "/v2/payments/{id}": ["get"], "/v2/refunds": ["post"]}, api_name="payments-api"),
                "README.md": "# payments-gateway\n\nThe payment-gateway service is the public entry point of PayHub. It authenticates merchants via auth-service and forwards authorised payments to payment-processor.\n\n## Operations\nSee the Payments Runbook in Confluence. Alerts page the payments on-call.",
            },
            topics=["payhub", "payments"],
        ),
        repo_record(
            "github", "northwind/payment-processor", "Payment authorisation and settlement engine",
            {
                "catalog-info.yaml": catalog_info(
                    component("payment-processor", "payments-platform", "Authorises and settles payments", depends=["component:ledger-service", "component:fraud-detector", "resource:payments-db"], provides=["webhooks-api"]),
                    resource("payments-db", "payments-platform", "PostgreSQL store for payment state"),
                    api("webhooks-api", "payments-platform", "Outbound merchant webhooks"),
                ),
                "README.md": "# payment-processor\n\nSettles payments, posts journal entries to ledger-service and scores risk with fraud-detector. Persists state in payments-db.",
            },
        ),
        repo_record(
            "github", "northwind/fraud-detector", "Real-time fraud scoring",
            {"catalog-info.yaml": catalog_info(component("fraud-detector", "payments-platform", "ML fraud scoring service")), "README.md": "# fraud-detector\n\nScores transactions for fraud risk. Called synchronously by payment-processor."},
        ),
        repo_record(
            "github", "northwind/identity-platform", "Shared identity and access platform",
            {
                "catalog-info.yaml": catalog_info(
                    system("identity-platform", "core-platform", "Shared authentication for all products", owner="team-identity"),
                    component("auth-service", "identity-platform", "OAuth2/OIDC token service", provides=["auth-api"]),
                    api("auth-api", "identity-platform", "OAuth2 token and introspection API"),
                ),
                "openapi.yaml": openapi("Auth API", "1.4.0", {"/oauth2/token": ["post"], "/oauth2/introspect": ["post"], "/.well-known/jwks.json": ["get"]}, api_name="auth-api"),
                "README.md": "# identity-platform\n\nHosts auth-service, the OAuth2 provider used by payment-gateway and claims-service.",
            },
        ),
        repo_record(
            "github", "northwind/notification-service", "Email/SMS notification delivery",
            {"catalog-info.yaml": catalog_info(component("notification-service", "identity-platform", "Sends email and SMS notifications")), "README.md": "# notification-service\n\nDelivers transactional email and SMS for claims-service and intake-service."},
        ),
    ],
    "gitlab": [
        repo_record(
            "gitlab", "northwind/ledger/ledger-service", "Double-entry ledger for PayHub",
            {
                "catalog-info.yaml": catalog_info(
                    system("ledger", "payhub", "Double-entry accounting ledger", owner="team-ledger"),
                    component("ledger-service", "ledger", "Ledger journal API", depends=["resource:ledger-db"], provides=["ledger-api"]),
                    resource("ledger-db", "ledger", "Ledger PostgreSQL database"),
                    api("ledger-api", "ledger", "Journal posting and balance API"),
                ),
                "README.md": "# ledger-service\n\nImmutable double-entry ledger. payment-processor posts journal entries here.",
            },
        ),
    ],
    "bitbucket": [
        repo_record(
            "bitbucket", "northwind/claims-portal", "ClaimsPro customer claims portal (monorepo)",
            {
                "catalog-info.yaml": catalog_info(
                    system("claims-portal", "claimspro", "Customer-facing insurance claims portal", owner="team-claims"),
                    component("claims-web", "claims-portal", "Claims web frontend", depends=["component:claims-service"], ctype="website"),
                    component("claims-service", "claims-portal", "Claims lifecycle backend", depends=["component:auth-service", "component:notification-service", "component:intake-service", "resource:claims-db"], provides=["claims-api"]),
                    api("claims-api", "claims-portal", "Claims submission and status API"),
                    resource("claims-db", "claims-portal", "Claims relational database"),
                ),
                "openapi.yaml": openapi("Claims API", "1.9.2", {"/claims": ["get", "post"], "/claims/{id}": ["get", "patch"], "/claims/{id}/documents": ["post"]}, api_name="claims-api"),
                "README.md": "# claims-portal\n\nMonorepo containing claims-web and claims-service. Uploaded documents are forwarded to intake-service in document-intake.",
            },
        ),
        repo_record(
            "bitbucket", "northwind/document-intake", "Document ingestion and OCR",
            {
                "catalog-info.yaml": catalog_info(
                    system("document-intake", "claimspro", "Document ingestion and OCR pipeline", owner="team-claims"),
                    component("intake-service", "document-intake", "Receives and classifies documents", depends=["component:ocr-worker", "component:notification-service"]),
                    component("ocr-worker", "document-intake", "OCR batch worker"),
                ),
                "README.md": "# document-intake\n\nintake-service stores uploads in object storage and queues OCR jobs for ocr-worker.",
            },
        ),
    ],
}

# ----------------------------------------------------------------------------- confluence
def page(pid, title, labels, body, space="ENG"):
    url = f"https://northwind.atlassian.net/wiki/spaces/{space}/pages/{pid}"
    return {
        "kind": "document", "external_id": f"confluence:{pid}", "source_system": "confluence", "source_url": url, "updated_at": TS,
        "payload": {"source": "confluence", "external_id": str(pid), "title": title, "url": url, "space": space, "labels": labels, "body": body},
    }


PAGES = [
    page(1001, "PayHub Architecture Overview", ["product-payhub", "app-payments-platform"],
         "PayHub is composed of the payments-platform and ledger applications.\n\nRequests enter through payment-gateway, which validates tokens with auth-service. payment-processor authorises payments, calls fraud-detector for risk scoring and posts journal entries to ledger-service.\n\nState is stored in payments-db and ledger-db (PostgreSQL on RDS)."),
    page(1002, "Payments Runbook", ["app-payments-platform", "svc-payment-processor"],
         "On-call runbook for payment incidents.\n\nIf payment-processor latency exceeds 2s, check fraud-detector health first.\n\nFailover of payments-db: promote the read replica in the secondary region and update the connection secret.\n\nEscalate to the ledger team if ledger-service rejects postings."),
    page(1003, "Claims Portal Release Notes 1.9", ["app-claims-portal"],
         "Release 1.9 of claims-portal adds document upload. claims-web now calls the new claims-api endpoint /claims/{id}/documents, which forwards files to intake-service.\n\nGlobex and Initech UAT sign-off completed."),
    page(1004, "Auth Service Integration Guide", ["svc-auth-service", "api-auth-api"],
         "How to integrate with auth-service.\n\nObtain tokens from the auth-api /oauth2/token endpoint using client credentials. Services such as payment-gateway and claims-service validate tokens via introspection."),
    page(1005, "Acme Bank - Production Environment", ["customer-acme"],
         "Acme Bank production runs PayHub on the nw-prod-eks-use1 cluster in namespace acme-prod.\n\nDatabase: payments-db-acme-prod (RDS). Change windows: Sunday 02:00-04:00 UTC.", space="CUST"),
    page(1006, "Document Intake OCR Tuning", ["app-document-intake"],
         "ocr-worker throughput tuning guide. Scale ocr-worker horizontally; intake-service back-pressure is controlled via queue depth."),
]

# ----------------------------------------------------------------------------- kubernetes
def wl(cluster, provider, ns, name, labels, annotations=None, image=None, replicas=2):
    return {
        "kind": "k8s_workload", "external_id": f"k8s:{cluster}/{ns}/Deployment/{name}", "source_system": "kubernetes", "updated_at": TS,
        "payload": {"cluster": cluster, "cluster_provider": provider, "namespace": ns, "name": name, "kind": "Deployment",
                    "labels": labels, "annotations": annotations or {}, "images": [image or f"registry.northwind.example/{name}:1.0"], "replicas": replicas},
    }


def app_labels(service, app):
    return {"app.kubernetes.io/name": service, "app.kubernetes.io/part-of": app}


PROD_EKS, NONPROD_EKS, PROD_AKS, NONPROD_GKE = "nw-prod-eks-use1", "nw-nonprod-eks-use1", "nw-prod-aks-weu", "nw-nonprod-gke-euw1"
ACME_PAYDB = "arn:aws:rds:us-east-1:111122223333:db:payments-db-acme-prod"
GLOBEX_PAYDB = "arn:aws:rds:us-east-1:111122223333:db:payments-db-globex-prod"
GLOBEX_CLAIMSDB = "/subscriptions/0000-1111/resourceGroups/rg-globex-prod/providers/Microsoft.Sql/servers/sql-globex-prod/databases/claims-db"
GLOBEX_BUCKET = "arn:aws:s3:::nw-globex-prod-intake-docs"

WORKLOADS = []
for customer, tier, cluster, prov in [("acme", "sit", NONPROD_EKS, "eks"), ("acme", "uat", NONPROD_EKS, "eks"), ("acme", "prod", PROD_EKS, "eks"), ("globex", "uat", NONPROD_EKS, "eks"), ("globex", "prod", PROD_EKS, "eks")]:
    ns = f"{customer}-{tier}"
    uses = {"ontology.io/uses": ACME_PAYDB} if (customer, tier) == ("acme", "prod") else ({"ontology.io/uses": GLOBEX_PAYDB} if (customer, tier) == ("globex", "prod") else {})
    for svc in ("payment-gateway", "payment-processor", "fraud-detector"):
        WORKLOADS.append(wl(cluster, prov, ns, svc, app_labels(svc, "payments-platform"), uses if svc == "payment-processor" else None))
    if tier == "prod":
        WORKLOADS.append(wl(cluster, prov, ns, "ledger-service", app_labels("ledger-service", "ledger")))
for customer, tier, cluster, prov in [("globex", "uat", NONPROD_EKS, "eks"), ("globex", "prod", PROD_AKS, "aks"), ("initech", "uat", NONPROD_GKE, "gke"), ("initech", "prod", PROD_AKS, "aks")]:
    ns = f"{customer}-{tier}"
    for svc in ("claims-web", "claims-service"):
        ann = {"ontology.io/uses": GLOBEX_CLAIMSDB} if (customer, tier, svc) == ("globex", "prod", "claims-service") else None
        WORKLOADS.append(wl(cluster, prov, ns, svc, app_labels(svc, "claims-portal"), ann))
    if tier == "prod":
        for svc in ("intake-service", "ocr-worker"):
            ann = {"ontology.io/uses": GLOBEX_BUCKET} if (customer, svc) == ("globex", "intake-service") else None
            WORKLOADS.append(wl(cluster, prov, ns, svc, app_labels(svc, "document-intake"), ann))
for cluster, prov in [(PROD_EKS, "eks"), (NONPROD_EKS, "eks"), (PROD_AKS, "aks"), (NONPROD_GKE, "gke")]:
    for svc in ("auth-service", "notification-service"):
        WORKLOADS.append(wl(cluster, prov, "platform", svc, app_labels(svc, "identity-platform"), replicas=3))

# ----------------------------------------------------------------------------- cloud
def cloud(provider, rid, rtype, name, region, account, tags):
    return {
        "kind": "cloud_resource", "external_id": f"{provider}:{rid}", "source_system": provider, "updated_at": TS,
        "payload": {"provider": provider, "resource_id": rid, "resource_type": rtype, "name": name, "region": region, "account": account, "tags": tags},
    }


AWS = [
    cloud("aws", f"arn:aws:eks:us-east-1:111122223333:cluster/{PROD_EKS}", "aws:eks:cluster", PROD_EKS, "us-east-1", "111122223333", {"cluster": PROD_EKS, "environment": "prod"}),
    cloud("aws", f"arn:aws:eks:us-east-1:444455556666:cluster/{NONPROD_EKS}", "aws:eks:cluster", NONPROD_EKS, "us-east-1", "444455556666", {"cluster": NONPROD_EKS}),
    cloud("aws", ACME_PAYDB, "aws:rds:db", "payments-db-acme-prod", "us-east-1", "111122223333", {"customer": "acme", "environment": "production", "application": "payments-platform", "database": "payments-db"}),
    cloud("aws", GLOBEX_PAYDB, "aws:rds:db", "payments-db-globex-prod", "us-east-1", "111122223333", {"customer": "globex", "environment": "prod", "application": "payments-platform", "database": "payments-db"}),
    cloud("aws", "arn:aws:rds:us-east-1:111122223333:db:ledger-db-prod", "aws:rds:db", "ledger-db-prod", "us-east-1", "111122223333", {"database": "ledger-db", "environment": "prod"}),
    cloud("aws", GLOBEX_BUCKET, "aws:s3:bucket", "nw-globex-prod-intake-docs", "us-east-1", "111122223333", {"customer": "globex", "environment": "prod", "application": "document-intake"}),
]
AZURE = [
    cloud("azure", f"/subscriptions/0000-1111/resourceGroups/rg-platform/providers/Microsoft.ContainerService/managedClusters/{PROD_AKS}", "microsoft.containerservice/managedclusters", PROD_AKS, "westeurope", "0000-1111", {"cluster": PROD_AKS}),
    cloud("azure", GLOBEX_CLAIMSDB, "microsoft.sql/servers/databases", "claims-db", "westeurope", "0000-1111", {"customer": "globex", "environment": "prod", "application": "claims-portal", "database": "claims-db"}),
]
GCP = [
    cloud("gcp", f"//container.googleapis.com/projects/nw-nonprod/locations/europe-west1/clusters/{NONPROD_GKE}", "container.googleapis.com/Cluster", NONPROD_GKE, "europe-west1", "nw-nonprod", {"cluster": NONPROD_GKE}),
    cloud("gcp", "//cloudsql.googleapis.com/projects/nw-nonprod/instances/claims-db-initech-uat", "sqladmin.googleapis.com/Instance", "claims-db-initech-uat", "europe-west1", "nw-nonprod", {"customer": "initech", "environment": "uat", "application": "claims-portal", "database": "claims-db"}),
]

# ----------------------------------------------------------------------------- catalog (CMDB/CRM export)
CATALOG = {
    "entities": [
        {"type": "organization", "name": "northwind", "display_name": "Northwind Technologies", "aliases": ["Northwind"]},
        {"type": "customer", "name": "acme", "display_name": "Acme Bank", "aliases": ["Acme Bank", "Customer A"], "properties": {"segment": "banking", "region": "us"}},
        {"type": "customer", "name": "globex", "display_name": "Globex Insurance", "aliases": ["Globex Insurance", "Customer B"], "properties": {"segment": "insurance", "region": "eu"}},
        {"type": "customer", "name": "initech", "display_name": "Initech Retail", "aliases": ["Initech Retail", "Customer C"], "properties": {"segment": "retail", "region": "eu"}},
        {"type": "product", "name": "payhub", "display_name": "PayHub", "description": "Card and wallet payments product", "aliases": ["PayHub", "Product X"]},
        {"type": "product", "name": "claimspro", "display_name": "ClaimsPro", "description": "Insurance claims management product", "aliases": ["ClaimsPro", "Product Y"]},
        {"type": "product", "name": "core-platform", "display_name": "Core Platform", "description": "Shared platform services"},
        # Business names used by account teams for technical components (aliases only; no new entities).
        {"type": "service", "name": "payment-processor", "aliases": ["Payment Service", "Payments Service"]},
        {"type": "application", "name": "payments-platform", "aliases": ["Application Y", "Payments Platform"]},
    ],
    "relationships": [
        {"type": "has_customer", "source": {"type": "organization", "name": "northwind"}, "target": {"type": "customer", "name": n}} for n in ("acme", "globex", "initech")
    ] + [
        {"type": "owns", "source": {"type": "customer", "name": c}, "target": {"type": "product", "name": p}}
        for c, p in [("acme", "payhub"), ("globex", "payhub"), ("globex", "claimspro"), ("initech", "claimspro")]
    ] + [
        {"type": "owns", "source": {"type": "organization", "name": "northwind"}, "target": {"type": "product", "name": "core-platform"}},
    ],
}

# ----------------------------------------------------------------------------- second tenant
CONTOSO = {
    "records": [
        repo_record("github", "contoso/payments-platform", "Contoso internal payments",
                    {"catalog-info.yaml": catalog_info(system("payments-platform", "contoso-pay", "Contoso payments"), component("payment-gateway", "payments-platform", "Contoso gateway"))}),
        wl("contoso-aks", "aks", "fabrikam-prod", "payment-gateway", app_labels("payment-gateway", "payments-platform")),
    ]
}


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# GENERATED by sample_data/generate.py - local test adapter data, not real systems\n" + yaml.safe_dump(data, sort_keys=False, width=140), encoding="utf-8")


def main() -> None:
    base = ROOT / "northwind"
    for provider, repos in REPOS.items():
        write(base / "fixtures" / f"{provider}.yaml", {"records": repos})
    write(base / "fixtures" / "confluence.yaml", {"records": PAGES})
    write(base / "fixtures" / "kubernetes.yaml", {"records": WORKLOADS})
    write(base / "fixtures" / "aws.yaml", {"records": AWS})
    write(base / "fixtures" / "azure.yaml", {"records": AZURE})
    write(base / "fixtures" / "gcp.yaml", {"records": GCP})
    write(base / "catalog" / "crm-export.yaml", CATALOG)
    write(ROOT / "contoso" / "fixtures" / "all.yaml", CONTOSO)
    counts = {p: len(r) for p, r in REPOS.items()} | {"confluence": len(PAGES), "kubernetes": len(WORKLOADS), "aws": len(AWS), "azure": len(AZURE), "gcp": len(GCP)}
    print(json.dumps(counts))


if __name__ == "__main__":
    main()
