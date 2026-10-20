"""AWS-host-only acquisition of one exact original VEP archive; dry-run by default.

No genomic files are created locally. The reviewed HTTPS source is streamed in
bounded conditional ranges into a private multipart upload. Every original byte
is hashed before conditional completion. Ordinary migration discovery/verifier
owns Ready status. This script never edits its transfer ledger or source files.
"""
import argparse
import base64
from collections import deque
import concurrent.futures
import contextlib
import fcntl
import hashlib
import json
import os
from http.client import IncompleteRead
from pathlib import Path
import re
import ssl
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler, HTTPSHandler, HTTPRedirectHandler

from botocore.config import Config
from botocore.exceptions import ClientError, ConnectionClosedError, EndpointConnectionError, ReadTimeoutError
from backend.config import aws_session
from backend.vep_runtime import CACHE_ARCHIVE_NAME as ARCHIVE_NAME, CACHE_ARCHIVE_SHA256 as ARCHIVE_SHA256, CACHE_ARCHIVE_BYTES as ARCHIVE_BYTES
from scripts import prioritize_migration_objects as migration

SOURCE = 'https://ftp.ensembl.org/pub/release-115/variation/indexed_vep_cache/' + ARCHIVE_NAME
SOURCE_ETAG = '"5e262bb19-638db41fb105a"'
SOURCE_MODIFIED = 'Tue, 01 Jul 2025 10:03:36 GMT'
PART_BYTES = 8 * 1024**2
PREFETCH_PARTS = 8
UPLOAD_PARTS = 2
MAX_SECONDS = 3600
MIN_RESERVATION_MICRO = 100000
BUILD_ACCOUNT = '509505638328'
BUILD_REGION = 'us-east-1'
BUILD_PROJECT = 'openngs-demo-build'
BUILD_PROJECT_ARN = 'arn:aws:codebuild:us-east-1:509505638328:project/' + BUILD_PROJECT
BUILD_ROLE = 'arn:aws:iam::509505638328:role/openngs-demo-compute-BuildRole-34zaGWrYCcXD'
ISSUER_HOST = 'i-05d8d1ca1bc1c87a6'
CODEBUILD_MAX_SECONDS = 45 * 60
CODEBUILD_MAX_COST = .70


def verify_cloud_runtime(release):
    # Keep CodeBuild acquisition independent of the application database and
    # public-reference publisher; only the trusted app issuer imports them.
    from scripts.acquire_references import verify_cloud_runtime as verify_ec2
    return verify_ec2(release)


def target_object(plan):
    if (plan.get('plan_id'), plan.get('owner_id'), plan.get('bucket')) != (migration.APPROVED_PLAN, migration.APPROVED_OWNER, migration.APPROVED_BUCKET):
        raise ValueError('Only the exact registered original migration is permitted')
    matches = [obj for obj in plan.get('objects', []) if obj.get('sha256') == ARCHIVE_SHA256]
    if len(matches) != 1:
        raise ValueError('The registered plan must identify the exact original archive once')
    obj = matches[0]
    dataset = 'ds_migration_' + hashlib.sha256((migration.APPROVED_OWNER + ':' + ARCHIVE_SHA256).encode()).hexdigest()[:40]
    expected_key = f'tenants/{migration.APPROVED_OWNER}/datasets/{dataset}/{ARCHIVE_NAME}'
    if (obj.get('name'), obj.get('bytes'), obj.get('dataset_id'), obj.get('key')) != (ARCHIVE_NAME, ARCHIVE_BYTES, dataset, expected_key):
        raise ValueError('Original VEP archive name, length, dataset and owner key must agree')
    aliases = obj.get('aliases', [])
    if not aliases or any((row.get('name'), row.get('user_id'), row.get('bytes')) != (ARCHIVE_NAME, migration.APPROVED_OWNER, ARCHIVE_BYTES) for row in aliases):
        raise ValueError('Every original archive alias must retain its exact inventoried identity')
    return obj


def load_local_plan(directory):
    registration = json.loads((directory / 'registration.json').read_bytes())
    required = {'plan_id': migration.APPROVED_PLAN, 'owner_id': migration.APPROVED_OWNER,
                'bucket': migration.APPROVED_BUCKET, 'sha256': migration.APPROVED_PLAN_SHA256,
                'key': 'operations/checkpoints/migration/' + migration.APPROVED_PLAN + '/plan.json',
                'version_id': migration.APPROVED_PLAN_VERSION}
    if registration != required:
        raise ValueError('Acquisition requires the exact registered immutable plan')
    raw = (directory / 'plan.json').read_bytes()
    if len(raw) > migration.MAX_MANIFEST or hashlib.sha256(raw).hexdigest() != migration.APPROVED_PLAN_SHA256:
        raise ValueError('The local migration metadata differs from its registered full SHA-256')
    plan = json.loads(raw)
    if sum(row['bytes'] for row in plan['objects']) != plan['total_bytes']:
        raise ValueError('The original migration storage envelope differs')
    return plan, target_object(plan), registration, len(raw)


