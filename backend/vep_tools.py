"""Offline VEP execution with exact complete archive membership and REF checks.

No URL, plugin, database connection or arbitrary VEP arguments are accepted.
The incomplete qualification cache is never accepted by the production CLI.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile

from .vep_runtime import (ASSEMBLY, CACHE_ARCHIVE_BYTES, CACHE_ARCHIVE_SHA256, CACHE_EXTRACTED_BYTES,
                          CACHE_INFO_SHA256, CACHE_MANIFEST_SHA256, CACHE_MEMBER_COUNT, CACHE_VERSION, VERSION)

FIELDS = 'Allele,Consequence,IMPACT,SYMBOL,Gene,Feature_type,Feature,BIOTYPE,EXON,INTRON'


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for raw in iter(lambda: stream.read(4 * 1024**2), b''):
            value.update(raw)
    return value.hexdigest()


def native_environment():
    # Preserve the pinned Perl API lookup while excluding the worker's Python
    # dependencies from any upstream scientific subprocess.
    return {k: v for k, v in os.environ.items() if k not in ['PYTHONPATH', 'PYTHONHOME', 'PERL5OPT', 'http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY']}


def runtime_version(log_path):
    # VEP's supported help entrypoint prints its own version and those of its
    # Ensembl APIs. It has no --version flag: that option exits 255 in 115.2.
    result = subprocess.run(['vep', '--help'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=native_environment(), timeout=30)
    Path(log_path).write_bytes(result.stdout)
    text = result.stdout.decode(errors='replace')
    match = re.search(r'^\s*ensembl-vep\s*:\s*(\d+\.\d+)\s*$', text, re.MULTILINE)
    if result.returncode != 0 or match is None or match.group(1) != VERSION:
        raise ValueError('Supported VEP help probe failed or scientific executable version differs; see version.log')
    return text


def worker_package_versions(apt_lock, installed_lock):
    required = dict(line.split('=', 1) for line in Path(apt_lock).read_text().splitlines() if line.strip())
    installed = {}
    for line in Path(installed_lock).read_text().splitlines():
        fields = line.split('\t')
        if len(fields) == 2:
            installed[fields[0].split(':', 1)[0]] = fields[1]
    if any(installed.get(name) != version for name, version in required.items()):
        raise ValueError('Python worker apt packages differ from the reviewed exact versions')
    return required


def cache_info(cache_root):
    path = Path(cache_root) / 'homo_sapiens/115_GRCh38/info.txt'
    if not path.is_file() or digest(path) != CACHE_INFO_SHA256:
        raise ValueError('Exact original release-115 cache info.txt is required')
    pairs = [line.split('\t', 1) for line in path.read_text().splitlines() if '\t' in line]
    info = dict(pairs)
    if info.get('species') != 'homo_sapiens' or info.get('assembly') != ASSEMBLY:
        raise ValueError('VEP cache species or assembly differs')
    return info


def extract_cache(archive, destination, member_manifest):
    archive, destination, member_manifest = Path(archive), Path(destination), Path(member_manifest)
    if archive.stat().st_size != CACHE_ARCHIVE_BYTES or digest(archive) != CACHE_ARCHIVE_SHA256:
        raise ValueError('VEP requires the complete unchanged original cache archive')
    if digest(member_manifest) != CACHE_MANIFEST_SHA256:
        raise ValueError('Complete cache membership manifest differs from reviewed originals')
    manifest = json.loads(member_manifest.read_bytes());expected = {r['path']: r for r in manifest['files']}
    if len(expected) != CACHE_MEMBER_COUNT or sum(r['bytes'] for r in expected.values()) != CACHE_EXTRACTED_BYTES:
        raise ValueError('Complete cache membership totals differ')
    if destination.exists():
        raise ValueError('Cache extraction requires fresh bounded job scratch')
    destination.mkdir(parents=True);seen = set();total = 0
    with tarfile.open(archive, 'r|gz') as source:
        for member in source:
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or '\\' in member.name or not name.parts or name.parts[0] != 'homo_sapiens':
                raise ValueError('Unsafe cache archive member')
            if member.isdir():
                if not (str(name) == 'homo_sapiens' or str(name).startswith('homo_sapiens/115_GRCh38')):
                    raise ValueError('Archive directory escaped the exact release')
                continue
            if not member.isfile() or str(name) not in expected or str(name) in seen:
                raise ValueError('Cache archive contains an unexpected, duplicate or non-regular member')
            row = expected[str(name)]
            if member.size != row['bytes']:
                raise ValueError('Cache archive member length differs from original inventory')
            target = destination.joinpath(*name.parts);target.parent.mkdir(parents=True, exist_ok=True)
            value = hashlib.sha256();written = 0
            with source.extractfile(member) as stream, target.open('xb') as out:
                for raw in iter(lambda: stream.read(4 * 1024**2), b''):
                    written += len(raw);total += len(raw)
                    if written > row['bytes'] or total > CACHE_EXTRACTED_BYTES:
                        raise ValueError('Cache archive exceeded its exact extraction scope')
                    value.update(raw);out.write(raw)
            if written != row['bytes'] or value.hexdigest() != row['sha256']:
                raise ValueError('Cache archive member SHA-256 differs from original inventory')
            target.chmod(0o444);seen.add(str(name))
    if seen != set(expected) or total != CACHE_EXTRACTED_BYTES:
        raise ValueError('Production VEP cache is incomplete; a qualification shard cannot substitute')
    cache_info(destination)
    return {'members': len(seen), 'bytes': total, 'archive_sha256': CACHE_ARCHIVE_SHA256, 'member_manifest_sha256': CACHE_MANIFEST_SHA256}


def vcf_rows(path):
    opener = gzip.open if str(path).endswith(('.gz', '.bgz')) else open
    with opener(path, 'rt') as stream:
        columns = False;last_contig = None;last_position = 0;completed = set()
        for line in stream:
            if line.startswith('##reference=') and any(x in line.lower() for x in ['grch37', 'hg19', 'chm13']):
                raise ValueError('VCF reference declaration conflicts with the GRCh38 profile')
            if line.startswith('#CHROM\t'):
                columns = True;continue
            if line.startswith('#'):
                continue
            fields = line.rstrip('\n').split('\t')
            if not columns or len(fields) < 8:
                raise ValueError('VEP input must be a valid VCF with its column header')
            contig = fields[0]
            try:
                position = int(fields[1])
            except ValueError:
                raise ValueError('VCF positions must be positive integers') from None
            if position <= 0 or not re.fullmatch('[ACGTNacgtn]+', fields[3]) or any(not re.fullmatch('[ACGTNacgtn]+', alt) for alt in fields[4].split(',')):
                raise ValueError('This VEP profile accepts explicit small-variant alleles only')
            if contig != last_contig:
                if contig in completed:
                    raise ValueError('VCF contigs must occur in one sorted block')
                if last_contig is not None:
                    completed.add(last_contig)
                last_contig = contig;last_position = 0
            if position < last_position:
                raise ValueError('VCF positions must be sorted before annotation')
            last_position = position
            yield fields


def validate_reference(variants, fasta, fai):
    entries = {}
    for line in Path(fai).read_text().splitlines():
        fields = line.split('\t')
        if len(fields) != 5 or fields[0] in entries:
            raise ValueError('FASTA index has invalid or duplicate rows')
        values = tuple(map(int, fields[1:]));length, offset, bases, width = values
        if length <= 0 or offset < 0 or not 0 < bases <= width <= bases + 2:
            raise ValueError('FASTA index arithmetic is invalid')
        entries[fields[0]] = values
    records = 0
    with Path(fasta).open('rb') as stream:
        if stream.read(2) == b'\x1f\x8b':
            raise ValueError('This VEP reference profile requires uncompressed FASTA and exact FAI')
        for row in vcf_rows(variants):
            name = row[0];candidates = [name, name[3:] if name.startswith('chr') else 'chr' + name]
            if name in ['MT', 'M', 'chrM']:
                candidates += ['MT', 'chrM']
            matches = [candidate for candidate in dict.fromkeys(candidates) if candidate in entries]
            if len(matches) != 1:
                raise ValueError('VCF contig has no unambiguous matching FASTA sequence')
            length, offset, bases, width = entries[matches[0]];start = int(row[1]) - 1;end = start + len(row[3])
            if end > length:
                raise ValueError('VCF REF extends beyond its exact FASTA contig')
            first = offset + start // bases * width + start % bases
            last = offset + (end - 1) // bases * width + (end - 1) % bases + 1
            stream.seek(first);actual = stream.read(last - first).replace(b'\n', b'').replace(b'\r', b'')
            if actual.upper() != row[3].upper().encode():
                raise ValueError('VCF REF allele differs from the selected exact FASTA')
            records += 1
    if not records:
        raise ValueError('VEP requires at least one input small variant')
    return records


def command(variants, output, *, cache_root=None, fasta=None, gff=None, assembly=ASSEMBLY, cache_version=CACHE_VERSION):
    if cache_root is None and (gff is None or fasta is None):
        raise ValueError('VEP requires an explicit cache or the independent GFF plus FASTA control')
    args = ['vep', '--format', 'vcf', '--vcf', '--symbol', '--terms', 'SO', '--no_stats', '--force_overwrite',
            '--input_file', str(variants), '--output_file', str(output), '--fields', FIELDS, '--species', 'homo_sapiens']
    if cache_root is not None:
        args += ['--offline', '--cache', '--dir_cache', str(cache_root), '--cache_version', str(cache_version), '--assembly', assembly]
    if fasta:
        args += ['--fasta', str(fasta), '--check_ref']
    if gff:
        # In pinned VEP 115.2, explicit --offline implies --cache. Independent
        # GFF+FASTA instead uses Runner's automatic offline branch when neither
        # cache nor database is selected, before any registry connection.
        args += ['--gff', str(gff)]
    return args


def annotation_summary(path):
    fields = None;records = [];symbols = set();consequences = set()
    opener = gzip.open if str(path).endswith('.gz') else open
    with opener(path, 'rt') as stream:
        for line in stream:
            if line.startswith('##INFO=<ID=CSQ,'):
                match = re.search(r'Format: ([^"\n]+)', line)
                if match:
                    fields = match.group(1).split('|')
            if line.startswith('#'):
                continue
            row = line.rstrip('\n').split('\t')
            info = dict(x.split('=', 1) for x in row[7].split(';') if '=' in x)
            if fields is None or not info.get('CSQ'):
                raise ValueError('VEP output is missing actual CSQ annotations')
            annotations = [dict(zip(fields, value.split('|'))) for value in info['CSQ'].split(',')]
            for annotation in annotations:
                if not annotation.get('Consequence'):
                    raise ValueError('VEP emitted an annotation without a consequence')
                consequences.update(annotation['Consequence'].split('&'))
                if annotation.get('SYMBOL'):
                    symbols.add(annotation['SYMBOL'])
            records.append({'id': row[2], 'contig': row[0], 'position': int(row[1]), 'ref': row[3], 'alt': row[4], 'annotations': annotations})
    return {'annotated_records': len(records), 'gene_symbols': sorted(symbols), 'consequences': sorted(consequences), 'records': records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['variants', 'cache-archive', 'fasta', 'fai', 'output']:
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--cache-members', default='/opt/studio/vep-cache-members.json')
    parser.add_argument('--work', default='/work/vep-cache')
    args = parser.parse_args()
    records = validate_reference(args.variants, args.fasta, args.fai)
    cache = extract_cache(args.cache_archive, args.work, args.cache_members)
    output = Path(args.output);plain = output.with_suffix('') if output.suffix == '.gz' else output
    subprocess.run(command(args.variants, plain, cache_root=args.work, fasta=args.fasta), env=native_environment(), check=True)
    summary = annotation_summary(plain)
    if summary['annotated_records'] != records:
        raise ValueError('VEP did not retain every validated input record')
    if output.suffix == '.gz':
        with output.open('wb') as stream:
            subprocess.run(['bgzip', '-c', str(plain)], stdout=stream, check=True, env=native_environment())
        subprocess.run(['tabix', '-p', 'vcf', str(output)], check=True, env=native_environment())
    report = {'version': VERSION, 'cache_version': CACHE_VERSION, 'assembly': ASSEMBLY, 'offline': True, 'cache': cache,
              'annotated_records': records, 'reference_sha256': digest(args.fasta),
              'scope': 'Computational transcript consequences; no independent pathogenicity or clinical classification'}
    output.with_name(output.name + '.runtime.json').write_text(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
