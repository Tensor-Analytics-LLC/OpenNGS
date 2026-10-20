# Self-hosting OpenNGS

Run the README instructions or `docker compose up --build`. First signup creates the administrator. Keep local access bound to localhost. Back up the state directory together with the local Vault master; losing that file makes locally encrypted credentials unreadable. Never publish state, runtime environments, session cookies, database dumps, private inventories or uploaded genomic data.

Datasets enter through the cloud upload manager and are verified before use. `NGS_DATA_DIR` holds application-managed temporary files and result indexes, not user-selected reference directories.

Portable mode executes the implemented built-in operations. Native tools need an available Docker runtime and approved immutable images, or the explicit AWS Batch worker contract. Real input/output checks, scientific compatibility and third-party rights still apply. Arbitrary Nextflow/WDL/CWL programs are not universally translated.

The seven bundled sample sources are small and self-contained. The local runner maps their frozen manifest to the checkout and verifies file hashes. Rebuilding the slices is optional and requires upstream source data. The GIAB variant slice is not a complete truth set and cannot validate a caller by itself.

## AWS components

Review the generators and supply your own resources; these files do not automatically deploy anything:

- `infra/cache-stack.json`: private versioned SSE-KMS cache, retained on stack deletion.
- `infra/lean_stack.py`: private CloudFront VPC origin, WAF, API host and private single-AZ PostgreSQL.
- `infra/bootstrap.sh`: app installation and insert-only initial database migration.
- `infra/demo_compute.py`: trusted image build and private Fargate queue with a two-vCPU ceiling.
- `infra/demo.Dockerfile`, `infra/demo_worker.py`: reviewed sample execution image.
- `backend/demo_jobs.py`: durable dispatch and provider reconciliation; run exactly one scheduler alongside the API.

Cloud mode needs `NGS_DEPLOYMENT=cloud`, a PostgreSQL DSN with certificate verification, protected state, `NGS_BUCKET`, `NGS_KMS_KEY`, `NGS_ORIGIN_TOKEN` and the reviewed queue/job-definition/image identifiers. Use instance roles and protected runtime configuration. The API private origin requires the matching CloudFront-injected origin header. Direct public API-host exposure is unsupported.

The sample lane runs reviewed commands in isolated Fargate tasks with no task IAM role or internet route. Short-lived grants reach one run's manifest/output destination. Personal ingestion requires an explicit funded Research Pro grant and the reviewed tenant factory; arbitrary code remains unavailable on the public sample lane. Patient/clinical use requires the uncompleted validation and compliance work described in READINESS.md. One API host and a single-AZ database minimize cost; they do not provide high availability.

## Cost and recovery

The ledger reserves integer micro-dollar estimates before paid work; new work that cannot fit its allowance is refused or deferred without discarding accepted jobs. Fargate allocation is capped at two concurrent vCPUs and no paid scaling occurs automatically. Configure scoped AWS Budgets, billing reconciliation, retention and alerts independently. Billing delays, attacks and unexpected service charges mean the ledger cannot guarantee an absolute invoice cap.

A bounded 100-user interactive read test has passed; the scientific sample lane remains resource-limited. It does not promise unrestricted daily WGS, long-read or single-cell analysis within the $200 Explore/platform baseline. Research Pro expenditure requires a separate funded allowance; self-hosted operators select and fund their own capacity.

Preserve PostgreSQL and protected Vault state when replacing the host. The importer records its source digest and refuses a different initial database. Cloud API restarts preserve queued jobs. Unknown provider outcomes keep reservations and are reconciled before resubmission. Stop the scheduler during a restore; confirm provider jobs and reconcile quotas before reopening execution. Use one dispatcher service per lane. Research dispatch has leases and deterministic provider reconciliation; changing the orchestration topology requires fencing and recovery qualification.

## Funded cloud lanes and post-deployment configuration

`infra/research_stack.py` and `scripts/publish_research_capacity.py` publish an immutable, version-pinned tenant factory and narrowly scoped CloudFormation service role. `scripts/build_research_image.py` builds the reviewed core runtime. The API can deploy only the approved template URL; a funded grant is required before provisioning. `scripts/build_vm_image.py`, `scripts/finalize_vm_image.py` and `scripts/publish_vm_capacity.py` prepare and publish the private exclusive-machine lane. Review current prices, capacities and IAM before publication. These operator scripts expect private deployment records under `evidence/deployment/`; those records are not distributed with public source.

`infra/budget_stack.py` supplies budget alerts; `scripts/publish_network_guard.py` reconciles only the OpenNGS endpoint/DNS controls. `scripts/publish_audit.py` creates the retained governance archive. After a verified source release, `scripts/configure_services.py` reconciles protected service variables and the CloudFront managed-origin ingress, preserving existing secrets and data. Inspect returned SSM status and the HTTPS service. See READINESS.md for remaining personal-data workbench and assay qualification limits.