def verify_admission(reservation, plan):
    """Read an existing shared-platform hold; never creates or increases funding."""
    from backend import db
    if not re.fullmatch(r'cost_[a-f0-9]{32}', reservation or ''):
        raise ValueError('Execution requires an existing shared-platform cost reservation')
    row = db.query("SELECT r.state,r.reserved_micro,a.account,u.role,u.frozen FROM cost_reservations r JOIN cost_allocations a ON a.reservation_id=r.id JOIN users u ON u.id=r.user_id WHERE r.id=?", (reservation,), one=True)
    if (not row or row['state'] != 'reserved' or row['account'] != 'shared-platform'
            or row['role'] != 'admin' or row['frozen'] or not MIN_RESERVATION_MICRO <= row['reserved_micro'] <= 2000000):
        raise ValueError('The reviewed active shared acquisition reservation is required')
    storage = db.query('SELECT user_id,plan_sha256,bytes FROM baseline_storage_allocations WHERE plan_id=?', (plan['plan_id'],), one=True)
    if not storage or (storage['user_id'], storage['plan_sha256'], storage['bytes']) != (plan['owner_id'], migration.APPROVED_PLAN_SHA256, plan['total_bytes']):
        raise ValueError('The exact original archive must already fit its funded migration storage envelope')
    return {'reservation': reservation, 'maximum_reserved_pretax_usd': row['reserved_micro'] / 1000000,
            'existing_storage_envelope': True}


def source_identity(value):
    if (not isinstance(value,dict) or value.get('bucket')!=migration.APPROVED_BUCKET
            or not value.get('key','').startswith('operations/builds/') or not value['key'].endswith('/source.zip')
            or value.get('version_id') in [None,'','null'] or not isinstance(value.get('version_id'),str)
            or type(value.get('bytes')) is not int or not 0<value['bytes']<=64*1024**2
            or any(not re.fullmatch('[a-f0-9]{64}',value.get(key,'')) for key in ['sha256','entrypoint_sha256','buildspec_sha256'])):
        raise ValueError('CodeBuild requires exact immutable reviewed source zip, entrypoint and buildspec identities')
    return value


def make_codebuild_admission(plan,obj,reservation,source):
    """Call only on the deployed app host after the operator stages exact metadata.

    Returns small authorization bytes for an operator's conditional KMS/S3 PUT.
    This function reads the real database hold/storage allocation and IMDSv2; it
    never uploads the receipt, starts a build or alters funding.
    """
    target_object(plan);source_identity(source)
    host=verify_cloud_runtime({'bucket':plan['bucket'],'kms':plan['kms']})
    if host['instance_id']!=ISSUER_HOST:
        raise ValueError('Only the deployed application host may attest its actual database admission')
    admission=verify_admission(reservation,plan)
    if admission['maximum_reserved_pretax_usd']<CODEBUILD_MAX_COST:
        raise ValueError('The existing shared hold cannot cover bounded CodeBuild acquisition')
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=source['entrypoint_sha256']:
        raise ValueError('Admission issuer and CodeBuild source must use the same reviewed acquisition script')
    now=int(time.time())
    return {'schema':1,'purpose':'exact-vep-archive-codebuild-acquisition','issued_at':now,'expires_at':now+3600,
        'issuer':host,'account':BUILD_ACCOUNT,'region':BUILD_REGION,'project_arn':BUILD_PROJECT_ARN,'service_role':BUILD_ROLE,
        'plan_id':plan['plan_id'],'plan_sha256':migration.APPROVED_PLAN_SHA256,'plan_version_id':migration.APPROVED_PLAN_VERSION,
        'owner_id':plan['owner_id'],'reservation':reservation,'admission':admission,
        'maximum_cost_pretax_usd':CODEBUILD_MAX_COST,'maximum_seconds':CODEBUILD_MAX_SECONDS,
        'target':{'bucket':plan['bucket'],'key':obj['key'],'sha256':obj['sha256'],'bytes':obj['bytes'],'dataset_id':obj['dataset_id']},
        'provider':{'url':SOURCE,'etag':SOURCE_ETAG,'last_modified':SOURCE_MODIFIED,'bytes':ARCHIVE_BYTES,'sha256':ARCHIVE_SHA256},
        'source':source}


