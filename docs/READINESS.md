# OpenNGS implementation and scientific readiness

20 October 2026. Public service: https://d2j3o1re9v00qo.cloudfront.net/. Source: https://github.com/Tensor-Analytics-LLC/OpenNGS. Existing accounts, workflows, Vault material, audit records and datasets were retained. Independent Spatial and Grepeye resources were not changed.

## What is implemented and deployed

- Explore authorization uses a server-side allowlist of curated source IDs, immutable sample hashes, tools, parameters, resource ceilings, queue/concurrency limits and daily/monthly usage. Arbitrary URLs, local paths, private asset uploads and custom execution cannot bypass it.
- Research Pro requires an explicit funded grant. Per-tenant storage/upload/CPU/RAM/scratch/GPU/runtime/concurrency/queue/export limits are persisted with an audited grant version. Unsupported GPU or other runtime profiles remain blocked despite a larger numeric allowance.
- Admission reserves worst-case monetary, CPU and output/storage exposure transactionally. Integer micro-dollar accounting, daily/monthly holds, retained-capacity reservations, actual/forecast alerts and non-destructive admission holds are active.
- Resumable S3 multipart ingestion, length-bound signed parts, full-content SHA-256 verification in AWS, paired-read/index/dictionary bindings, tenant-scoped immutable run input snapshots and output commit manifests are implemented.
- The API and scientific execution are separate. Private Fargate sample execution has no task role or internet route. Funded tenant queues use reviewed digest-pinned runtimes and tenant-scoped IAM. Fresh exclusive VMs provide larger scratch, with no public IP/default internet route, encrypted volumes, bounded runtime and termination/recovery tokens.
- PostgreSQL preserves durable jobs, leases, submission intents, stages and upstream results. Ambiguous provider outcomes retain reservations for reconciliation. Completed stage outputs are validated before reuse; cache keys include scientific parameters and companion identities.
- An independent one-minute AWS schedule terminates expired exclusive workers even if the API/controller is unavailable. Its project/VPC/tenant/run checks and scoped termination role have been tested with an actual disposable worker. It has no data or secret access.
- The existing Spatial-derived canvas, inspectors, compatible connectors, preflight, execution/log views, sample results and research reporting remain integrated. User and admin entitlement/budget controls are available.
- The trusted API has bounded admission and a database pool. SSE streams have separate bounded slots, so execution monitoring does not consume ordinary request capacity.
- HTTPS uses CloudFront/WAF with a private origin. Storage is private/versioned/SSE-KMS, PostgreSQL uses certificate verification, and credentials retain authenticated envelope encryption. This is not a zero-knowledge vault: authorized application execution can decrypt a bound credential.
- OpenNGS-only DNS allowlisting and S3 endpoint restrictions are deployed. Scientific compute cannot use arbitrary internet destinations. The audit chain is checkpointed separately into a retained encrypted Object Lock bucket in 30-day Governance mode; application/worker roles have no delete or governance-bypass permission. A scoped CloudTrail trail delivers OpenNGS S3 object-access events into that archive with log validation enabled. AWS administrators remain privileged.

The [capability matrix](capability-matrix.json) enumerates every current catalogue entry with implementation, licence, contracts, resources, runtime publication and evidence scope. The [dependency report](dependency-readiness.json) separates Ready components from unavailable complete bundles. Catalogue presence and a successful container build are never equivalent to scientific support.

## Tier policy

| Control | Explore | Research Pro |
|---|---|---|
| Input | Immutable curated platform samples | Verified private datasets and authorized dependencies |
| Upload | Disabled by API | Explicit maximum per dataset; resumable multipart |
| Node ceiling | 2 vCPU, 4 GiB RAM, 20 GiB declared scratch, 120 seconds | Administrator-assigned finite limits, within published worker profiles |
| CPU allowance | 1 vCPU-hour/day, 8/month | Explicit daily/monthly grant |
| Concurrency / queue | 1 active, 20 queued per user; bounded global queue | Finite assigned concurrency/queue; dispatcher currently two remote stages globally |
| Stored outputs | 256 MiB per user; 64 MiB accepted output ceiling per run | Assigned stored-byte/output reservation |
| Exports | 1 GiB/month | Assigned export allowance |
| Money | Shared $200 all-in baseline, conservative per-job ceiling | Additional funded monthly allowance; no unlimited scaling |
| Retention | Assigned 7-day policy; automatic output expiry is not enabled | Assigned retention; no automatic deletion of existing private data |

Storage admission currently prevents growth when retained capacity is full. Automatic data/output retention cleanup is unfinished; the interface discloses this. It is not represented as implemented expiry. Unfunded or incompatible jobs receive a clear refusal/defer reason without silently reducing scientific inputs.

## Scientific workflows

