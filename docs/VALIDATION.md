# OpenNGS validation evidence — 20 October 2026

Evidence is scoped below. Small integration tests, command qualification, full-scale performance and scientific assay accuracy are separate. Private operational evidence is retained locally under evidence/deployment; credentials, user inventories and signed URLs are excluded from public source.

## Application and scientific operators

**69 backend tests and 5 JavaScript tests passed.** These cover authorization, tenant isolation, atomic budgets/quotas/output holds, funded activation, companion bindings, input/cache integrity, idempotent VM recovery, immutable audit checkpoint tampering, native command compilation, WES target coverage, mitochondrial coordinate round-trips, operator known counts, DAG semantics, streaming SHA-256, provider metering, bounded log redaction and the independent deadline reaper. JavaScript syntax checks also passed.

Known-count examples include capture-target merging/padding, exactly 12× synthetic target coverage and mitochondrial rotation/liftover round-trip coordinates. Controlled GIAB-allele perturbations produced the expected 810 TP / 2 FP / 90 FN. That last result tests the exact-allele scoring operator; it is not a caller accuracy benchmark, and equivalent differently represented variants need normalization or haplotype-aware benchmarking.

## Actual hosted Explore workflows

| Workflow | Data/scope | Outcome | Observed elapsed |
|---|---|---|---:|
| Read quality | 1,000 synthetic paired reads | Succeeded; 3 artifacts | Initial 1,217 s includes IAM debugging; not a steady-state runtime |
| GIAB variants | Public HG002 chr22 truth slice, 1,000 source records | Succeeded; 4 artifacts | 69.10 s |
| ClinVar | Public GRCh38 subset, 999 alleles after indexing | Succeeded; 2 artifacts; 494 source-labelled VUS | 73.59 s |
| Indexed alignment fixture | Known synthetic alignments/reference/index | Succeeded; 4 artifacts | 69.37 s |
| Native read QC | Actual FastQC and fastp on synthetic reads | Succeeded; 12 artifacts | 84.75 s |
| DNA engineering | Actual Minimap2, HTSlib/SAMtools and FreeBayes | Succeeded; 12 artifacts | Initial 1,237 s includes archive-publication debugging; not a steady-state runtime |
| RNA read quality | Pinned public bacterial paired reads | Succeeded; 3 artifacts | 84.52 s |

The hosted tests use image-contained fixtures and private S3; scientific execution does not depend on the SSD or a localhost server. The computer remained connected for observation, so a literal offline/disconnected-desktop test was not performed. AWS execution/staging paths contain no desktop dependency.

Results verification included real ClinVar filtering and an authenticated 32-byte S3 range response (HTTP 206). The cloud pileup initially failed because the FASTA interface rejected a remote signed URL. A bounded FAI-directed S3 reference reader fixed that path: the 50-base checked window now returns **100 synthetic alignments with depth 100**, matching the fixture. Pileup coverage is explicitly limited to rendered reads, not whole-file depth.

Explore private ingestion returned HTTP 403. An altered sample source parameter returned HTTP 422. Server authorization and immutable datasets remain authoritative even with modified clients.

## Research Pro ingestion, compute and recovery

The synthetic validation tenant is funded from the already authorized baseline, with a **$4 all-in internal maximum** after the initial $2 ingestion/VM validation. No external Pro customer was funded or promised unlimited scaling.

- **Multipart ingestion:** a synthetic **32 MiB binary object** was interrupted after the first **16 MiB** part; a fresh authenticated session found the same transfer and uploaded part, then resumed it. AWS-side full-content SHA-256 verification reached Ready. Another tenant's transfer lookup returned 422. This tests transport/integrity, not analysis of a 32-MiB scientific sample.
- **Native execution:** 1,000 synthetic read pairs ran through actual fastp and QC in a digest-pinned private tenant Fargate runtime. Run run_e106a3c7ab27488bbdaa6d2a339f659e succeeded with 6 artifacts in **187.58 s**, including separate worker startup for stages.
- **Exclusive machine:** run run_5d3e54d083a246ff9d32c9608e0117c1 completed actual fastp on a fresh **2-vCPU / 4-GiB VM with 32 GiB encrypted scratch**. A scoped PassRole denial was reconciled using the same accepted run, attempt and provider client token. The final run succeeded with 10 retained artifacts, including recovery summaries; allocated CPU was 145 s. The initial debug interval is included in wall time. The VM and AMI builder are terminated, and no attempt scratch volumes remain.
- **Budget control:** an authenticated SNS test alert reached the real consumer. New Pro spending returned **429**, its estimate reported that admission did not fit, the already accepted scientific pipeline stayed running, and Explore admission remained open. The operator cleared only this synthetic alert hold with an audit reason.
- **Recovery:** upstream identity checks, companion-sensitive cache keys, immutable accepted inputs, provider submission intents and VM request replay are tested. The permission-recovery execution above is actual cloud evidence. A complete regional failure/backup restore has not been qualified.

The configured large-file and high-memory resource envelopes are not proof at those sizes. Only the stated 32-MiB resumed transfer and 32-GiB scratch VM were exercised. Peak scientific process memory and peak scratch consumption were not captured in these cloud tests; the interface displays unsampled telemetry rather than invented zero values.

## Native assay command qualification