def verify_codebuild_admission(session,plan,obj,uri,version,digest,*,environment=None,now=None):
    """Attest an explicit non-VPC CodeBuild route; never infer AWS from env alone."""
    env=os.environ if environment is None else environment
    build_arn=env.get('CODEBUILD_BUILD_ARN','');build_id=env.get('CODEBUILD_BUILD_ID','')
    prefix=f'arn:aws:codebuild:{BUILD_REGION}:{BUILD_ACCOUNT}:build/'
    if (not re.fullmatch(re.escape(prefix+BUILD_PROJECT+':')+r'[a-f0-9-]{36}',build_arn)
            or build_id!=build_arn.removeprefix(prefix)
            or env.get('AWS_REGION',env.get('AWS_DEFAULT_REGION'))!=BUILD_REGION):
        raise ValueError('CodeBuild requires its exact reviewed project, build ARN and region')
    identity=session.client('sts').get_caller_identity()
    assumed_role=BUILD_ROLE.rsplit('/',1)[1]
    if identity.get('Account')!=BUILD_ACCOUNT or not re.fullmatch(
            rf'arn:aws:sts::{BUILD_ACCOUNT}:assumed-role/{re.escape(assumed_role)}/[^/]+',identity.get('Arn','')):
        raise ValueError('CodeBuild caller must be the exact reviewed project service role')
    builds=session.client('codebuild').batch_get_builds(ids=[build_id]).get('builds',[])
    if len(builds)!=1:raise ValueError('CodeBuild must describe exactly its current authenticated build')
    build=builds[0];compute=build.get('environment',{})
    if (build.get('arn')!=build_arn or build.get('id')!=build_id or build.get('projectName')!=BUILD_PROJECT
            or build.get('serviceRole')!=BUILD_ROLE or build.get('buildStatus')!='IN_PROGRESS'
            or type(build.get('timeoutInMinutes')) is not int or not 1<=build['timeoutInMinutes']<=45
            or compute.get('computeType')!='BUILD_GENERAL1_MEDIUM' or compute.get('privilegedMode') is not False
            or compute.get('type')!='LINUX_CONTAINER' or compute.get('image')!='aws/codebuild/standard:7.0'
            or build.get('vpcConfig')):
        raise ValueError('Current CodeBuild identity, public managed environment or 45-minute MEDIUM cost bound differs')
    if not re.fullmatch('[a-f0-9]{64}',digest or '') or version in [None,'','null']:
        raise ValueError('An immutable app-issued admission receipt version and SHA-256 are required')
    receipt_key=f'operations/checkpoints/migration/{migration.APPROVED_PLAN}/admissions/exact-vep/{digest}.json'
    if uri!='s3://'+plan['bucket']+'/'+receipt_key:
        raise ValueError('CodeBuild admission must use the exact content-addressed private migration receipt key')
    client=session.client('s3')
    response=client.get_object(Bucket=plan['bucket'],Key=receipt_key,VersionId=version)
    if response.get('VersionId')!=version or response.get('ServerSideEncryption')!='aws:kms' or response.get('SSEKMSKeyId')!=plan['kms']:
        response['Body'].close()
        raise ValueError('CodeBuild admission receipt must retain its exact private KMS/S3 version')
    raw=migration.bounded_read(response,32768)
    if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('CodeBuild admission bytes differ from their authorized SHA-256')
    receipt=json.loads(raw);clock=time.time() if now is None else now
    if (receipt.get('schema')!=1 or receipt.get('purpose')!='exact-vep-archive-codebuild-acquisition'
            or type(receipt.get('issued_at')) is not int or type(receipt.get('expires_at')) is not int
            or not receipt['issued_at']-30<=clock<receipt['expires_at']<=receipt['issued_at']+3600
            or receipt.get('issuer')!={'instance_id':ISSUER_HOST,'account':BUILD_ACCOUNT,'region':BUILD_REGION}
            or receipt.get('account')!=BUILD_ACCOUNT or receipt.get('region')!=BUILD_REGION
            or receipt.get('project_arn')!=BUILD_PROJECT_ARN or receipt.get('service_role')!=BUILD_ROLE
            or receipt.get('plan_id')!=plan['plan_id'] or receipt.get('plan_sha256')!=migration.APPROVED_PLAN_SHA256
            or receipt.get('plan_version_id')!=migration.APPROVED_PLAN_VERSION or receipt.get('owner_id')!=plan['owner_id']
            or receipt.get('maximum_cost_pretax_usd')!=CODEBUILD_MAX_COST or receipt.get('maximum_seconds')!=CODEBUILD_MAX_SECONDS
            or receipt.get('target')!={'bucket':plan['bucket'],'key':obj['key'],'sha256':obj['sha256'],'bytes':obj['bytes'],'dataset_id':obj['dataset_id']}
            or receipt.get('provider')!={'url':SOURCE,'etag':SOURCE_ETAG,'last_modified':SOURCE_MODIFIED,'bytes':ARCHIVE_BYTES,'sha256':ARCHIVE_SHA256}):
        raise ValueError('App-issued CodeBuild admission is expired or differs from the exact reviewed acquisition')
    admission=receipt.get('admission',{});reservation=receipt.get('reservation')
    if (not re.fullmatch(r'cost_[a-f0-9]{32}',reservation or '') or admission.get('reservation')!=reservation
            or admission.get('existing_storage_envelope') is not True
            or type(admission.get('maximum_reserved_pretax_usd')) not in [int,float]
            or not CODEBUILD_MAX_COST<=admission['maximum_reserved_pretax_usd']<=2):
        raise ValueError('CodeBuild admission requires the real current shared hold and existing storage envelope')
    source=source_identity(receipt.get('source'));observed_source=build.get('source',{})
    if (observed_source.get('type')!='S3' or observed_source.get('location')!=source['bucket']+'/'+source['key']
            or observed_source.get('insecureSsl') not in [None,False] or build.get('sourceVersion')!=source['version_id']
            or hashlib.sha256(observed_source.get('buildspec','').encode()).hexdigest()!=source['buildspec_sha256']
            or hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=source['entrypoint_sha256']):
        raise ValueError('CodeBuild source version, exact buildspec or executed script differs from admission')
    head=client.head_object(Bucket=source['bucket'],Key=source['key'],VersionId=source['version_id'],ChecksumMode='ENABLED')
    if (head.get('VersionId')!=source['version_id'] or head.get('ContentLength')!=source['bytes']
            or head.get('ChecksumType','FULL_OBJECT')!='FULL_OBJECT'
            or head.get('ChecksumSHA256')!=base64.b64encode(bytes.fromhex(source['sha256'])).decode()
            or head.get('ServerSideEncryption')!='aws:kms' or head.get('SSEKMSKeyId')!=plan['kms']):
        raise ValueError('CodeBuild source archive lacks its exact immutable full-object checksum and encryption')
    return {'build_arn':build_arn,'account':BUILD_ACCOUNT,'region':BUILD_REGION,'receipt_version_id':version,
            'receipt_sha256':digest,'expires_at':receipt['expires_at'],'reservation':reservation}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise ValueError('The exact reviewed Ensembl HTTPS source must not redirect')


