# OpenNGS validation evidence — 20 October 2026

Evidence is scoped below. Small integration tests, command qualification, full-scale performance and scientific assay accuracy are separate. Private operational evidence is retained locally under evidence/deployment; credentials, user inventories and signed URLs are excluded from public source.

## Application and scientific operators

**169 backend tests and 20 JavaScript tests passed.** These cover authorization, tenant isolation, atomic budgets/quotas/output holds, funded activation, companion bindings, input/cache integrity, idempotent VM recovery, immutable audit checkpoint tampering, native command compilation, WES target coverage, mitochondrial coordinate round-trips, operator known counts, DAG semantics, streaming SHA-256, provider metering, bounded log redaction and the independent deadline reaper. JavaScript syntax checks also passed.

Known-count examples include capture-target merging/padding, exactly 12× synthetic target coverage and mitochondrial rotation/liftover round-trip coordinates. Controlled GIAB-allele perturbations produced the expected 810 TP / 2 FP / 90 FN. That last result tests the exact-allele scoring operator; it is not a caller accuracy benchmark, and equivalent differently represented variants need normalization or haplotype-aware benchmarking.

## Private results, data rights and cleanup

Four isolated AWS preparations passed on synthetic private inputs: a 16,920-byte VCF produced one exact queryable/exportable allele; header inspection found its expected contig; a private CSV preserved all three plotted values; a 200-base alignment window returned exactly 100 reads at maximum depth 100 without truncation. Cross-tenant status, query and export attempts were denied (HTTP 422). The four workers each used 2 vCPU/4 GiB; observed provider allocation intervals were 113–160 seconds, including startup. Conservative admission held $0.592 pretax including upload overhead; this is not an invoice. An interrupted result ingestion was recovered from the existing successful worker output without launching another worker.

Private sources are staged in full within their admitted storage envelope before the bounded preview is prepared. The current implementation does not claim instant remote whole-genome browsing. Tests cover malformed interchange, excessive output, unsafe paths, tenant access, nonfinite chart values and truncation rejection.

Content-specific rights reviews bind the original database identity, SHA-256 and object version. Tests verify that generic source nodes, renamed copies, changed versions, cached outputs and worker metadata cannot remove known inherited obligations. Revocation is rechecked before execution. Administrative acknowledgment records existing authorization; it does not issue a vendor licence.

Derived-viewer cleanup requires a completed, exact-version retention deletion. Tests cover active read/ingestion protection, canonical paths, source preservation, missing-index regeneration and an interleaving where an old cleanup must not remove a newly published index. Baseline storage-accounting tests cover partial migrations, aliases, spoofed metadata, changed registrations and full physical Research Pro limits. A live derived-cache canary then removed only its disposable cache and JSON, returned an explicit expired status and verified that the original source version, audit trail and run tombstone were intact. The reviewed 473,449,575,292-byte plan was allocated inside the existing 600-GiB baseline without changing any entitlement.

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

The hosted tests use image-contained fixtures and private S3; scientific execution runs in AWS without a localhost server. The computer remained connected for observation, so a literal offline/disconnected-desktop test was not performed. AWS execution/staging paths contain no desktop dependency.

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

## Catalogue runtime completion and cross-bucket execution

The source catalogue contains **138 public entries**, including 11 additional named bindings for existing annotation/phenotype/transcript data families. A binding advertises neither a new download nor permission to use its data. Exact versioned asset selection, companion readiness and selected-content rights checks remain required. The [runtime matrix](runtime-capabilities.json) records each node's tested image/version, contracts, assertions and outstanding requirements.

Actual AWS execution evidence covers **43 native command contracts**:

- Eighteen inherited core contracts passed regression in the refreshed image. The authoritative core versions include BWA-MEM2 2.2.1, GATK 4.6.2.0, Picard 3.4.0, SAMtools/BCFtools 1.21, fastp 0.24.0, FastQC 0.12.1, Minimap2 2.28 and FreeBayes 1.3.6.
- Seventeen additional contracts passed: MultiQC 1.25.1, Trimmomatic 0.39, Bowtie2 2.5.4, HISAT2 2.2.1, STAR 2.7.11b, Salmon 1.10.3, Sniffles2 2.6.1, VarScan 2.4.6, Delly 1.3.1, WhatsHap 2.4, MACS3 3.0.3, Kraken2 2.1.3, CrossMap 0.7.3, deepTools 3.5.6, CNVkit 0.9.14, Bismark 0.24.2 and ExpansionHunter 5.0.0. Controls include actual read mapping, quantified fragments, peaks, coverage, a known taxon, identity-chain coordinates and a 20/20 repeat genotype. Some callers used negative controls only.
- Three additional contracts passed using existing core binaries: FilterMutectCalls with matching Mutect2 statistics; Picard LiftoverVcf preserving two identity-chain variants with zero rejects; and GATK two-sample CombineGVCFs/GenotypeGVCFs producing joint records and expected homozygous-reference genotypes. These do not prove somatic error modelling, cross-assembly accuracy or cohort/pedigree scale.

