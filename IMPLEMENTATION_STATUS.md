# Current release status · 20 October 2026

The public cloud endpoint is https://d2j3o1re9v00qo.cloudfront.net. The API uses PostgreSQL and a private origin; the sample Fargate image is digest-pinned. Cloud scientific validation is in progress. Local source passes 41 backend and four graph tests. No full-scale WGS/WES/mtDNA or clinical readiness is claimed.

Explore is restricted on the server to approved sample sources, reviewed tools, bounded parameters and explicit daily/monthly CPU, queue, resource, output, retention and export limits. Research Pro requires an administrator-assigned funded allowance. The baseline ledger is capped at $200; additional Pro reservations are allocated separately. Funded labels alone do not install missing runtimes or reference dependencies.

The machine-readable [capability matrix](docs/capability-matrix.json) and [dependency report](docs/dependency-readiness.json) distinguish executable operators, pending validation, third-party entitlement prerequisites and unsupported catalogue declarations. The original inventory below records earlier research verification and is not a claim of current cloud/full-scale readiness.

# OpenNGS implementation inventory

This is a cumulative upgrade of the existing studio. Existing accounts, saved graphs, data index, Vault, audit chain, cache and Spatial design tokens are retained. Synced `sources/` files and the independent Spatial application were not changed.

## Delivered and exercised

| Area | Working implementation | Evidence |
|---|---|---|
| Catalogue | 116 entries, including 62 portable/source operations and 39 compiling native command profiles; 12 editable templates | `evidence/catalogue-audit.json` |
| Tool onboarding | Literal CWL, OpenAPI scalar query/path/body controls, immutable image resolution, reviewed publication; bounded source/help discovery | Parser/compiler/publication tests and browser inspection |
| Interoperability | Explicit representation adapters, branch-local build checks, actual header/index inspection, runtime type checks, conditional routing, sample manifests and bounded scatter/gather | Round-trip, known-count and namespace tests |
| Validation | Exact-allele precision/recall/F1/TiTv, Wilson intervals, QC cutoffs, supplied-p-value BH correction; actual hap.py native command contract | `tests-result.xml`, `evidence/aws-calibration.json` |
| Tertiary results | Disk index, virtual row windows, source-aware annotations, multiallelic zygosity, faceted filtering, evidence inspector and filtered exports | GIAB 1,003 alleles; ClinVar 999 alleles; browser verification |
| Visualization | Indexed alignment pileup/range endpoint; source-driven histograms, scatter/volcano/MA/heatmaps and existing AnnData embeddings | 100 known synthetic alignments; byte-range tests |
| Caching | Per-workspace node content hashes, verified primary/secondary outputs, upstream reuse after downstream edits | Cache corruption/invalidation tests; live repeated runs |
| Governance | Authentication, authorization, CSRF/origin, quota reservation, encrypted Vault, revocation, kill/freeze/quarantine and audit chain | Original and extension security tests; live KMS verification |
| Data | Existing 14,032-file SSD inventory, ranked public sources and deduplicated private S3 multipart upload | `cloud-validation.json` |
| Run summary | Automatic measured QC/audit JSON artifact on termination plus an in-app summary viewer | `evidence/live-verification.json` |

## Measured verification

- Current local source: **31 backend tests + 4 graph tests passed**. A Starlette/httpx deprecation warning remains; no failing tests.
- AWS uploaded snapshot: **29 tests passed**. Two later local tests cover allele-specific zygosity and annotation-report joins; the AWS snapshot identity is recorded with its archive hash.
- AWS synthetic load probe: **1,000,000 allele rows**, 46.85 s generation/indexing, 89.41 ms p50 and 92.31 ms p95 filtered queries, 485,642,240-byte index, 85,848 KiB peak process RSS on t3.medium in us-east-1a. This measures server indexing/query performance, not clinical accuracy or browser rendering a million rows.
- Controlled GIAB-locus perturbation: exactly **810 TP / 2 FP / 90 FN**, with correct derived metrics. This calibrates the operator; it is not a benchmark of a sequencer or variant caller.
- Browser canvas probe: 500 nodes, 2,000 drawable edges; p95 drawing 4.1 ms, p95 frame interval 34.3 ms. The observed session did not establish 60 FPS.
- ClinVar source inspection: 494 VUS-labelled alleles in the 999-allele subset. Conflicting pathogenicity text remains conflicting; no independent pathogenicity classification was generated.
- Temporary AWS validation compute and network stack: deleted; no active validation hosts. Retained cache S3/KMS remains billable. Spatial and Grepeye resources were not modified.

## Remaining engineering and external prerequisites

1. **Native compute fleet:** Batch queues/definitions, immutable scientific worker images, per-tool dependencies and sample/reference contracts still need publication, execution testing and operation-specific benchmarking. Compiling 39 command profiles is not evidence that 39 binaries ran successfully. Existing templates explicitly require actual source assets and approved runtimes. Public-S3 discovery/probing is implemented; automatic binding and streaming execution for every public object is not. Current cloud workers stage admitted files through the private tenant cache.
2. **Hostile multi-tenancy:** The local edition is authenticated desktop research software. Credentialed staging/publishing must be separated from untrusted scientific workloads before PHI or hostile containers are admitted in production. Independent IAM/network controls, scoped storage identities, reconciliation, WORM anchoring, SSO/MFA/enterprise billing and deployment hardening remain necessary. No compliance certification is claimed.
3. **Vendor access:** Franklin schemas/endpoints, commercial binaries, eligible hardware and vendor licence verifiers cannot be supplied by a platform trial. Gated operations refuse execution instead of reporting simulated success. Decrypting a token is not licence validation.
4. **Universal DSLs and semantics:** Arbitrary shell/JavaScript workflows are not auto-translated. Assemblies require exact references and liftover chains; directory/complex-output workflows require reviewed contracts. Every conceivable tool combination cannot be scientifically guaranteed.
5. **Cohort parallelism:** Shards and explicit cohorts exist. Automatic per-shard distributed scheduling/retries and complete scalable joint-genotyping operations have not been deployed.
6. **Clinical tertiary interpretation:** HPO/inheritance filters use supplied annotations; phenotype inference, compound-heterozygote adjudication, independent ACMG evidence evaluation and clinical sign-off are not built. Full raw-to-report clinical assays are unvalidated.
7. **Extended omics:** Charts and native analysis command profiles exist. Full remote HDF5/Zarr expression analytics, model/embedding validation, differential-expression model fitting and mutational-signature fitting remain outside the implemented research paths.
8. **Commercial readiness:** Whole-genome/long-read/scRNA performance, recovery/failover, all native licensing terms, independent security review and assay-specific clinical validation remain uncompleted. The current build is not an Illumina/Franklin parity claim.

See `README.md` for execution, data semantics and deployment boundaries. Each node's actual licence/runtime status is retained in the catalogue audit.
