# Ontology Schema

The ontology is defined as data in
[`src/ontology_platform/ontology/schema.py`](src/ontology_platform/ontology/schema.py). This
document describes it. The live definition is served by `GET /api/v1/ontology/schema` and by the
MCP tool `get_ontology_schema`.

## Entity types

| Type | Identity (canonical key fields) | Customer-scoped | Typical sources |
|---|---|---|---|
| organization | name | no | catalog |
| customer | name | **yes** (itself) | catalog, k8s labels, cloud tags |
| product | name | no | catalog, Backstage `Domain` |
| application | name | no | Backstage `System`, k8s `part-of` label |
| repository | provider, path | no | GitHub / GitLab / Bitbucket |
| service | name | no | Backstage `Component`, k8s `app.kubernetes.io/name` |
| api | name | no | Backstage `API`, OpenAPI spec |
| database | name | no | Backstage `Resource(type=database)`, cloud `database` tag |
| cloud_resource | provider, resource_id | if tagged with a customer | AWS / Azure / GCP inventories |
| kubernetes_cluster | name | no | k8s connector, EKS/AKS/GKE resources |
| deployment | application, customer, tier | **yes** | k8s workloads, cloud tags |
| environment | customer, tier | **yes** | k8s namespace/labels, cloud tags |
| documentation | source, external_id | no | Confluence, READMEs |

Canonical key format: `<type>:<normalized identity parts joined by "/">`, for example
`deployment:payments-platform/acme/prod` or `repository:github/northwind/payment-processor`.

Normalization:
* Names are lowercased; non-alphanumeric characters become `-`.
* Token synonyms are applied: `svc→service`, `db→database`, `k8s→kubernetes`.
* Environment tiers are mapped to `dev | sit | uat | staging | prod`, so `production`, `prd` and
  `live` all become `prod`.

**Distinguishing deployments of the same application across customers.** The application node is
shared, but every (application, customer, tier) has its own `deployment` node, and every
(customer, tier) has its own `environment` node. `payments-platform` for Acme PROD and for Globex
PROD are therefore separate nodes with their own clusters, namespaces and cloud resources.

## Relationship types (domain → range)

| Relationship | Allowed source → target |
|---|---|
| owns | customer, organization → product |
| has_customer | organization → customer |
| contains | product → application; application → service, database |
| has | application, service → repository |
| exposes | application, service → api |
| depends_on | application, service → service, application, database, api, cloud_resource |
| deployed_in | application, deployment, service → environment |
| belongs_to | environment → customer; cloud_resource, kubernetes_cluster → environment, customer; repository, customer → organization |
| runs_on | application, deployment, service → kubernetes_cluster, cloud_resource |
| uses | deployment → cloud_resource, database |
| describes | documentation → application, repository, service, api, product, database, kubernetes_cluster, customer |
| deploys | deployment → application, service |
| implements | repository → service, api, application |
| hosted_on | database, kubernetes_cluster → cloud_resource |

The pipeline rejects candidate relationships that break these rules and counts them as
`relationships_rejected`, with a warning on the ingestion run.

```
Organization ─has_customer─▶ Customer ─owns─▶ Product ─contains─▶ Application ─has─▶ Repository
                                ▲                                   │  │  │            │implements
                       belongs_to                          contains │  │  └exposes─▶ API ◀───┘
                                │                                   ▼  │depends_on
                           Environment ◀─deployed_in─ Deployment ─deploys─▶ Application/Service
                                                         │ runs_on        │uses
                                                         ▼                ▼
                                              KubernetesCluster ─hosted_on─▶ CloudResource ◀─hosted_on─ Database
Documentation ─describes─▶ (application | repository | service | api | product | …)
```

## Fact metadata

Every entity and relationship carries:

* **status**:
  * `active`
  * `stub`: referenced by some source but not yet defined by its own source
  * `stale`: no source observes it any more. Stale facts are never hard-deleted.
* **version** plus an append-only `graph_changes` history (`created`, `updated`, `stale`,
  `reactivated`) with a full snapshot for each version.
* **provenance**: one row per (fact, connector, source record, extractor), holding:
  * source system: the fixture adapter shows as e.g. `github (fixture)`
  * source URL
  * extractor: `structured:<kind>`, `reference` or `text_mention`
  * confidence
  * evidence snippet
  * first/last observed time and run
* **confidence** on relationships. Structured sources give 1.0. Mentions found in a Confluence
  label give 0.9, in a title 0.85, and in body text 0.6.
* **aliases**: every name observed for the entity. Used by the resolver and by question entity
  linking.

## Extraction conventions (configurable in `extraction/conventions.py`)

| Source | Convention |
|---|---|
| Repos | Backstage `catalog-info.yaml`. Kinds map as Domain→product, System→application, Component→service, API→api, Resource(database)→database. `dependsOn`, `providesApis` and `consumesApis` give dependency and exposure edges. `openapi.yaml/json` gives API endpoints; `README.md` gives a documentation node. |
| Kubernetes | Labels: `app.kubernetes.io/name` gives the service and `app.kubernetes.io/part-of` the application. Customer and environment come from `ontology.io/customer` and `ontology.io/environment`, or else from namespace `<customer>-<tier>`. Annotations: `ontology.io/uses` (cloud resource ids), `ontology.io/depends-on`. |
| Cloud | Tags `customer`, `environment`, `application`, `database`, `cluster`. EKS/AKS/GKE resource types become cluster nodes; RDS/Azure SQL/Cloud SQL/etc. become database nodes. |
| Confluence | Labels `app-<name>`, `svc-<name>`, `api-<name>`, `product-<name>`, `customer-<name>`, `db-<name>`, `cluster-<name>` give `describes` edges. Body text mentions of known entities give lower-confidence `describes` edges. |