| Workflow | Actual AWS execution | Result and limits |
|---|---|---|
| Germline core | All 1,000 synthetic read pairs, 50,905 compressed read bytes, 20-kb engineering reference and synthetic known-site/annotation data | FastQC, BWA-MEM2, coordinate sort, Picard duplicate marking, indexing, BQSR/ApplyBQSR, HaplotypeCaller, bcftools normalization/annotation and variant statistics succeeded. The original run failed only at output routing. After the routing fix, recovery completed with **23 artifacts**, reusing **all 11 computational stages** from verified cache; no scientific workers relaunched. |
| Capture-aware WES | Same synthetic read/reference data, explicit synthetic kit/release, **1,000 unpadded target bases**, **50-base calling padding** | **Succeeded**, 19-node graph, **504.59 s**. Actual mean target depth **9.481×**, fraction ≥10× **0.501**, fraction ≥20×/30× **0**. This is not a clinical coverage pass, real capture-kit validation or full exome. Native worker logs and provider allocation metering were received. |
| Mitochondrial | 100 synthetic aligned reads; 20-kb engineering mitochondrial contig plus 1-kb nuclear stub; explicit nuclear-candidate interval | **Succeeded**, 16-node graph, **29 artifacts**, **1,220.93 s**. Extraction, shifted reference, both alignments, Mutect2 mitochondrial calls, both filters, liftover and circular reconciliation ran. Base-count QC recovered the **5 engineered alternate reads / 81 eligible reads = 6.17%** at 1-based position 981. One interior call was retained and an overlapping source call excluded in reconciliation. This tests one positive control, not NUMT classification, contamination, indel sensitivity or a clinical heteroplasmy limit. |

The native qualification stages used **2 vCPU, 4 GiB RAM and 300-second per-node limits**. Fargate provisioned 200 GiB ephemeral storage; the graph declared a smaller working requirement. Peak RSS and peak scratch use were not sampled. Wall times include isolated worker startup and orchestration, not only tool execution. Full-size WGS, WES and mtDNA assays have not run. No representative caller precision/recall claim follows from these synthetic tests.

## Interactive performance

A bounded live test after the catalogue optimization, with **100 independent synthetic authenticated users**, completed **2,307/2,307 requests with HTTP 200**, with human-like think time:

- p50: **306 ms**
- p95: **598 ms**
- p99: **745 ms**
- Maximum: **1,311 ms**

It exercised session, workflow-list, catalogue and entitlement reads over HTTPS for about 63 seconds. The preceding stable run also passed 2,250/2,250 requests at 851-ms p95. An earlier run had 35 failures while pool sizing and a live API restart were being debugged; both stable runs excluded a concurrent deployment. This is not sustained production capacity or 100 simultaneous genome analyses. Mixed 100-user/SSE stress remains pending; stream admission separation has a focused concurrency test.

The live browser renderer probe drew **500 nodes and 2,000 edges** for 90 frames on this Mac's in-app browser: **16.7 ms median frame interval, 17.6 ms p95 frame interval, 1.8 ms p95 drawing**, device-pixel ratio 2 and a 684×383 canvas. This is approximately 60 FPS on the measured device. It is not a 120-FPS or all-device guarantee.

## Network, audit and security

A fresh Fargate task in the actual scientific subnet/security profile observed:

| Probe | Result |
|---|---|
| Unapproved example.com DNS | Blocked |
| Approved regional S3 DNS | Resolved |
| Unapproved public 1000 Genomes S3 listing from compute | HTTP 403 |
| Direct external 1.1.1.1 TCP/443 | Timed out; no internet route |

The probe exited successfully and its task stopped. These observations establish the tested egress restrictions, not an independent penetration test. A preliminary Batch probe used the image's fixed entrypoint incorrectly and failed before testing connectivity; the direct reviewed ECS task above supplied the correct entrypoint.

A separate real Fargate task using the tenant's actual runtime IAM role could read its own ingested input and write its run output. Reading a foreign tenant's object returned **403**; overwriting its own immutable input returned **AccessDenied**. The probe exited successfully and stopped. This supplements API tenant-isolation tests with actual AWS authorization evidence.

The independent one-minute AWS deadline reaper was exercised with a fresh empty private worker, no data or instance profile and a three-minute guest backup timer. The scheduled Lambda logged its termination, and AWS confirmed **terminated after 99.28 seconds**, before the guest backup timer. The reaper checks the OpenNGS VPC, exact project tag, tenant/run identities and expired deadline; its IAM role has no dataset or secret access. The application and original guest timer remain separate safeguards. There is still no guarantee against all AWS control-plane outages.

The deployed audit API verified a version-pinned encrypted Governance Object Lock checkpoint with 30-day retention against the database chain prefix. A unit test detected a changed prefix hash. Raw audit bodies remain in RDS. Application/worker IAM has no delete or governance-bypass grant; privileged AWS administrators can still bypass Governance if separately authorized.

The OpenNGS-only CloudTrail data-event trail is logging. Actual S3 HeadObject records and digest files were delivered to the versioned encrypted archive; sampled log objects have Governance retention through 19 November. Log-file validation is enabled, but an independent cryptographic digest-chain validation was not performed. This trail is scoped to the project's object access and excludes its own log-delivery prefix. Management/KMS-event archival and a complete compliance audit remain unfinished.

No compliance certification, enterprise availability, clinical validation or full-catalogue interoperability is established. See [readiness and exact blockers](READINESS.md), the [capability matrix](capability-matrix.json), [dependency readiness](dependency-readiness.json) and [cost model](COST_MODEL.md).
