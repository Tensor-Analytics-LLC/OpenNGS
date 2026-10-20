"""Build a reviewed source-only snapshot. Never include live state or inventories."""
import hashlib,json,re,shutil
from pathlib import Path

def main():
    paths=[]
    for d in ['backend','frontend','demo-data','docs','tests','.github']:
        paths.extend(p for p in Path(d).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc')
    paths.extend(Path(x) for x in ['README.md','IMPLEMENTATION_STATUS.md','LICENSE','SECURITY.md','Dockerfile','compose.yaml','.dockerignore','.gitignore','requirements.lock','requirements.txt','start.sh','infra/cache-stack.json','infra/cloud_worker.py','infra/worker.Dockerfile','infra/demo.Dockerfile','infra/demo_compute.py','infra/demo_worker.py','infra/lean_stack.py','infra/bootstrap.sh','infra/rds-ca.pem','infra/openngs-demo.service','infra/openngs-verifier.service','scripts/prepare_demo_data.py','scripts/import_cloud_state.py','scripts/build_demo_image.py','scripts/publish_release.py','scripts/package_source.py'])
    paths.extend(Path(x) for x in ['scripts/qualify_core_cloud.py','scripts/qualify_core_refresh.py','scripts/qualify_core_adjunct.py','scripts/finalize_core_qualification.py','scripts/prepare_migration_validation.py'])
    records=[];destination=Path('.state/public-source');destination.mkdir(parents=True,exist_ok=True)
    paths.extend(Path(x) for x in ['infra/budget_stack.py','infra/openngs-billing.service','infra/research_stack.py','infra/research.Dockerfile','infra/openngs-research.service','scripts/acquire_references.py','scripts/build_research_image.py','scripts/publish_research_capacity.py','scripts/build_vm_image.py','scripts/finalize_vm_image.py','scripts/publish_vm_capacity.py','infra/network_guard.py','scripts/publish_network_guard.py','infra/audit_stack.py','infra/openngs-audit.service','scripts/publish_audit.py','scripts/configure_services.py','scripts/verify_cloud_samples.py','scripts/verify_research_cloud.py','scripts/verify_vm_cloud.py','scripts/load_cloud.py'])
    paths.extend(Path(x) for x in ['infra/worker_reaper.py','scripts/publish_worker_reaper.py'])
    paths.append(Path('scripts/promote_timestamp_precision.py'))
    paths.append(Path('scripts/allocate_reviewed_migration_storage.py'))
    paths.append(Path('scripts/publish_cloud_references.py'))
    paths.extend(Path(x) for x in ['scripts/verify_private_viewer_cloud.py','infra/extended-refresh.Dockerfile','scripts/verify_retention_cloud.py'])
    paths.extend(Path(x) for x in ['infra/extended.Dockerfile','infra/extended-packages.txt','infra/viewer_worker.py','infra/openngs-retention.service','infra/openngs-migration.service','scripts/build_extended_runtime.py','scripts/finalize_extended_runtime.py','scripts/qualify_extended_runtime.py','scripts/migrate_inventory.py','scripts/publish_retention.py','scripts/verify_extended_cloud.py'])
    paths.extend(Path(x) for x in ['infra/secondary.Dockerfile','infra/secondary-refresh.Dockerfile','infra/secondary-environments.json','scripts/qualify_secondary_cloud.py','scripts/report_runtime_matrix.py','scripts/finalize_secondary_audit.py','scripts/build_secondary_runtime.py','scripts/finalize_secondary_runtime.py','scripts/install_secondary_environments.py','scripts/qualify_secondary_runtime.py','scripts/qualify_portable_refresh.py'])
    for p in sorted(set(paths)):
        if p.is_symlink() or not p.resolve().is_relative_to(Path.cwd().resolve()):raise ValueError('Public source must be a regular workspace file')
        raw=p.read_bytes()
        if re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY',raw):raise ValueError('Disallowed identifier or key in '+str(p))
        if p.suffix!='.pem' and re.search(rb'\bAKIA[0-9A-Z]{16}\b',raw):raise ValueError('Possible access key in '+str(p))
        if any(s in p.parts for s in ['.state','evidence','.aws','sources']):raise ValueError('Private source path')
        target=destination/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,target)
        records.append({'path':str(p),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'mode':'100755' if str(p)=='start.sh' else '100644'})
    Path('.state/public-release.json').write_text(json.dumps(records,indent=2))
    print(json.dumps({'files':len(records),'bytes':sum(x['bytes'] for x in records),'secret_scan':'passed; certificate-only PEM bundle separately verified'}))

if __name__=='__main__':main()
