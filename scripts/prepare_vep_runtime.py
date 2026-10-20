"""Prepare a tiny real release-115 cache slice and deterministic VEP controls.

Original migration sources are read-only. No production cache download or AWS
write occurs. The old release-84 cache in upstream tests is not relabelled.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request


COMMIT = '2beada0d57ca6234f467b14a6c60280f4a082717'
EXAMPLE_GIT_SHA1 = '4ca33c767e357ba7dca1f7d5a4d9007f2c7dfdfc'
PLAN_SHA = '41047a533687c94988ab048ea13d7877067b564b1c636ce6c0bf76e695b20ea4'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def spliced_control():
    """Coordinate-sorted GFF and independently known two-exon coding changes."""
    sequence = list('N' * 600);coding = 'ATGAAA' + 'GCT' * 57 + 'TAA'
    sequence[99:189] = coding[:90];sequence[299:389] = coding[90:]
    return {
        'control.fa': '>CONTROL\n' + '\n'.join(''.join(sequence)[i:i+60] for i in range(0, 600, 60)) + '\n',
        'control.fa.fai': 'CONTROL\t600\t9\t60\t61\n',
        # Tabix requires nondecreasing feature starts. Keep parents first among
        # equal-start rows, then pair each exon and its CDS before the next exon.
        'control.gff3': '##gff-version 3\n'
            'CONTROL\tOpenNGS\tgene\t100\t389\t.\t+\t.\tID=gene:NGS_GENE;Name=NGS_CONTROL;biotype=protein_coding\n'
            'CONTROL\tOpenNGS\tmRNA\t100\t389\t.\t+\t.\tID=transcript:NGS_TX;Parent=gene:NGS_GENE;Name=NGS_TX;biotype=protein_coding\n'
            'CONTROL\tOpenNGS\texon\t100\t189\t.\t+\t.\tParent=transcript:NGS_TX\n'
            'CONTROL\tOpenNGS\tCDS\t100\t189\t.\t+\t0\tParent=transcript:NGS_TX\n'
            'CONTROL\tOpenNGS\texon\t300\t389\t.\t+\t.\tParent=transcript:NGS_TX\n'
            'CONTROL\tOpenNGS\tCDS\t300\t389\t.\t+\t0\tParent=transcript:NGS_TX\n',
        'gff-control.vcf': '##fileformat=VCFv4.2\n##contig=<ID=CONTROL,length=600>\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n'
            'CONTROL\t103\tstop\tA\tT\t.\tPASS\t.\nCONTROL\t106\tmissense\tG\tA\t.\tPASS\t.\nCONTROL\t108\tsynonymous\tT\tC\t.\tPASS\t.\n',
    }


def prepare(directory, migration_directory=Path('.state/migration')):
    recipe = json.loads(Path('infra/vep-runtime.json').read_text())
    raw_plan = (migration_directory / 'plan.json').read_bytes()
    if sha(raw_plan) != PLAN_SHA:
        raise ValueError('Only the exact original registered migration plan is permitted')
    plan = json.loads(raw_plan)
    fixtures = directory / 'fixtures';fixtures.mkdir(parents=True, exist_ok=True)
    members = []
    for expected in recipe['qualification_cache_members']:
        aliases = [(o, a) for o in plan['objects'] for a in o['aliases']
                   if a['path'].endswith('/vep_cache/' + expected['path'])]
        if len(aliases) != 1:
            raise ValueError('Exact original qualification cache member is missing or ambiguous')
        obj, alias = aliases[0]
        if obj['sha256'] != expected['sha256'] or obj['bytes'] != expected['bytes']:
            raise ValueError('Qualification cache differs from the exact original release')
        fd = os.open(alias['path'], os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno());raw = stream.read(expected['bytes'] + 1);after = os.fstat(stream.fileno())
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        if before.st_size != expected['bytes'] or before.st_mtime_ns != alias['mtime_ns'] or identity(after) != identity(before) or sha(raw) != expected['sha256']:
            raise ValueError('Original qualification cache identity changed')
        target = fixtures / 'cache' / expected['path'];target.parent.mkdir(parents=True, exist_ok=True);target.write_bytes(raw)
        members.append({'path': str(target.relative_to(fixtures)), 'bytes': len(raw), 'sha256': sha(raw), 'source_dataset_id': obj['dataset_id']})
    url = f'https://raw.githubusercontent.com/Ensembl/ensembl-vep/{COMMIT}/examples/homo_sapiens_GRCh38.vcf'
    with urllib.request.urlopen(url, timeout=20) as response:
        example = response.read(16384)
    if hashlib.sha1(b'blob ' + str(len(example)).encode() + b'\0' + example).hexdigest() != EXAMPLE_GIT_SHA1:
        raise ValueError('Pinned upstream example VCF changed')
    rows = [line for line in example.decode().splitlines() if line.startswith('#') or (line.startswith('21\t') and 25000001 <= int(line.split('\t')[1]) <= 26000000)]
    (fixtures / 'cache-control.vcf').write_text('\n'.join(rows) + '\n')
    (fixtures / 'upstream-example.vcf').write_bytes(example)
    # A 180-base ORF split across two exons supplies independent known coding
    # changes: AAA>TAA stop gained, GCT>ACT missense and GCT>GCC synonymous.
    for name, text in spliced_control().items():
        (fixtures / name).write_text(text)
    files = [{'path': str(path.relative_to(fixtures)), 'bytes': path.stat().st_size, 'sha256': sha(path.read_bytes())}
             for path in sorted(fixtures.rglob('*')) if path.is_file()]
    manifest = {'schema': 1, 'upstream_commit': COMMIT, 'cache_version': 115, 'cache_assembly': 'GRCh38',
                'cache_scope': 'Original release-115 chr21:25000001-26000000 transcript cache only; incomplete for production',
                'original_members': members, 'example_source': url, 'example_git_blob_sha1': EXAMPLE_GIT_SHA1,
                'files': files, 'human_records': len([row for row in rows if not row.startswith('#')]),
                'gff_expected': {'stop': 'stop_gained', 'missense': 'missense_variant', 'synonymous': 'synonymous_variant'}}
    raw = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode();(directory / 'fixture-manifest.json').write_bytes(raw)
    production = [{'path': alias['path'].split('/vep_cache/', 1)[1], 'bytes': obj['bytes'], 'sha256': obj['sha256']}
                  for obj in plan['objects'] for alias in obj['aliases'] if '/vep_cache/homo_sapiens/115_GRCh38/' in alias['path']]
    if len(production) != 13922 or len({row['path'] for row in production}) != 13922:
        raise ValueError('Original complete production cache membership differs')
    (directory / 'production-cache-members.json').write_text(json.dumps({'schema': 1, 'cache_version': 115, 'assembly': 'GRCh38',
                                                                      'files': sorted(production, key=lambda row: row['path'])}, sort_keys=True, separators=(',', ':')))
    wheels = directory / 'wheels';wheels.mkdir(exist_ok=True)
    required = {line.rsplit('sha256:', 1)[1] for line in Path('infra/vep-requirements.lock').read_text().splitlines() if line.strip()}
    for path in Path('.state/deepvariant-build/wheels').glob('*.whl'):
        if sha(path.read_bytes()) in required and not (wheels / path.name).exists():
            shutil.copyfile(path, wheels / path.name)
    if {sha(path.read_bytes()) for path in wheels.glob('*.whl')} != required:
        subprocess.run([sys.executable, '-m', 'pip', 'download', '--use-deprecated=legacy-certs', '--dest', str(wheels), '--only-binary=:all:',
                        '--require-hashes', '--no-deps', '-r', 'infra/vep-requirements.lock'], check=True)
    return {'fixture_manifest_sha256': sha(raw), 'fixture_bytes': sum(f['bytes'] for f in files), 'human_records': manifest['human_records'], 'aws_mutations': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__);parser.add_argument('--directory', default='.state/vep-build')
    args = parser.parse_args();print(json.dumps(prepare(Path(args.directory)), indent=2))


if __name__ == '__main__':
    main()
