# OpenNGS operating cost model — 20 October 2026

USD, AWS us-east-1, 730-hour month. These are sustainable list-rate estimates and conservative application reservations, not a final AWS invoice. Credits and introductory free offers are excluded. Tax is reserved at 18%; the actual invoice jurisdiction and tax treatment must be reconciled by the account owner.

## Baseline and Explore

| Component | Monthly pretax allowance | Basis |
|---|---:|---|
| Interactive API | $24.53 | t4g.medium, standard CPU credits, private IPv6/CloudFront origin |
| PostgreSQL | $11.68 | Single-AZ db.t4g.micro; additional burst credits remain a variable risk |
| API disk + database disk | $4.70 | 30 GiB gp3 + 20 GiB RDS storage |
| Four private service endpoints | $29.20 | Secrets Manager, ECR API, ECR Docker and Logs |
| WAF | $7.00 | Web ACL/rules allowance; requests are variable |
| Encryption and database secret | $3.40 | Three customer-managed keys plus one secret |
| Images and private VM snapshot | $3.00 | Reviewed runtime images and encrypted AMI root snapshot |
| Storage, backups, logs, DNS, requests and transfer reserve | $26.49 | Includes up to approximately 600 GiB S3 capacity plus bounded operational overhead; use the actual footprint before expanding |
| **Fixed pretax reservation** | **$110.00** | Counted once, including shared scientific storage |
| **With 18% tax reserve** | **$129.80** | No Pro usage included |
| Safety margin | $15.00 | Unavailable to new job admission |
| **Maximum variable headroom** | **$55.20 including tax** | $46.78 pretax, less validation, accepted jobs and already reconciled usage |

The application targets $180 and stops accepting new paid work before its $200 baseline ceiling is exhausted. Reservations include queued and running work; uncertain provider outcomes retain their reservation. Already accepted jobs and existing data are preserved. AWS budget actual/forecast alerts are a second control, not the enforcement mechanism.

A conservative $2 pretax infrastructure-validation reservation was added this month. The internal Pro integration tenant has a separate $4 all-in maximum, allocated back to baseline validation. The original $2 integration allowance was increased to $4 for additional synthetic core-tool qualification, still inside the baseline. These are ceilings/reservations, not measured invoice charges. Build and test resources were temporary; both the AMI builder and exclusive test VM were terminated. Scientific workers scale to zero.

After germline/WES/mtDNA qualification, the application's October projection was **$134.39 including the tax reserve**, with **$50.61 available to new admission after the $15 safety margin**. This includes $2.00575 of outstanding pretax reservations and $1.880193 of reconciled conservative variable usage above the $110 fixed reserve. It is a cost-control snapshot, not the final posted AWS bill. Delayed or untagged charges still need invoice reconciliation.

### Scientific storage footprint

- Verified AWS reference components: **3,420,060,130 bytes**, approximately **$0.0733/month** in S3 Standard. Includes Broad hg38/v0 FASTA/FAI/dictionary and HG002 v4.2.1 GRCh38 truth VCF/TBI/confident BED. This is not a full aligner/annotation bundle.
- Read-only SSD inventory: **473,449,575,292 unique bytes**, approximately **$10.14/month** storage only if migrated. All 14,032 inventory entries were hashed; 10,828 unique contents were found. These bytes have **not** all migrated. Licensing, exact bundle provenance, index preparation and compatibility remain separate.
- The storage allowance is part of $110, not an extra hidden charge. A complete set of all catalogue databases has not been measured or acquired, so its affordability is not established. The smallest currently executable deployment is the curated, pinned sample bundle plus the existing API/database, with scale-to-zero execution.

### What the cap can and cannot do

Public traffic, WAF requests, signed-download URL reuse, billing lag, RDS surplus credits and unexpected provider charges prevent a mathematical guarantee that an AWS invoice can never exceed $200. Application-level guards bound admitted work and storage growth. Untagged historical charges and account-wide taxes need invoice reconciliation. Delayed Cost Explorer responses currently lack complete posted project costs; a zero response must not be represented as free operation.