The five separately provisioned environments produced these measured outputs:

| Method/version | Actual input and assertion | Command elapsed | Limit of evidence |
|---|---|---:|---|
| Scanpy 1.12.4; igraph 1.0.0 | 64 cells × 128 genes; raw counts preserved, finite 64×2 UMAP, at least two Leiden clusters, fixed seed | 12.448 s | Synthetic populations; no batch correction, cell-type annotation or representative scRNA-seq accuracy |
| SnpEff 5.4.0c / Java 21 | Explicit artificial GFF3/FASTA-built database; known coding SNV at position 105 annotated as GENE1 missense | 0.361 s | No human annotation database availability or clinical interpretation claim |
| hap.py 0.3.15 / RTG 3.13 | Two truth and two query SNPs; exactly 1 TP, 1 FP, 1 FN, precision 0.5 and recall 0.5 | 3.103 s | Controlled SNP accounting; no GIAB caller benchmark or differently represented indel qualification |
| Strelka2 2.9.10 | Indexed engineering BAM/reference; complete configured germline workflow, valid zero-variant VCF | 2.831 s | Negative control only; sensitivity and somatic Strelka workflow unqualified |
| Manta 1.6.0 | Indexed engineering BAM/reference; complete configured workflow, valid zero-variant SV VCF | 1.089 s | Negative control only; positive SV recovery and full-size accuracy unqualified |

The exact optional-family image was then rerun in a separate AWS isolation audit: **all five methods passed again** with network disabled, read-only root, all Linux capabilities dropped, no-new-privileges, UID 1000, **2 vCPU / 2,500 MiB** memory limit, and fresh bounded writable mounts (512 MiB work, 128 MiB inputs, 512 MiB temporary space). This is a restricted execution check on these tiny controls, not a claim that representative datasets fit those resources. The image digest and audit source/evidence hashes are bound in the release manifest.

Environment installation failures and failed execution attempts were retained as evidence. Publication uses only passing outcomes. These measurements are command times within AWS build qualification; they exclude image acquisition, environment construction and tenant cold starts. They do not establish peak memory or scratch requirements. Human SnpEff/VEP resources and other full assay dependencies must still be bound and verified.

The refreshed image additionally passed **13 portable regressions**: SAM→BAM, BAM sort/index/statistics, VCF BGZF/index/filter/statistics, FASTQ quality summary, paired/interleaved FASTQ conversion, and BED/IntervalList conversion. Assertions preserved 2,000 mapped alignment records, the two VCF positions and indexed queries, byte-identical uncompressed mates for 1,000 pairs, and the exact 0-based/1-based interval round-trip. The audit used the immutable image, nonroot UID 1000, read-only root, no network, dropped capabilities and bounded writable mounts.

An actual funded-tenant, seven-node graph then read **three verified private migrated objects totalling 71,175 bytes from the shared cache bucket**, while its output bucket remained tenant-specific. Exact source version/SHA-256/length grants and tenant IAM constrained access. Trimmomatic → Bowtie2, FastQC → MultiQC, and Salmon all succeeded in the approved runtime. Outputs preserved **2,000/2,000 mapped reads** and **1,000 assigned fragments**. Run `run_f556206ebf1e492b95fbac18c574c9ca` completed in **639.359 s**; five actual workers ran for **24.875–41.014 s** each with 2 vCPU and 4 GiB RAM. Cold-start/staging/orchestration time is included in the overall result. The maximum admitted cost reservation was **$0.272 pretax**, not a final AWS invoice amount. No allowance was increased for this test.

The default old portable image and old exclusive-VM AMI do not gain migrated-source support merely because other routes were refreshed. Unsupported combinations return `MIGRATION_RUNTIME_PENDING` before provider access. The 13 refreshed portable routes and qualified native/viewer routes carry an explicit source-grant contract version. DeepVariant, VEP and vendor/model-specific runtime gaps remain disclosed in the matrix; they are not claimed complete.

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