class Provider:
    def __init__(self):
        self.opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
        self.cancelled=threading.Event();self.response_lock=threading.Lock();self.responses=set()

    def _check_cancelled(self):
        if getattr(self,'cancelled',None) is not None and self.cancelled.is_set():
            raise TimeoutError('Conditional source reads were cancelled; no further range requests are permitted')

    def cancel(self):
        self.cancelled.set()
        with self.response_lock:responses=list(self.responses)
        for response in responses:
            try:
                sock=getattr(getattr(getattr(response,'fp',None),'raw',None),'_sock',None)
                if sock is not None:sock.shutdown(socket.SHUT_RDWR)
                response.close()
            except OSError:pass

    def _track(self,response,active):
        if getattr(self,'response_lock',None) is not None:
            with self.response_lock:
                if active:self.responses.add(response)
                else:self.responses.discard(response)

    @staticmethod
    def _transient(error):
        if isinstance(error,HTTPError):return error.code in [408,429,500,502,503,504]
        reason=error.reason if isinstance(error,URLError) else error
        if isinstance(reason,ssl.SSLCertVerificationError):return False
        return isinstance(reason,(TimeoutError,ConnectionError,IncompleteRead,OSError))

    def _open(self, method, extra=None):
        return self.opener.open(Request(SOURCE, method=method, headers={
            'Accept-Encoding': 'identity', 'If-Match': SOURCE_ETAG,
            'If-Unmodified-Since': SOURCE_MODIFIED, **(extra or {})}), timeout=30)

    @staticmethod
    def _headers(response):
        if (response.headers.get('ETag') != SOURCE_ETAG or response.headers.get('Last-Modified') != SOURCE_MODIFIED
                or response.headers.get('Content-Encoding') not in [None, 'identity']):
            raise ValueError('Reviewed Ensembl source identity or transfer encoding changed')

    def head(self):
        with self._open('HEAD') as response:
            self._headers(response)
            if response.status != 200 or response.headers.get('Content-Length') != str(ARCHIVE_BYTES) or response.headers.get('Accept-Ranges') != 'bytes':
                raise ValueError('Reviewed Ensembl archive length or range support changed')
        return {'url': SOURCE, 'etag': SOURCE_ETAG, 'last_modified': SOURCE_MODIFIED, 'bytes': ARCHIVE_BYTES}

    def read_range(self, offset, length):
        for attempt in range(3):
            self._check_cancelled()
            try:
                with self._open('GET', {'Range': f'bytes={offset}-{offset + length - 1}'}) as response:
                    self._track(response,True)
                    try:
                        self._check_cancelled();self._headers(response)
                        if (response.status != 206 or response.headers.get('Content-Length') != str(length)
                                or response.headers.get('Content-Range') != f'bytes {offset}-{offset + length - 1}/{ARCHIVE_BYTES}'):
                            raise ValueError('Ensembl must return exactly the requested bounded original range')
                        body = response.read(length + 1)
                        self._check_cancelled()
                        if len(body) != length:
                            raise ValueError('Ensembl range was truncated or exceeded its admitted length')
                        return body
                    finally:self._track(response,False)
            except (HTTPError,URLError,TimeoutError,ConnectionError,IncompleteRead,OSError) as error:
                if isinstance(error,HTTPError):error.close()
                self._check_cancelled()
                if attempt==2 or not self._transient(error):raise
                cancelled=getattr(self,'cancelled',None)
                if cancelled is not None:cancelled.wait(.25*(attempt+1))


