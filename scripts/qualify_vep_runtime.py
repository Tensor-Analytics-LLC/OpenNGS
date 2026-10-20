"""Execute actual VEP on the bounded original 115 cache and spliced controls."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from backend import vep_tools
from backend.vep_runtime import ASSEMBLY, CACHE_INFO_SHA256, CACHE_VERSION, UPSTREAM_IMAGE, VERSION


def run(argv, log):
    with log.open('wb') as stream:
        result = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT, env=vep_tools.native_environment(), timeout=120)
    return result.returncode


def evidence(path):
    return {'name': path.name, 'bytes': path.stat().st_size, 'sha256': vep_tools.digest(path)}


def indexed_output(plain, query_contig):
    compressed = plain.with_name(plain.name + '.gz')
    with compressed.open('wb') as stream:
        subprocess.run(['bgzip', '-c', str(plain)], stdout=stream, env=vep_tools.native_environment(), check=True)
    subprocess.run(['tabix', '-p', 'vcf', str(compressed)], env=vep_tools.native_environment(), check=True)
    rows = subprocess.check_output(['tabix', str(compressed), query_contig], env=vep_tools.native_environment()).decode().splitlines()
    return [evidence(compressed), evidence(Path(str(compressed) + '.tbi'))], len(rows)


def main():
    output = Path(sys.argv[1]);work = output.parent;work.mkdir(parents=True, exist_ok=True)
    fixtures = Path('/inputs/fixtures')
    manifest = json.loads(Path('/opt/studio/vep-fixture-manifest.json').read_bytes())
    for row in manifest['files']:
        path = fixtures / row['path']
        if path.stat().st_size != row['bytes'] or vep_tools.digest(path) != row['sha256']:
            raise ValueError('Qualification fixture identity differs')
    version = vep_tools.runtime_version(work / 'version.log')
    packages = vep_tools.worker_package_versions('/opt/studio/vep-worker-apt.lock', '/opt/studio/vep-worker-packages.lock')
    cases = []
    positive = work / 'human.vcf';log = work / 'human.log'
    vep_tools.cache_info(fixtures / 'cache')
    if run(vep_tools.command(fixtures / 'cache-control.vcf', positive, cache_root=fixtures / 'cache'), log):
        raise ValueError('Native VEP failed the unchanged original release-115 cache control')
    human = vep_tools.annotation_summary(positive)
    if human['annotated_records'] != 10 or 'MRPL39' not in human['gene_symbols']:
        raise ValueError('VEP did not produce the expected human gene annotations')
    if not {'synonymous_variant', 'intron_variant'}.issubset(human['consequences']):
        raise ValueError('VEP human control lacks distinct coding and intronic consequences')
    files, count = indexed_output(positive, '21')
    if count != 10:
        raise ValueError('Actual human VEP tabix query differs from all annotated records')
    cases.append({'id': 'release-115-cache', 'status': 'passed', 'assertions': {
        **{key: human[key] for key in ['annotated_records', 'gene_symbols', 'consequences']},
        'indexed_records': count, 'cache_info_sha256': CACHE_INFO_SHA256,
        'cache_scope': 'Unchanged original 115 cache chr21:25000001-26000000 only'}, 'outputs': files})
    gff = work / 'control.gff3.gz'
    with gff.open('wb') as stream:
        subprocess.run(['bgzip', '-c', str(fixtures / 'control.gff3')], stdout=stream, check=True, env=vep_tools.native_environment())
    subprocess.run(['tabix', '-p', 'gff', str(gff)], check=True, env=vep_tools.native_environment())
    if vep_tools.validate_reference(fixtures / 'gff-control.vcf', fixtures / 'control.fa', fixtures / 'control.fa.fai') != 3:
        raise ValueError('Independent control REF alleles are invalid')
    positive = work / 'gff.vcf';log = work / 'gff.log'
    if run(vep_tools.command(fixtures / 'gff-control.vcf', positive, fasta=fixtures / 'control.fa', gff=gff), log):
        raise ValueError('Native VEP failed the independent spliced GFF control')
    if 'Setting offline mode.' not in log.read_text():
        raise ValueError('Native GFF control did not attest automatic offline mode')
    result = vep_tools.annotation_summary(positive)
    expected = manifest['gff_expected']
    for name, consequence in expected.items():
        matching = [record for record in result['records'] if record['id'] == name]
        if len(matching) != 1 or not any(consequence in row['Consequence'].split('&') and row.get('SYMBOL') == 'NGS_CONTROL' for row in matching[0]['annotations']):
            raise ValueError('VEP disagrees with the independently known spliced coding consequence')
    files, count = indexed_output(positive, 'CONTROL')
    cases.append({'id': 'spliced-gff-control', 'status': 'passed', 'assertions': {
        'annotated_records': result['annotated_records'], 'gene_symbols': result['gene_symbols'], 'indexed_records': count,
        'expected_consequences': expected, 'automatic_offline': True, 'synthetic': True}, 'outputs': files})
    for name, root, assembly, release in [('wrong-cache-version', fixtures / 'cache', ASSEMBLY, 114),
                                         ('wrong-assembly', fixtures / 'cache', 'GRCh37', CACHE_VERSION),
                                         ('missing-cache', work / 'missing-cache', ASSEMBLY, CACHE_VERSION)]:
        code = run(vep_tools.command(fixtures / 'cache-control.vcf', work / (name + '.vcf'),
                                    cache_root=root, assembly=assembly, cache_version=release), work / (name + '.log'))
        if code == 0:
            raise ValueError('Native VEP accepted an incompatible cache control')
        cases.append({'id': name, 'status': 'passed', 'assertions': {'nonzero_exit': True, 'exit_code': code}})
    readonly = bool(os.statvfs('/').f_flag & os.ST_RDONLY)
    input_readonly = bool(os.statvfs('/inputs/fixtures').f_flag & os.ST_RDONLY)
    if not readonly or not input_readonly:
        raise ValueError('Qualification must execute with read-only root and input mount')
    # Networking is disabled by the operator's docker invocation. Record and
    # verify the interface state so a successful output cannot masquerade as an
    # offline control that called the Ensembl service.
    interfaces = set(os.listdir('/sys/class/net'))
    if interfaces != {'lo'}:
        raise ValueError('Qualification requires the no-network runtime profile')
    proof = {'family': 'ensembl-vep-offline-v1', 'version': VERSION, 'version_stdout': version.strip(),
             'upstream_image': UPSTREAM_IMAGE, 'cache_version': CACHE_VERSION, 'assembly': ASSEMBLY,
             'network': 'none', 'readonly_root': readonly, 'input_mount_readonly': input_readonly,
             'worker_python_version': sys.version.split()[0], 'worker_package_versions': packages,
             'worker_apt_lock_sha256': vep_tools.digest('/opt/studio/vep-worker-apt.lock'),
             'worker_installed_packages_sha256': vep_tools.digest('/opt/studio/vep-worker-packages.lock'),
             'cases': cases, 'scope': 'Engineering software integration only; bounded original cache and synthetic spliced control, not clinical validation or full-cache readiness'}
    output.write_text(json.dumps(proof, indent=2))
    print(json.dumps({'status': 'passed', 'cases': len(cases), 'human_records': 10, 'synthetic_records': 3}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        Path(sys.argv[1]).write_text(json.dumps({'status': 'failed', 'exception': type(error).__name__,
                                               'error': str(error), 'upstream_image': UPSTREAM_IMAGE,
                                               'version': VERSION}, indent=2))
        raise
