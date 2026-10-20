# Self-hosting OpenNGS

Run the README instructions or `docker compose up --build`. First signup creates the administrator. Keep local access bound to localhost. Back up the state directory together with the local Vault master; losing that file makes locally encrypted credentials unreadable. Never publish state, runtime environments, session cookies, database dumps, private inventories or uploaded genomic data.

Set `NGS_LOCAL_ROOT` to a specific directory you control and `NGS_DATA_DIR` to an artifact directory, then register folders beneath that root in Settings. The Docker edition exposes its `/data` volume. Mount a specific research directory if necessary; do not mount the entire filesystem or Docker socket into a public API container.

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

The sample lane runs reviewed commands in isolated Fargate tasks with no task IAM role or internet route. Short-lived grants reach one run's manifest/output destination. Do not enable arbitrary code or patient uploads on this demonstration deployment. One API host and a single-AZ database minimize cost; they do not provide high availability.

## Cost and recovery

The ledger reserves integer micro-dollar estimates before paid work; valid sample jobs can wait durably for budget capacity. Fargate allocation is capped at two concurrent vCPUs and no paid scaling occurs automatically. Configure scoped AWS Budgets, billing reconciliation, retention and alerts independently. Billing delays, attacks and unexpected service charges mean the ledger cannot guarantee an absolute invoice cap.

The hosted service is scoped to four to five users using bounded sample slices. It does not promise unrestricted daily WGS, long-read or single-cell analysis within the $200 Explore/platform baseline. Research Pro expenditure requires a separate funded allowance; self-hosted operators select and fund their own capacity.

Preserve PostgreSQL and protected Vault state when replacing the host. The importer records its source digest and refuses a different initial database. Cloud API restarts preserve queued jobs. Unknown provider outcomes keep reservations and are reconciled before resubmission. Stop the scheduler during a restore; confirm provider jobs and reconcile quotas before reopening execution. Multiple schedulers require explicit leader fencing and are not supported by this release.
