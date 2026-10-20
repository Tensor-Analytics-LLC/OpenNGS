# OpenNGS by Tensor Analytics

An open-source visual research studio for genomic workflows. Connect tools on a node canvas, inspect parameters and data contracts, execute pipelines and review results in the browser.

The self-hosted edition includes the complete declared catalogue, local data registration, tool onboarding, encrypted credentials and research result viewers. The hosted service offers **OpenNGS Explore** with real, bounded curated-sample execution and **Research Pro** with administrator-funded private ingestion and reviewed cloud execution. Missing runtimes, dependencies, licences and validation are disclosed. New work that cannot fit its allowance is refused or deferred; scientific inputs are never silently reduced.

**Live:** https://d2j3o1re9v00qo.cloudfront.net/ · [Readiness and remaining blockers](docs/READINESS.md) · [Costs](docs/COST_MODEL.md) · [Validation](docs/VALIDATION.md)

## Run locally

Requires Python 3.13. Node.js is needed only for graph tests.

```sh
git clone https://github.com/Tensor-Analytics-LLC/OpenNGS.git
cd OpenNGS
python3.13 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock
./start.sh
```

Open **http://127.0.0.1:8766**. First signup creates the administrator; subsequent accounts receive an eight vCPU-hour trial allocation. Start with the read quality, GIAB chr22, ClinVar, RNA quality or genome tracks templates. Bundled samples require no AWS account. Keep this desktop service bound to localhost.

Alternatively run `docker compose up --build`. Separate volumes preserve state and artifacts. No Docker socket or AWS credentials are mounted. Portable operations run by default. Native tools require an explicitly available Docker runtime or a published AWS Batch worker and approved immutable images.

## Configuration

| Variable | Purpose |
|---|---|
| `NGS_STATE_DIR` | Private account database, sessions, audit and encrypted Vault state |
| `NGS_DATA_DIR` | Artifacts and local indexes |
| `NGS_LOCAL_ROOT` | Filesystem root the administrator may browse/register |
| `NGS_RUNTIME_PYTHON` | Optional existing Python environment for the launcher |
| `NGS_PORT` | Local port; default 8766 |
| `NGS_AWS_PROFILE` / `AWS_PROFILE` | Your own AWS profile |
| `AWS_REGION` | Default us-east-1 |
| `NGS_DEMO` | Set 1 for the restricted sample contract; default 0 |
| `NGS_DATABASE_URL` | PostgreSQL DSN; omitted uses SQLite |

Settings expose data roots, runtime approvals, cache configuration, tool rights and credential bindings. Commercial tools, models and databases retain their own licences; a platform trial does not grant third-party rights.

## Included capabilities

- Canvas editing, worker-based graph validation, typed ports, explicit adapters, parameters, versioned graph saves, templates and execution history.
- Portable operations, native argv compilation, bounded execution, streaming events, cancellation, transactional quotas and verified caching in the local runner.
- Windowed VCF/BCF filtering, annotation evidence, research exports, indexed alignment pileups and source-driven omics charts.
- Authentication, CSRF/origin checks, tenant-scoped artifacts, encrypted credential bindings, authenticator MFA, audit verification and admin controls.
- AWS infrastructure generators for a private API origin, PostgreSQL state and isolated, digest-pinned Fargate sample execution, funded tenant queues and exclusive VM scratch profiles.

See [implementation coverage](IMPLEMENTATION_STATUS.md), [self-hosting](docs/SELF_HOSTING.md), [sample provenance](demo-data/README.md) and [security reporting](SECURITY.md).

## Verification and limits

```sh
python -m pytest -q
node --test tests/*.mjs
```

The checked source passed **69 backend tests and five JavaScript tests** on 20 October 2026. Tests cover application behavior and known-count scientific operators; they are not clinical assay validation. Real cloud execution includes synthetic germline, capture-aware WES and mitochondrial workflows, cache recovery, resumable ingestion and an exclusive VM. See the validation report for exact scope. Full-size assays remain unvalidated.

This is research software. Universal tool compatibility, unrestricted whole-genome scale, enterprise availability and regulatory certification are not established. Source ClinVar labels are not independent ACMG classifications. Synthetic datasets and incomplete public truth slices are labelled explicitly.

## Licence

Application source: [MIT](LICENSE). External tools and upstream dataset slices retain their own terms and attribution. No private local collection or patient uploads are included.
