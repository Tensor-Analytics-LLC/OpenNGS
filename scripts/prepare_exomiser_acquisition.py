"""Build an offline private Exomiser acquisition package; never contacts AWS.

The operator must separately review permissions, create one new target-bound
shared hold, stage immutable source/admission versions, and start a build once.
This preparation does none of those actions and includes no admission record.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
from zipfile import ZipFile,ZipInfo,ZIP_DEFLATED

from scripts import acquire_exact_exomiser_archives as acquisition

BASE_SHA256='56609732edd8925e31502acc8d395baa0264622780893a656d048a577e4842a2'
PROJECT_ROOT=Path(__file__).resolve().parents[1]


def digest(raw):return hashlib.sha256(raw).hexdigest()


def encode(value):return (json.dumps(value,sort_keys=True,indent=2)+'\n').encode()


def buildspec():
    return {'version':'0.2','env':{'shell':'bash','variables':{
        'AWS_DEFAULT_REGION':'us-east-1','AWS_REGION':'us-east-1','EXOMISER_ARCHIVE':'',
        'NGS_STATE_DIR':'/tmp/openngs-exomiser-acquisition','NGS_DATA_DIR':'/tmp/openngs-exomiser-acquisition/data',
        'PIP_DISABLE_PIP_VERSION_CHECK':'1','PIP_NO_INDEX':'1','PYTHONDONTWRITEBYTECODE':'1'}},
        'phases':{'install':{'commands':['python3 verify_package.py','python3 -m venv /tmp/openngs-exomiser',
            '/tmp/openngs-exomiser/bin/python -m pip install --no-index --no-cache-dir --require-hashes --only-binary=:all: --find-links wheels -r infra/acquisition-requirements.lock']},
        'build':{'commands':['/tmp/openngs-exomiser/bin/python -m scripts.codebuild_exact_exomiser_entry '
            '--archive "$EXOMISER_ARCHIVE" --directory metadata --checkpoint-directory /tmp/exomiser-exact-checkpoint '
            '--execute --codebuild-admission "$EXOMISER_ADMISSION_URI" --admission-version "$EXOMISER_ADMISSION_VERSION" '
            '--admission-sha256 "$EXOMISER_ADMISSION_SHA256" --wall-seconds 2700']}}}


def prepare(directory,output,base_zip):
    raw=Path(base_zip).read_bytes()
    if digest(raw)!=BASE_SHA256:raise ValueError('The frozen offline dependency source ZIP differs')
    with ZipFile(io.BytesIO(raw)) as base:
        manifest=json.loads(base.read('source-file-manifest.json'))
        if len(base.namelist())!=len(set(base.namelist())):raise ValueError('Duplicate frozen source members')
        for row in manifest['files']:
            content=base.read(row['path'])
            if (len(content),digest(content))!=(row['bytes'],row['sha256']):raise ValueError('Frozen offline source member differs')
        members={name:base.read(name) for name in ['backend/__init__.py','backend/config.py','scripts/prioritize_migration_objects.py']}
        wheels=[name for name in base.namelist() if name.startswith('wheels/') and name.endswith('.whl')]
        if len(wheels)!=7:raise ValueError('Exactly seven existing hash-pinned offline wheels are required')
        members.update({name:base.read(name) for name in wheels})
        members['infra/acquisition-requirements.lock']=base.read('infra/vep-requirements.lock')
    objects=[]
    for archive in sorted(acquisition.ARCHIVES):
        plan,obj,registration,size=acquisition.load_local_plan(Path(directory),archive);objects.append(obj)
    members['metadata/plan.json']=(Path(directory)/'plan.json').read_bytes()
    members['metadata/registration.json']=(Path(directory)/'registration.json').read_bytes()
    for name in ['scripts/acquire_exact_exomiser_archives.py','scripts/codebuild_exact_exomiser_entry.py']:
        members[name]=(PROJECT_ROOT/name).read_bytes()
    spec=encode(buildspec());members['buildspec.yml']=spec
    verifier='''"""Verify private code/dependency metadata without network access."""
import hashlib,json
from pathlib import Path,PurePosixPath
root=Path.cwd().resolve();manifest=json.loads((root/'source-file-manifest.json').read_bytes())
assert manifest['schema']==1 and len(manifest['files'])==17
for row in manifest['files']:
    name=PurePosixPath(row['path']);assert not name.is_absolute() and '..' not in name.parts
    path=root.joinpath(*name.parts);assert path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root)
    raw=path.read_bytes();assert len(raw)==row['bytes'] and hashlib.sha256(raw).hexdigest()==row['sha256'],row['path']
assert len([r for r in manifest['files'] if r['path'].startswith('wheels/')])==7
print(json.dumps({'verified_source_files':len(manifest['files']),'verified_offline_wheels':7}))
'''
    members['verify_package.py']=verifier.encode()
    rows=[{'path':name,'bytes':len(content),'sha256':digest(content)} for name,content in sorted(members.items())]
    source_manifest={'schema':1,'purpose':acquisition.PURPOSE,'owner_id':plan['owner_id'],'plan_id':plan['plan_id'],
        'plan_sha256':acquisition.migration.APPROVED_PLAN_SHA256,'files':rows,
        'scope':'Private code and exact original migration metadata; no archive data or credentials'}
    manifest_raw=encode(source_manifest);members['source-file-manifest.json']=manifest_raw
    archive=io.BytesIO()
    with ZipFile(archive,'w',compression=ZIP_DEFLATED,compresslevel=9) as zipped:
        for name,content in sorted(members.items()):
            info=ZipInfo(name,(1980,1,1,0,0,0));info.compress_type=ZIP_DEFLATED;info.external_attr=0o100600<<16
            zipped.writestr(info,content)
    source=archive.getvalue()
    if len(source)>64*1024**2:raise ValueError('Private code package exceeds its metadata source bound')
    targets={obj['name']:{'bytes':obj['bytes'],'sha256':obj['sha256'],'provider':acquisition.profile(obj),
        'new_shared_hold_details':{'purpose':acquisition.PURPOSE,'archive_sha256':obj['sha256'],
            'plan_sha256':acquisition.migration.APPROVED_PLAN_SHA256,'source_zip_sha256':digest(source),
            'maximum_seconds':2700,'maximum_attempts':1}} for obj in objects}
    package={'schema':1,'status':'Prepared locally only; no funding, staging, admission, launch or data writes',
        'source_zip_sha256':digest(source),'source_zip_bytes':len(source),'source_file_manifest_sha256':digest(manifest_raw),
        'entrypoint_sha256':digest(members['scripts/acquire_exact_exomiser_archives.py']),
        'wrapper_sha256':digest(members['scripts/codebuild_exact_exomiser_entry.py']),'buildspec_sha256':digest(spec),
        'frozen_offline_dependency_source_sha256':BASE_SHA256,'offline_wheels':7,'source_files_excluding_manifest':17,
        'project':acquisition.BUILD_PROJECT_ARN,'service_role':acquisition.BUILD_ROLE,'managed_image':'aws/codebuild/standard:7.0',
        'compute_type':'BUILD_GENERAL1_MEDIUM','privileged_mode':False,'maximum_build_minutes':45,
        'maximum_attempt_pretax_usd':.80,'new_shared_hold_required_per_target_per_attempt':True,
        'cost_scope':'Conservative rounded submitted-build-to-termination minutes at reviewed .01/min, requests/KMS, seven-day private multipart storage and boundary headroom; reconcile actual provider times separately',
        'startbuild_policy':'No automatic launch/retry. Persist exact intent and accepted build ID. An unknown StartBuild outcome requires operator reconciliation; container admission cannot prevent an externally submitted build minimum charge.',
        'attempt_claim':'Permanent per-reservation conditional claim; another build/receipt/source/target cannot reuse the same hold',
        'maximum_shared_raw_buffers':8,'maximum_shared_raw_buffer_bytes':64*1024**2,'maximum_inflight_uploads':2,
        'part_bytes':8*1024**2,'maximum_identical_part_attempts':3,'progress_interval_seconds':30,
        'source_identity_basis':'No invented ETag/Last-Modified: exact HTTPS ranges, source size, full original SHA-256 before conditional commit',
        'checkpoint_compatibility':'Independent per-original-SHA CAS checkpoint. Original migration parts/ledger untouched. Fresh funded terminal successor preserves private upload and prior admission history.',
        'ready_or_rights_grants':False,'extraction_performed':False,
        'extraction_scope':'Archive acquisition only; extracted 44.7GB members require separate exact archive/member-manifest verification',
        'release_announcement':acquisition.RELEASE_URL,'third_party_terms':'Official 2410 release contains mixed third-party scores and phenotype data; acquisition does not grant usage rights',
        'targets':targets,'buildspec_rule':'Embedded buildspec.yml bytes equal buildspec.json; source version and full ZIP SHA pinned in separately issued admission'}
    prefix='operations/checkpoints/migration/'+plan['plan_id']+'/'
    permissions={'schema':1,'writes_performed':False,'bucket':plan['bucket'],'role':acquisition.BUILD_ROLE,
        'exact_archive_keys':[obj['key'] for obj in objects],
        'archive_actions':['s3:GetObject','s3:GetObjectVersion','s3:PutObject','s3:AbortMultipartUpload','s3:ListMultipartUploadParts'],
        'exact_checkpoint_keys':[prefix+'exact-exomiser-worker/'+obj['sha256']+'.json' for obj in objects],
        'checkpoint_actions':['s3:GetObject','s3:GetObjectVersion','s3:PutObject'],
        'ordinary_receipt_keys':[prefix+'uploaded/'+obj['sha256']+'.json' for obj in objects],
        'receipt_actions':['s3:GetObject','s3:PutObject'],
        'new_claim_key_template':prefix+'exact-exomiser-claims/<new_exact_reservation_id>.json',
        'claim_actions':['s3:GetObject','s3:GetObjectVersion','s3:PutObject'],
        'budget_safety_read':'s3:GetLifecycleConfiguration on the exact bucket; a matching enabled multipart-abort rule of at most seven days is mandatory',
        'list_bucket':'Only exact archive/checkpoint/ordinary-receipt current-key prefixes; no bucket-wide listing',
        'version_pinned_reads':['exact immutable source ZIP','content-addressed exact-exomiser admission receipt',registration['key']],
        'identity_read':{'Action':'codebuild:BatchGetBuilds','Resource':'arn:aws:codebuild:us-east-1:509505638328:build/openngs-demo-build:*'},
        'kms_actions':['kms:Decrypt','kms:GenerateDataKey'],'kms_constraint':'Exact original plan KMS key through s3.us-east-1.amazonaws.com',
        'excluded_actions':['codebuild:StartBuild','s3:DeleteObject','s3:DeleteObjectVersion','database writes','rights or Ready publication']}
    output=Path(output);output.mkdir(mode=0o700,parents=True,exist_ok=False)
    for name,content in {'source.zip':source,'source-file-manifest.json':manifest_raw,'buildspec.json':spec,
                         'package.json':encode(package),'permission-needs.json':encode(permissions)}.items():
        fd=os.open(output/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(content)
    return package


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',default='.state/migration');parser.add_argument('--output',default='.state/exomiser-cloud-acquisition')
    parser.add_argument('--offline-source',default='.state/vep-cloud-acquisition-successor/source.zip');args=parser.parse_args()
    package=prepare(Path(args.directory),Path(args.output),Path(args.offline_source))
    print(json.dumps({key:package[key] for key in ['source_zip_sha256','source_zip_bytes','entrypoint_sha256',
        'wrapper_sha256','source_file_manifest_sha256','maximum_attempt_pretax_usd','maximum_build_minutes']}))


if __name__=='__main__':main()
