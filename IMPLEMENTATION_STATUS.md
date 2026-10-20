# OpenNGS release status · 20 October 2026

The service is active at https://d2j3o1re9v00qo.cloudfront.net/. This release extends existing accounts, workflows, Vault, datasets and audit records. Spatial and Grepeye resources were preserved.

Current authoritative reports:

- [Implementation, tiers, migration readiness and exact remaining blockers](docs/READINESS.md)
- [Scientific, cloud, load, isolation and recovery evidence](docs/VALIDATION.md)
- [All-in baseline and separately funded Pro cost model](docs/COST_MODEL.md)
- [Complete declared capability matrix](docs/capability-matrix.json)
- [Dependency component and workflow readiness](docs/dependency-readiness.json)

Implemented, deployed, tested and scientifically validated are separate statuses. Real sample workflows, funded private ingestion, isolated native stages and an exclusive VM have executed. **Complete full-size WGS/WES/mtDNA scientific validation and the entire broader catalogue are not finished.** There is no clinical certification or universal interoperability claim.

Automatic expiry of completed-run S3 outputs is enabled after a live exact-version deletion qualification; source data, saved workflows and audit history remain protected. The private 473.45-GB inventory migration is running with resumable multipart transfers, exact AWS source copies and independent checksum verification. Migration progress is visible in Operations; it is not yet complete. The current approximately $151.56 all-in baseline projection includes bounded migration/validation reservations and remains a forecast rather than the posted AWS bill.


The catalogue now exposes 138 entries; the latest funded-owner snapshot has 92 Ready entries, 20 requiring credentials/rights and 26 with specific remaining dependencies or validation. Forty-five native contracts have actual small-control evidence. DeepVariant WGS has a separate tested CPU/model runtime, and VEP115.2 has actual offline annotation/rejection evidence while its complete original cache is still provisioning. Original ClinVar/HPO resources and complete GRCh37/GRCh38/T2T raw reference components are verified in AWS. Runtime readiness is not full-assay scientific validation.
