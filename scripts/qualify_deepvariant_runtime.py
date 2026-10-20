"""Execute the actual WGS model and rejection controls on pinned public reads."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import pysam
from backend import compiler, registry
from backend.deepvariant_runtime import UPSTREAM_IMAGE, VERSION

ROOT = Path(os.environ.get('OPENNGS_DEEPVARIANT_QUALIFICATION_ROOT', '/qualification'))
FIXTURES = Path('/qualification/fixtures')


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(8 * 1024**2), b''):
            result.update(block)
    return result.hexdigest()


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    recipe = json.loads(Path('/opt/studio/deepvariant-runtime.json').read_text())
    for row in recipe['fixtures']:
        path = FIXTURES / row['name']
        if path.stat().st_size != row['bytes'] or digest(path) != row['sha256']:
            raise ValueError('Public fixture integrity mismatch: ' + row['name'])
    source = FIXTURES / 'NA12878_S1.chr20.10_10p1mb.bam'
    # The historical source omits HD.SO; a genuine coordinate sort supplies it
    # while preserving the public reads and their original alignment reference.
    reads = ROOT / 'sorted.bam'
    pysam.sort('-o', str(reads), str(source))
    pysam.index(str(reads))
    reference_source = FIXTURES / 'ucsc.hg19.chr20.unittest.fasta'
    # Exercise the production BGZF path without an expanded reference copy.
    # The source stays immutable; compression and indexes occupy scratch only.
    reference = ROOT / 'reference.fa.gz'
    pysam.tabix_compress(str(reference_source), str(reference), force=True)
    pysam.faidx(str(reference))
    uncompressed = hashlib.sha256()
    with gzip.open(reference, 'rb') as handle:
        for block in iter(lambda: handle.read(8 * 1024**2), b''):
            uncompressed.update(block)
    reference_lossless = uncompressed.hexdigest() == digest(reference_source)
    if not reference_lossless:
        raise ValueError('BGZF qualification reference differs from the pinned original FASTA')
    bound = {'alignment': [{'path': str(reads), 'format': 'INDEXED_BAM', 'metadata': {'index': str(reads) + '.bai'}}],
             'reference': [{'path': str(reference), 'format': 'FASTA',
                            'metadata': {'index': str(reference) + '.fai', 'index_paths': [str(reference) + '.gzi']}}]}
    plan = compiler.compile_tool(registry.BASE['deepvariant'], {'params': {'model_type': 'WGS', 'interval': 'chr20:10000000-10010000'},
        'resources': {'threads': 1, 'memory_mb': 4096}}, bound, Path('/work'))
    for original, target in plan['bindings']:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
    argv = plan['steps'][0]['argv']
    cases = []
    began = time.monotonic()
    with (ROOT / 'positive.log').open('wb') as log:
        result = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, timeout=720, check=False)
    positive = {'id': 'wgs-truth-control', 'status': 'failed', 'seconds': round(time.monotonic() - began, 3)}
    try:
        if result.returncode:
            raise RuntimeError((ROOT / 'positive.log').read_text(errors='replace')[-3000:])
        output = Path('/work/calls.vcf.gz')
        with pysam.VariantFile(str(output)) as vcf:
            samples = list(vcf.header.samples)
            assert samples == ['NA12878'], samples
            rows = list(vcf)
        with pysam.TabixFile(str(output)) as tabix:
            indexed = sum(1 for contig in tabix.contigs for _ in tabix.fetch(contig))
        truth = FIXTURES / 'test_nist.b37_chr20_100kbp_at_10mb.vcf.gz'
        def alleles(records):
            return {(r.contig, r.pos, r.ref, alt) for r in records for alt in r.alts or ()}
        with pysam.VariantFile(str(truth)) as handle:
            truth_rows = list(handle.fetch('chr20', 9999999, 10010000))
        matches = len(alleles(rows) & alleles(truth_rows))
        assert matches >= 10, matches
        assert indexed == len(rows) and indexed > 0
        assert all(r.contig == 'chr20' and 10000000 <= r.pos <= 10010000 for r in rows)
        assert all('GT' in r.samples['NA12878'] for r in rows)
        staged=list(Path('/work/deepvariant-inputs').iterdir())
        assert len(staged)==5 and all(p.is_symlink() and p.resolve().is_relative_to('/inputs') for p in staged)
        canonical_reference = Path('/work/deepvariant-inputs/reference.fa')
        canonical_gzi = Path(str(canonical_reference) + '.gzi')
        with canonical_reference.open('rb') as handle:
            assert handle.read(2) == b'\x1f\x8b'
        assert canonical_gzi.is_symlink() and digest(canonical_gzi) == digest(str(reference) + '.gzi')
        positive.update(status='passed', assertions={'variant_records': len(rows), 'indexed_records': indexed,
            'truth_allele_matches': matches, 'truth_variants_in_region': len(truth_rows),
            'single_sample': samples[0], 'requested_region_only': True, 'input_staging_symlinks': 5,
            'reference_bgzf': True, 'explicit_reference_gzi': True, 'reference_lossless': reference_lossless,
            'scope': 'Actual NA12878 10kb WGS inference and exact-allele truth overlap; not normalized precision/recall or clinical validation'},
            outputs=[{'name': p.name, 'bytes': p.stat().st_size, 'sha256': digest(p)} for p in
                     [output, Path(str(output) + '.tbi'), Path('/work/calls.runtime.json')]])
    except Exception as error:
        positive['error'] = str(error)[-3500:]
    cases.append(positive)

    bad_reference = ROOT / 'wrong-reference.fa'
    bad_reference.write_text('>chr20\nACGT\n')
    pysam.faidx(str(bad_reference))
    multisample = ROOT / 'multi-sample.bam'
    with pysam.AlignmentFile(str(reads), 'rb') as original:
        header = original.header.to_dict()
        header['RG'].append({'ID': 'another', 'SM': 'OTHER'})
        with pysam.AlignmentFile(str(multisample), 'wb', header=header) as target:
            for row in original:
                target.write(row)
    pysam.index(str(multisample))
    def rejection(ident, changes, message):
        command = list(argv)
        for key, value in changes.items():
            command[command.index('--' + key) + 1] = str(value)
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, check=False)
        actual = result.stdout.decode(errors='replace')
        passed = result.returncode != 0 and message in actual
        cases.append({'id': ident, 'status': 'passed' if passed else 'failed',
            'assertions': {'nonzero_exit': result.returncode != 0, 'expected_rejection': message} if passed else {},
            **({} if passed else {'error': actual[-1800:]})})
    rejection('missing-index', {'index': ROOT / 'absent.bai'}, 'explicit BAM/BAI and FASTA/FAI')
    missing_gzi = ROOT / 'missing-gzi.fa.gz'
    shutil.copyfile(reference, missing_gzi)
    shutil.copyfile(str(reference) + '.fai', str(missing_gzi) + '.fai')
    rejection('missing-gzi', {'reference': missing_gzi, 'reference-index': str(missing_gzi) + '.fai'},
              'explicit adjacent GZI companion')
    if Path(str(missing_gzi) + '.gzi').exists():
        raise ValueError('Missing-GZI control must reject without generating an unbound companion')
    rejection('reference-mismatch', {'reference': bad_reference, 'reference-index': str(bad_reference) + '.fai'}, 'contigs and lengths must exactly match')
    rejection('multi-sample', {'reads': multisample, 'index': str(multisample) + '.bai'}, 'exactly one explicit BAM read-group sample')
    rejection('unsupported-model', {'model-type': 'WES'}, 'Only the DeepVariant 1.8.0 WGS model')
    models = {}
    for name in ['wgs', 'smallmodels/wgs']:
        root = Path('/opt/models/wgs') if name == 'wgs' else Path('/opt/smallmodels/wgs')
        files = [{'path': str(p.relative_to(root)), 'bytes': p.stat().st_size, 'sha256': digest(p)}
                 for p in sorted(root.rglob('*')) if p.is_file()]
        if not files or not any(p['path'] == 'saved_model.pb' for p in files):
            raise ValueError('Pinned upstream model files are missing')
        models[name] = files
    report = {'upstream_image': UPSTREAM_IMAGE, 'version': VERSION, 'cases': cases,
        'dependency_lock_sha256': digest('/opt/studio/deepvariant-requirements.lock'),
        'models': {'WGS': models}, 'fixtures': recipe['fixtures'],
        'prepared_bam': {'source_sha256': digest(source), 'operation': 'pysam coordinate sort and index', 'sha256': digest(reads)},
        'prepared_reference': {'source_sha256': digest(reference_source), 'operation': 'lossless pysam BGZF compression and faidx',
            'sha256': digest(reference), 'uncompressed_sha256': uncompressed.hexdigest(),
            'gzi_sha256': digest(str(reference) + '.gzi')}}
    report_path=Path(sys.argv[1])
    report_path.write_text(json.dumps(report, indent=2))
    for output in positive.get('outputs', []):
        original=Path('/work')/output['name']
        target=report_path.parent/output['name']
        if original!=target:shutil.copyfile(original,target)
    if ROOT/'positive.log'!=report_path.parent/'positive.log':shutil.copyfile(ROOT/'positive.log',report_path.parent/'positive.log')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
