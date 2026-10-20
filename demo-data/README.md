# Sample provenance

`manifest.json` records sizes, SHA-256 digests, semantic formats, sources and public/synthetic flags. Fixtures demonstrate application behavior, not clinical accuracy.

| Node | Contents |
|---|---|
| `sample.giab` | First 1,000 chr22 records from HG002 GRCh38 GIAB/NIST v4.2.1. [Source release](https://giab.s3.us-east-1.amazonaws.com/release/AshkenazimTrio/HG002_NA24385_son/NISTv4.2.1/GRCh38/). An incomplete variant slice, not a full caller benchmark. |
| `sample.clinvar` | First 1,000 source records from a public GRCh38 ClinVar snapshot. Source timestamp is retained in the manifest. [Source directory](https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/), [data use](https://www.ncbi.nlm.nih.gov/clinvar/docs/maintenance_use/). Labels are source assertions. |
| `sample.rna` | Tiny public bacterial paired RNA reads from [nf-core/test-datasets](https://github.com/nf-core/test-datasets/tree/cad884e7fbe5617dcd55bdd07d9ddf65b54febe4/testdata/prokaryotic), pinned to that commit. Retain upstream attribution and terms. |
| `sample.fastq` | Deterministic synthetic paired reads, seed 7341. |
| `sample.somatic` | Synthetic engineering tumour/normal reads, seeds 7351/7341. Not a biological cancer benchmark. |
| `sample.alignments` | 100 synthetic indexed alignments and matching reference. |
| `reference.tiny` | Synthetic 20,000-base engineering reference and FASTA index. |

OpenNGS synthetic fixtures use the application MIT licence. That licence does not replace upstream data terms, attribution or disclaimers. No private database collection or patient uploads are included.