## Research Pro

There is no invented subscription price. An administrator must assign a funded monthly monetary allowance and every resource/usage limit explicitly. A label change alone provides no compute rights. Dedicated tenant buckets/queues remain idle without jobs. Only the published runtime and supported resource profiles are usable.

Accounting separates shared platform, Explore, internal baseline-funded validation, each Pro tenant and Pro-attributable shared capacity. Project tags are active. TenantId tags are present on resources but AWS currently returns “Tag keys not found” when activation is requested; per-tenant posted-cost attribution awaits AWS tag discovery/activation. Application tenant ledgers remain enforced independently. Application reservations do not depend on tag reporting becoming immediately available. Posted charges and reservations are compared conservatively rather than added a second time.

### Per-job reservation formulas

For each active Pro node, before tax:

- Fargate lane: `((node wall seconds + 600) / 3600) × (vCPU × $0.06 + RAM GiB × $0.008 + $0.02)`.
- Exclusive VM lane: `((node wall seconds + 900) / 3600) × (published instance hourly rate + (20 + scratch GiB) × $0.08 / 730)`.
- Graph publication/request reserve: **$0.10** once per graph.
- Retained tenant capacity: `assigned storage GiB × $0.023/month`, reserved once per month; increases reserve only the delta. This is separate from upload request overhead and is not charged again for each uploaded copy.
- Tax is added once. No third-party API tariff, commercial licence, GPU or unreviewed profile is implicitly included.

Fargate estimates deliberately use a conservative tariff above the current on-demand CPU/RAM price. Spot savings are not pre-spent. On-Demand fallback is not automatic. Published exclusive profiles choose the smallest compatible size:

| VM | vCPU / RAM | Pretax compute per hour |
|---|---|---:|
| t3.medium | 2 / 4 GiB | $0.0416 |
| m7i.large | 2 / 8 GiB | $0.1008 |
| m7i.xlarge | 4 / 16 GiB | $0.2016 |
| m7i.2xlarge | 8 / 32 GiB | $0.4032 |
| m7i.4xlarge | 16 / 64 GiB | $0.8064 |

The T3 profile uses standard credits and is selected only for stage timeouts of at most 300 seconds. Longer CPU jobs use M7i. Encrypted delete-on-termination scratch is configurable up to 2 TiB; only the 2-vCPU/32-GiB scratch case has run in this release. Larger profiles require actual AWS capacity/quota and full-size validation.

Completed attempts record provider allocation intervals and reconcile at conservative published tariffs, with startup/minimum-duration and publication overhead. Native WES execution exercised this metering and bounded/redacted log delivery. Spot invoice prices and peak RSS/scratch were not measured; absent usage retains the original reservation. Old reconciled validation reservations were not retroactively discounted. Recorded conservative usage is not a final AWS invoice.

The full-input templates carry conservative per-stage maxima. Their reservation can exceed the baseline's entire variable headroom. The estimate endpoint returns the exact graph reservation and remaining tenant allowances before submission. This is an upper reservation, not a measured WGS/WES assay price. A reliable minimum budget for a complete assay requires representative runs, agreed coverage, read lengths, references, annotation scope and retention. No full-scale assay has been funded or measured in this release.

Sources: [EC2 prices](https://aws.amazon.com/ec2/pricing/on-demand/), [RDS pricing](https://aws.amazon.com/rds/postgresql/pricing/), [Fargate pricing](https://aws.amazon.com/fargate/pricing/), [S3 pricing](https://aws.amazon.com/s3/pricing/), [PrivateLink pricing](https://aws.amazon.com/privatelink/pricing/), [CloudFront VPC origins](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-vpc-origins.html). Runtime tariffs were also checked through AWS Price List API; review current rates before changing profiles.