def ordered_ranges(provider,size,within_admission,remaining_seconds,*,retained_parts=lambda:0,on_failure=lambda error:None):
    """At most eight retained range buffers; only the next part is yielded.

    Callers release their yielded bytes before advancing. A caller retaining
    upload bodies reports their count; those slots are withheld from refills.
    Futures are submitted incrementally and source retries retain all original
    range and identity preconditions.
    """
    count=(size+PART_BYTES-1)//PART_BYTES
    pool=concurrent.futures.ThreadPoolExecutor(max_workers=PREFETCH_PARTS)
    pending={};stopped=threading.Event();lock=threading.Lock();failure=[];cancelled=False;finished=False
    def stop(error=None):
        nonlocal cancelled
        with lock:
            first_failure=error is not None and not failure
            if first_failure:failure.append(error)
            stopped.set();should_cancel=not cancelled;cancelled=True
        if first_failure:on_failure(error)
        if should_cancel and hasattr(provider,'cancel'):provider.cancel()
    class RangeResult:
        def __init__(self,raw):self.raw=raw
    def read(number):
        if stopped.is_set():raise TimeoutError('Range prefetch stopped before another source request')
        within_admission();offset=(number-1)*PART_BYTES
        return RangeResult(provider.read_range(offset,min(PART_BYTES,size-offset)))
    def completed(future):
        if not future.cancelled():
            error=future.exception()
            if error is not None:stop(error)
    def submit(number):
        within_admission()
        if failure:raise failure[0]
        future=pool.submit(read,number);pending[number]=future;future.add_done_callback(completed)
    try:
        initial=min(PREFETCH_PARTS,count)
        for number in range(1,initial+1):submit(number)
        next_number=initial+1
        for number in range(1,count+1):
            within_admission()
            if failure:raise failure[0]
            future=pending.pop(number)
            try:result=future.result(timeout=max(.001,remaining_seconds()))
            except BaseException:
                if failure:raise failure[0]
                raise
            # A worker can retain its returned Future result while callbacks
            # finish. Empty this envelope before transferring ownership so the
            # executor cannot retain a ninth raw body after a refill.
            raw=result.raw;result.raw=None;del result
            within_admission()
            if failure:raise failure[0]
            yield number,raw
            # Release both the generator and Future references before another
            # source request is submitted, preserving the eight-buffer bound.
            del raw,future
            retained=retained_parts()
            if type(retained) is not int or not 0<=retained<PREFETCH_PARTS:
                raise ValueError('Retained upload bodies must share the bounded source buffer window')
            while next_number<=count and len(pending)<PREFETCH_PARTS-retained:
                submit(next_number);next_number+=1
        finished=True
    finally:
        if not finished:
            stop()
            for future in pending.values():future.cancel()
        pool.shutdown(wait=finished,cancel_futures=True)


@contextlib.contextmanager
def checkpoint(directory, plan, obj, reservation):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / ('exact-vep-' + obj['sha256'] + '.json')
    with path.with_suffix('.lock').open('a+b') as lock:
        os.chmod(path.with_suffix('.lock'), 0o600)
        try:fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('Another exact VEP acquisition owns this private checkpoint') from None
        binding = {'schema': 1, 'plan_id': plan['plan_id'], 'plan_sha256': migration.APPROVED_PLAN_SHA256,
                   'owner_id': plan['owner_id'], 'bucket': plan['bucket'], 'key': obj['key'], 'sha256': obj['sha256'],
                   'bytes': obj['bytes'], 'source': SOURCE, 'etag': SOURCE_ETAG, 'last_modified': SOURCE_MODIFIED,
                   'part_bytes': PART_BYTES, 'reservation': reservation}
        value = json.loads(path.read_bytes()) if path.exists() else {**binding, 'state': 'pending', 'parts': {}}
        if any(value.get(key) != expected for key, expected in binding.items()):
            raise ValueError('Private acquisition checkpoint differs from its exact original source and reservation')
        def save(**changes):
            value.update(changes)
            temporary = path.with_suffix('.new')
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'wb') as output:
                output.write(migration.encode(value));output.flush();os.fsync(output.fileno())
            temporary.replace(path)
        try:yield value, save
        finally:fcntl.flock(lock, fcntl.LOCK_UN)