| Blueprint | Implemented path | Current status / precise missing evidence |
|---|---|---|
| Full-input WGS germline | QC → BWA-MEM2 → coordinate sort → duplicate marking → BQSR/ApplyBQSR → indexing → HaplotypeCaller → allele-compatible annotation → statistics/research exports | All native stages passed on 1,000 synthetic pairs and a 20-kb engineering reference. An export-routing failure was fixed and all 11 computational stages were reused from verified cache to complete the recovered run. Full-scale accuracy, matching complete human reference/known-sites/annotation bundles and assay ploidy/interval policy remain unqualified. The BQSR template binds one known-sites input; configure the complete selected known-sites strategy. |
| Capture-aware WES | Germline path plus required manufacturer/kit/release, merged target BED, explicit calling padding and coverage on unpadded targets | Synthetic end-to-end run succeeded, including 1,000 target bases, 50-base padding and actual 9.481× mean target coverage. This does not meet a clinical 30× target or validate a real capture kit. Exact kit resources and representative full-exome accuracy remain required. |
| mtDNA | Explicit mitochondrial+nuclear candidate regions → extraction with mates → original/shifted references → realignment → Mutect2 mitochondrial mode → mitochondrial filtering → liftover/reconciliation → allele-fraction/coverage QC | Synthetic native run succeeded, with both call/filter branches and circular reconciliation. QC recovered five alternate reads out of 81 (6.17%) at one engineered site. Contamination/heteroplasmy sensitivity, NUMT discrimination and representative boundary-call accuracy remain unverified. This is not clinical mitochondrial interpretation. |
| SV / CNV / cohort / pedigree | Declared typed operations and selected native command profiles, explicit cohort/flow operators | Runtime publication, companion/reference contracts, distributed joint-genotyping and representative branch validation are incomplete. No full production branch support is claimed. |

No full-size WGS, WES or mtDNA assay has been scientifically validated. No clinical sign-off, independent ACMG classification, HIPAA/GDPR/SOC2/ISO certification or Origin/Illumina/Franklin parity is claimed.

## AWS data readiness and migration

Six exact public reference/truth objects were copied AWS-to-AWS with source ETag preconditions, destination version IDs and full-object SHA-256 checksums: Broad hg38/v0 FASTA/FAI/dictionary and HG002 v4.2.1 GRCh38 truth VCF/TBI/confident BED. Total: **3,420,060,130 bytes**. They are Ready components, not complete workflows. They are not silently substituted for another build/release.

The 14,032-file SSD inventory is fully hashed, including 10,828 unique contents totalling **473,449,575,292 bytes**. Originals were not modified. The whole collection has **not** been migrated. Hashing alone does not establish database release, rights or scientific compatibility. Legacy SSD entries remain visible to their owner but cloud execution refuses them until exact verified ingestion is complete.

Curated workflows run from their cloud image and private S3 without accessing the SSD or the desktop. No hosted execution is relayed through localhost. The broader reference/index/annotation and private collection migration is incomplete; no contrary availability claim is made. Shared components need reviewed binding/materialization and full bundle qualification before a complete assay can advertise Ready.

## Remaining work and resolution requirements

1. **Full assay qualification:** Bind exact references/indices/known sites/annotations, WES capture release and mtDNA nuclear-candidate/shift resources; run representative full-input datasets with separately funded allowances. Record accuracy, stratifications, runtime, allocation, peak memory, scratch, cost and failure recovery. Small smoke tests cannot close this.
2. **Catalogue completion:** Publish missing per-tool runtimes and validate each input/output/secondary-file contract. Four declarations lack executable native profiles; many other implementations have not run representative data. Commercial tools, databases and clinical APIs require actual vendor rights, credentials and reviewed bounded adapters.
3. **Personal-data result workbench:** Untrusted native genomic parsing is intentionally refused on the API host. A separately isolated indexing/pileup/omics worker service must be published before arbitrary Research Pro files can use that workbench. Private uploads and reviewed scientific workflows work independently of that unfinished viewer path.
4. **Durable operations:** Automated per-shard distributed retries, draining tier revocation, failed-upload creation reconciliation, orphan-output reclamation and dataset retention/deletion require additional work. Existing data is preserved; finite byte reservations remain in force.
5. **Large-data validation:** Only a 32-MiB interrupted/resumed upload and small native stages were exercised. Multi-hundred-GiB ingestion, high-memory VM execution and full scratch-pressure recovery remain untested. The ingestion API's configured size envelope is not proof at that size.
6. **Security/availability:** Governance checkpoints and the delivered scoped S3 data-event trail do not constitute a complete independent compliance archive. Archival of management/KMS activity, enterprise SSO/passkeys, independent penetration review and high availability remain unfinished. The low-cost single API host/single-AZ RDS can have deployment/recovery downtime.
7. **Scale:** 100 interactive synthetic users passed a bounded read test. Mixed 100-user/SSE stress, sustained workload tests and 1,000-user qualification remain pending. Upgrade the API to multiple stateless processes/hosts behind the existing edge, use a shared session/admission strategy and a larger PostgreSQL pool/database only after a measured bottleneck and funded shared capacity. Do not conflate interactive users with simultaneous genomes.

The active service is an evolving research implementation with working cloud execution and measured limits, not the completed universal scientific/clinical platform described in the broader objective.