def acquire_archive(client, plan, obj, provider, directory, reservation, *, wall_seconds=MAX_SECONDS, expires_at=None):
    if not 1 <= wall_seconds <= MAX_SECONDS:
        raise ValueError('Acquisition wall time must remain within the reviewed one-hour bound')
    started = time.monotonic();upload_failures=[];upload_stopped=threading.Event();upload_lock=threading.Lock()
    def within_deadline():
        if time.monotonic()-started>wall_seconds or expires_at is not None and time.time()>=expires_at:
            raise TimeoutError('Acquisition reached its admitted wall time or receipt expiry; private upload retained for safe resume')
    def within_admission():
        if upload_failures:raise upload_failures[0]
        within_deadline()
    def remaining_seconds():
        remaining=wall_seconds-(time.monotonic()-started)
        return min(remaining,expires_at-time.time()) if expires_at is not None else remaining
    with checkpoint(directory, plan, obj, reservation) as (state, save):
        within_admission()
        existing = migration.head_existing(client, plan, obj)
        if existing:
            within_admission()
            migration.publish_receipt(client, plan, obj, existing)
            return {'state': 'Existing exact immutable object recovered', 'version_id': existing['VersionId'], 'provider_bytes': 0}
        if state['state'] in ['creating', 'completing', 'completed', 'failed-sha256']:
            raise ValueError('An ambiguous mutation or checksum failure requires operator reconciliation; no mutation repeated')
        provider.head()
        upload = state.get('upload_id')
        if not upload:
            within_admission()
            save(state='creating')
            response = client.create_multipart_upload(Bucket=plan['bucket'], Key=obj['key'], ChecksumAlgorithm='SHA256',
                ServerSideEncryption='aws:kms', SSEKMSKeyId=plan['kms'], BucketKeyEnabled=True,
                Metadata=migration.metadata(plan, obj))
            upload = response['UploadId'];save(state='uploading', upload_id=upload)
        known = {};request = {'Bucket': plan['bucket'], 'Key': obj['key'], 'UploadId': upload}
        while True:
            page = client.list_parts(**request)
            known.update({part['PartNumber']: part for part in page.get('Parts', [])})
            if not page.get('IsTruncated'):break
            request['PartNumberMarker'] = page['NextPartNumberMarker']
        count = (obj['bytes'] + PART_BYTES - 1) // PART_BYTES
        if any(number < 1 or number > count for number in known):
            raise ValueError('Private acquisition upload has unexpected part numbers')
        digest = hashlib.sha256();total = 0;parts = []
        # Resumption re-reads the uploaded prefix from the pinned provider. This
        # rebuilds a real whole-object SHA-256 rather than serializing hash state
        # or confusing a composite multipart checksum with the original digest.
        pending_uploads=deque();pool=concurrent.futures.ThreadPoolExecutor(max_workers=UPLOAD_PARTS)
        class Upload:
            # Submit this callable without a raw-byte argument. Clear the owned
            # body before Future completion; executor WorkItem arguments must
            # not retain a ninth payload after the coordinator frees a slot.
            def __init__(self,number,raw,checksum):
                self.number=number;self.raw=raw;self.checksum=checksum
            def __call__(self):
                try:
                    for attempt in range(3):
                        within_admission()
                        if upload_stopped.is_set():raise TimeoutError('Private part uploads were cancelled')
                        try:
                            return client.upload_part(Bucket=plan['bucket'],Key=obj['key'],UploadId=upload,
                                PartNumber=self.number,Body=self.raw,ChecksumSHA256=self.checksum)
                        except (ConnectionClosedError,EndpointConnectionError,ReadTimeoutError):
                            # Only identical bytes in the same private UploadId
                            # are retryable. Check the admission before each try.
                            if attempt==2:raise
                finally:self.raw=None
        def stop_uploads(error):
            with upload_lock:
                if not upload_failures:upload_failures.append(error)
            upload_stopped.set()
            if hasattr(provider,'cancel'):provider.cancel()
        def failed(future):
            if not future.cancelled() and future.exception() is not None:stop_uploads(future.exception())
        def acknowledge(*,reconcile=False):
            if not reconcile:within_deadline()
            number,length,checksum,provider_bytes,future,response=pending_uploads[0]
            if future is not None:response=future.result(timeout=max(.001,remaining_seconds()))
            if not reconcile:within_deadline()
            if response.get('ChecksumSHA256')!=checksum or not response.get('ETag'):
                raise ValueError('S3 did not attest the exact uploaded part SHA-256 and ETag')
            part={'PartNumber':number,'ETag':response['ETag'],'ChecksumSHA256':checksum}
            parts.append(part)
            save(parts={**state['parts'],str(number):{**part,'bytes':length}},provider_bytes=provider_bytes)
            pending_uploads.popleft()
        try:
            with contextlib.closing(ordered_ranges(provider,obj['bytes'],within_admission,remaining_seconds,
                    retained_parts=lambda:len(pending_uploads),on_failure=stop_uploads)) as ranges:
                for number,raw in ranges:
                    within_admission()
                    if len(pending_uploads)>=UPLOAD_PARTS:acknowledge()
                    length=min(PART_BYTES,obj['bytes']-(number-1)*PART_BYTES)
                    if len(raw)!=length:raise ValueError('Provider did not return the exact admitted part length')
                    digest.update(raw);total+=length
                    checksum=base64.b64encode(hashlib.sha256(raw).digest()).decode()
                    previous=known.get(number);future=None;response=None
                    if previous and previous.get('Size')==length and previous.get('ChecksumSHA256')==checksum:
                        response=previous
                    else:
                        future=pool.submit(Upload(number,raw,checksum));future.add_done_callback(failed)
                    pending_uploads.append((number,length,checksum,total,future,response))
                    # Collect only the ordered head; later S3 acknowledgments
                    # may arrive first but cannot reorder hashes or checkpoints.
                    while pending_uploads and (pending_uploads[0][4] is None or pending_uploads[0][4].done()):
                        acknowledge()
                    del raw
            while pending_uploads:acknowledge()
        except BaseException:
            upload_stopped.set()
            for record in pending_uploads:
                if record[4] is not None:record[4].cancel()
            if hasattr(provider,'cancel'):provider.cancel()
            pool.shutdown(wait=True,cancel_futures=True)
            # Preserve the already-acknowledged ordered prefix, including a
            # preceding request that finished while a later one failed. This
            # performs bookkeeping only: no upload, retry or final commit.
            # A failed checkpoint claim stops reconciliation immediately; the
            # original error remains authoritative and ListParts recovers on
            # the next admitted attempt.
            try:
                while pending_uploads:
                    future=pending_uploads[0][4]
                    if future is not None and (future.cancelled() or not future.done() or future.exception() is not None):break
                    acknowledge(reconcile=True)
            except Exception:pass
            raise
        finally:
            pool.shutdown(wait=True,cancel_futures=True)
        if total != obj['bytes'] or digest.hexdigest() != obj['sha256']:
            save(state='failed-sha256', observed_bytes=total, observed_sha256=digest.hexdigest())
            raise ValueError('Complete Ensembl archive differs from the original inventoried SHA-256; no object committed')
        composite = base64.b64encode(hashlib.sha256(b''.join(base64.b64decode(part['ChecksumSHA256']) for part in parts)).digest()).decode() + '-' + str(len(parts))
        within_admission()
        save(state='completing', verified_original_sha256=digest.hexdigest(), composite_sha256=composite)
        try:
            response = client.complete_multipart_upload(Bucket=plan['bucket'], Key=obj['key'], UploadId=upload,
                MultipartUpload={'Parts': parts}, IfNoneMatch='*')
        except ClientError as error:
            if error.response['Error']['Code'] != 'PreconditionFailed':raise
            head = migration.head_existing(client, plan, obj)
            if not head:raise ValueError('Conditional acquisition race has no exact verifiable winning object')
            # Only this script's private upload can be aborted; normal migration
            # IDs, checkpoints and leases are never opened or modified.
            client.abort_multipart_upload(Bucket=plan['bucket'], Key=obj['key'], UploadId=upload)
        else:
            version = response.get('VersionId')
            if version in [None, '', 'null']:raise ValueError('Completed exact archive has no immutable version')
            head = client.head_object(Bucket=plan['bucket'], Key=obj['key'], VersionId=version, ChecksumMode='ENABLED')
            migration.verify_existing(head, plan, obj, composite=composite)
        save(state='completed', version_id=head['VersionId'])
        migration.publish_receipt(client, plan, obj, head)
        return {'state': 'Acquired exact original bytes; awaiting ordinary migration discovery and verification',
                'version_id': head['VersionId'], 'provider_bytes': total, 'sha256': digest.hexdigest(),
                'seconds': round(time.monotonic() - started, 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', default='.state/migration')
    parser.add_argument('--checkpoint-directory', default='.state/exact-vep-acquisition')
    parser.add_argument('--reservation')
    parser.add_argument('--wall-seconds', type=int, default=MAX_SECONDS)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true')
    mode.add_argument('--emit-admission', metavar='OUTPUT_JSON', help='On the deployed app host, attest current DB funding into a private local receipt; no S3 writes')
    parser.add_argument('--source-identity-json', help='Private small JSON containing immutable source zip identity, entrypoint SHA and buildspec SHA')
    parser.add_argument('--codebuild-admission', help='Exact private content-addressed S3 receipt URI for the explicitly reviewed CodeBuild route')
    parser.add_argument('--admission-version')
    parser.add_argument('--admission-sha256')
    args = parser.parse_args()
    plan, obj, registration, manifest_bytes = load_local_plan(Path(args.directory))
    if args.emit_admission:
        if not args.source_identity_json or args.codebuild_admission:
            raise ValueError('Receipt issuance requires only a reviewed source identity JSON and existing reservation')
        raw=Path(args.source_identity_json).read_bytes()
        if len(raw)>32768:raise ValueError('Source identity metadata exceeds its bound')
        receipt=make_codebuild_admission(plan,obj,args.reservation,json.loads(raw))
        body=migration.encode(receipt)
        descriptor=os.open(args.emit_admission,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'wb') as output:output.write(body);output.flush();os.fsync(output.fileno())
        print(json.dumps({'admission_file':args.emit_admission,'sha256':hashlib.sha256(body).hexdigest(),
                          'bytes':len(body),'expires_at':receipt['expires_at'],'aws_mutations':False}))
        return
    result = {'plan_id': plan['plan_id'], 'owner_id': plan['owner_id'], 'name': obj['name'], 'sha256': obj['sha256'],
              'bytes': obj['bytes'], 'source': SOURCE, 'etag': SOURCE_ETAG, 'last_modified': SOURCE_MODIFIED,
              'sha256_basis': 'Original immutable inventory; provider CHECKSUMS is non-cryptographic BSD sum',
              'maximum_memory_part_bytes': PART_BYTES, 'maximum_seconds': args.wall_seconds,
              'maximum_prefetched_parts':PREFETCH_PARTS,'maximum_parallel_uploads':UPLOAD_PARTS,
              'maximum_shared_raw_buffer_bytes':PART_BYTES*PREFETCH_PARTS,
              'maximum_range_buffer_bytes':PART_BYTES*PREFETCH_PARTS,
              's3_part_count': (obj['bytes'] + PART_BYTES - 1) // PART_BYTES,
              'maximum_s3_kms_request_pretax_usd': .10, 'maximum_duplicate_temporary_7_day_storage_pretax_usd': .15,
              'retained_bytes_already_in_original_storage_envelope': True,
              'aws_mutations': args.execute, 'shared_ledger_modified': False, 'source_deleted': False}
    if args.execute:
        expires_at=None;reservation=args.reservation;wall_seconds=args.wall_seconds
        if args.codebuild_admission:
            session=aws_session()
            result['host']=verify_codebuild_admission(session,plan,obj,args.codebuild_admission,args.admission_version,args.admission_sha256)
            expires_at=result['host']['expires_at']
            if reservation and reservation!=result['host']['reservation']:
                raise ValueError('Requested reservation differs from the app-issued CodeBuild admission')
            reservation=result['host']['reservation'];wall_seconds=min(wall_seconds,CODEBUILD_MAX_SECONDS)
            result.update(maximum_seconds=wall_seconds,maximum_codebuild_pretax_usd=.45,maximum_combined_acquisition_pretax_usd=CODEBUILD_MAX_COST)
        else:
            if args.admission_version or args.admission_sha256:raise ValueError('Admission version and digest require the explicit CodeBuild route')
            result['host'] = verify_cloud_runtime({'bucket': plan['bucket'], 'kms': plan['kms']})
            result['admission'] = verify_admission(reservation, plan)
            session=aws_session()
        client = session.client('s3', config=Config(connect_timeout=5, read_timeout=90, retries={'total_max_attempts': 1}))
        head = client.head_object(Bucket=registration['bucket'], Key=registration['key'], VersionId=registration['version_id'])
        if (head.get('VersionId'), head.get('ContentLength'), head.get('Metadata', {}).get('sha256')) != (registration['version_id'], manifest_bytes, registration['sha256']):
            raise ValueError('Cloud migration manifest differs from its exact registered immutable version')
        result['acquisition'] = acquire_archive(client, plan, obj, Provider(), Path(args.checkpoint_directory), reservation, wall_seconds=wall_seconds,expires_at=expires_at)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:main()
    except Exception as error:
        raise SystemExit('Exact VEP acquisition stopped (' + type(error).__name__ + '). Existing versions and original sources were preserved.')
