import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest
from backend import vep_runtime as runtime, vep_tools as tools
from scripts.finalize_vep_runtime import verify_outputs
from scripts.prepare_vep_runtime import spliced_control


def cache_asset():
    return {'name': runtime.CACHE_ARCHIVE_NAME, 'sha256': runtime.CACHE_ARCHIVE_SHA256,
            'bytes': runtime.CACHE_ARCHIVE_BYTES, 'path': 's3://private/original-cache',
            'assembly': 'GRCh38', 'metadata': {'version_id': 'immutable-original'}, 'state': 'Ready'}


def test_complete_cache_contract_rejects_shards_wrong_release_and_mutable_versions():
    assert runtime.cache_binding(cache_asset())['complete_members'] == 13922
    for changes in [{'name': '25000001-26000000.gz'}, {'sha256': '0' * 64}, {'bytes': 1},
                    {'assembly': 'GRCh37'}, {'state': 'Provisioning'}, {'metadata': {'version_id': 'null'}}]:
        with pytest.raises(ValueError):
            runtime.cache_binding({**cache_asset(), **changes})


def archive_fixture(tmp_path, monkeypatch, kind='valid'):
    files = {'homo_sapiens/115_GRCh38/info.txt': b'species\thomo_sapiens\nassembly\tGRCh38\n',
             'homo_sapiens/115_GRCh38/21/1-1000000.gz': b'exact original transcript bytes'}
    rows = [{'path': name, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()} for name, body in files.items()]
    manifest = tmp_path / 'members.json';manifest.write_text(json.dumps({'files': rows}))
    archive = tmp_path / 'cache.tar.gz'
    with tarfile.open(archive, 'w:gz') as output:
        for name, body in files.items():
            if kind == 'missing' and name.endswith('.gz'):
                continue
            member = tarfile.TarInfo(name);member.size = len(body)
            if kind == 'checksum' and name.endswith('.gz'):
                body = b'x' * len(body)
            output.addfile(member, io.BytesIO(body))
            if kind == 'duplicate' and name.endswith('info.txt'):
                output.addfile(member, io.BytesIO(body))
        if kind in ['symlink', 'traversal']:
            member = tarfile.TarInfo('homo_sapiens/115_GRCh38/link' if kind == 'symlink' else '../escape')
            if kind == 'symlink':
                member.type = tarfile.SYMTYPE;member.linkname = '/outside'
            else:
                member.size = 1
            output.addfile(member, io.BytesIO(b'x'))
    for name, value in [('CACHE_ARCHIVE_BYTES', archive.stat().st_size), ('CACHE_ARCHIVE_SHA256', tools.digest(archive)),
                        ('CACHE_MANIFEST_SHA256', tools.digest(manifest)), ('CACHE_MEMBER_COUNT', len(rows)),
                        ('CACHE_EXTRACTED_BYTES', sum(row['bytes'] for row in rows)),
                        ('CACHE_INFO_SHA256', hashlib.sha256(files['homo_sapiens/115_GRCh38/info.txt']).hexdigest())]:
        monkeypatch.setattr(tools, name, value)
    return archive, manifest


def test_complete_archive_extracts_only_exact_original_members(tmp_path, monkeypatch):
    archive, manifest = archive_fixture(tmp_path, monkeypatch)
    result = tools.extract_cache(archive, tmp_path / 'scratch', manifest)
    assert result['members'] == 2
    assert tools.cache_info(tmp_path / 'scratch')['assembly'] == 'GRCh38'
    with pytest.raises(ValueError, match='fresh bounded'):
        tools.extract_cache(archive, tmp_path / 'scratch', manifest)


@pytest.mark.parametrize('kind', ['missing', 'duplicate', 'symlink', 'traversal', 'checksum'])
def test_archive_rejects_missing_changed_duplicate_and_unsafe_members(tmp_path, monkeypatch, kind):
    archive, manifest = archive_fixture(tmp_path, monkeypatch, kind)
    with pytest.raises(ValueError):
        tools.extract_cache(archive, tmp_path / 'scratch', manifest)
    assert not (tmp_path / 'escape').exists()


def reference_fixture(tmp_path):
    reference = tmp_path / 'reference.fa';reference.write_text('>chr1\nACGTAC\nGTACGT\n')
    index = tmp_path / 'reference.fa.fai';index.write_text('chr1\t12\t6\t6\t7\n')
    variants = tmp_path / 'input.vcf';variants.write_text('##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n1\t6\tv\tCGT\tC\t.\tPASS\t.\n')
    return variants, reference, index


def test_reference_guard_checks_alleles_across_fasta_line_breaks(tmp_path):
    variants, reference, index = reference_fixture(tmp_path)
    assert tools.validate_reference(variants, reference, index) == 1
    variants.write_text(variants.read_text().replace('\tCGT\t', '\tAAA\t'))
    with pytest.raises(ValueError, match='REF allele differs'):
        tools.validate_reference(variants, reference, index)


def test_reference_guard_rejects_ambiguous_contig_aliases_and_gzip(tmp_path):
    variants, reference, index = reference_fixture(tmp_path)
    index.write_text(index.read_text() + '1\t12\t6\t6\t7\n')
    with pytest.raises(ValueError, match='unambiguous'):
        tools.validate_reference(variants, reference, index)
    reference.write_bytes(gzip.compress(reference.read_bytes()))
    with pytest.raises(ValueError, match='uncompressed'):
        tools.validate_reference(variants, reference, index)


@pytest.mark.parametrize('line', ['1\t2\tv\tA\t<DEL>\t.\tPASS\t.\n', '1\t0\tv\tA\tT\t.\tPASS\t.\n'])
def test_small_variant_profile_rejects_invalid_or_unsupported_input(tmp_path, line):
    path = tmp_path / 'input.vcf';path.write_text('#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n' + line)
    with pytest.raises(ValueError):
        list(tools.vcf_rows(path))


def test_command_is_offline_and_cannot_enable_unselected_plugins_or_existing_variant_scores():
    argv = tools.command('/inputs/variants.vcf', '/work/output.vcf', cache_root='/work/cache', fasta='/inputs/reference.fa')
    assert '--offline' in argv and '--cache' in argv and '--check_ref' in argv
    assert argv[argv.index('--cache_version') + 1] == '115'
    assert not {'--plugin', '--everything', '--sift', '--polyphen', '--check_existing', '--database'}.intersection(argv)


def test_independent_gff_uses_native_automatic_offline_without_implied_cache():
    argv = tools.command('/inputs/control.vcf', '/work/control.vcf', fasta='/inputs/control.fa', gff='/work/control.gff3.gz')
    assert '--gff' in argv and '--fasta' in argv and '--check_ref' in argv
    assert not {'--offline', '--cache', '--database', '--dir_cache'}.intersection(argv)
    with pytest.raises(ValueError, match='explicit cache'):
        tools.command('/inputs/control.vcf', '/work/control.vcf', gff='/work/control.gff3.gz')


def test_runtime_version_uses_supported_help_and_preserves_actual_stdout(tmp_path, monkeypatch):
    calls = [];raw = b'Versions:\n  ensembl : 115\n  ensembl-vep          : 115.2\n\nUsage:\n'
    def run(argv, **kwargs):
        calls.append(argv);return SimpleNamespace(returncode=0, stdout=raw)
    monkeypatch.setattr(tools.subprocess, 'run', run)
    log = tmp_path / 'version.log'
    assert 'ensembl-vep' in tools.runtime_version(log)
    assert calls == [['vep', '--help']] and log.read_bytes() == raw


@pytest.mark.parametrize('returncode,raw', [(255, b'Unknown option: version\n'),
                                         (0, b'ensembl-vep : 116.3\n'),
                                         (0, b'other-module : 115.2\n')])
def test_runtime_version_rejects_failed_wrong_or_unrelated_version(tmp_path, monkeypatch, returncode, raw):
    monkeypatch.setattr(tools.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=returncode, stdout=raw))
    log = tmp_path / 'version.log'
    with pytest.raises(ValueError, match='Supported VEP help probe'):
        tools.runtime_version(log)
    assert log.read_bytes() == raw


def test_worker_apt_lock_checks_installed_versions_and_normalizes_architecture(tmp_path):
    expected = tmp_path / 'apt.lock';expected.write_text('libpython3.10=3.10.12-1~22.04.18\npython3.10=3.10.12-1~22.04.18\n')
    actual = tmp_path / 'installed.lock';actual.write_text('libpython3.10:amd64\t3.10.12-1~22.04.18\npython3.10\t3.10.12-1~22.04.18\n')
    assert tools.worker_package_versions(expected, actual)['python3.10'] == '3.10.12-1~22.04.18'
    actual.write_text(actual.read_text().replace('22.04.18', '22.04.11'))
    with pytest.raises(ValueError, match='exact versions'):
        tools.worker_package_versions(expected, actual)


def release():
    outputs = [{'name': 'calls.vcf.gz', 'bytes': 10, 'sha256': 'a' * 64}]
    cases = [
        {'id': 'release-115-cache', 'status': 'passed', 'outputs': outputs, 'assertions': {
            'annotated_records': 10, 'indexed_records': 10, 'gene_symbols': ['MRPL39'], 'cache_info_sha256': runtime.CACHE_INFO_SHA256}},
        {'id': 'spliced-gff-control', 'status': 'passed', 'outputs': outputs, 'assertions': {
            'annotated_records': 3, 'indexed_records': 3, 'gene_symbols': ['NGS_CONTROL'],
            'automatic_offline': True,
            'expected_consequences': {'stop': 'stop_gained', 'missense': 'missense_variant', 'synonymous': 'synonymous_variant'}}},
    ] + [{'id': name, 'status': 'passed', 'assertions': {'nonzero_exit': True}} for name in ['wrong-cache-version', 'wrong-assembly', 'missing-cache']]
    return {'family': runtime.FAMILY, 'version': runtime.VERSION, 'cache_version': 115, 'assembly': 'GRCh38',
            'upstream_image': runtime.UPSTREAM_IMAGE, 'image': '509505638328.dkr.ecr.us-east-1.amazonaws.com/openngs-demo@sha256:' + 'b' * 64,
            'tools': ['vep'], 'source_sha256': 'c' * 64, 'requirements_sha256': runtime.REQUIREMENTS_SHA256,
            'worker_apt_lock_sha256': runtime.WORKER_APT_LOCK_SHA256,
            'fixture_manifest_sha256': runtime.FIXTURE_MANIFEST_SHA256, 'cache_manifest_sha256': runtime.CACHE_MANIFEST_SHA256,
            'qualification': {'version': runtime.VERSION, 'upstream_image': runtime.UPSTREAM_IMAGE, 'network': 'none',
                              'worker_python_version': '3.10.12', 'worker_apt_lock_sha256': runtime.WORKER_APT_LOCK_SHA256,
                              'worker_package_versions': {'python3.10': '3.10.12-1~22.04.18'},
                              'worker_installed_packages_sha256': 'e' * 64,
                              'readonly_root': True, 'input_mount_readonly': True, 'cases': cases}}


def test_runtime_needs_every_executed_positive_negative_and_isolation_control():
    assert runtime.validate_release(release())['tools'] == ['vep']
    changes = []
    item = release();item['qualification']['cases'].pop();changes.append(item)
    item = release();item['qualification']['network'] = 'available';changes.append(item)
    item = release();item['qualification']['cases'][0]['assertions']['gene_symbols'] = [];changes.append(item)
    item = release();item['qualification']['cases'][0]['assertions']['indexed_records'] = 9;changes.append(item)
    item = release();item['qualification']['cases'][2]['assertions']['nonzero_exit'] = False;changes.append(item)
    item = release();item['requirements_sha256'] = 'd' * 64;changes.append(item)
    for value in changes:
        with pytest.raises(ValueError):
            runtime.validate_release(value)


def test_annotation_parser_requires_actual_csq_consequences(tmp_path):
    path = tmp_path / 'output.vcf'
    path.write_text('##INFO=<ID=CSQ,Number=.,Type=String,Description="Format: Allele|Consequence|SYMBOL">\n'
                    '#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n1\t1\tv\tA\tT\t.\tPASS\tCSQ=T|stop_gained|CONTROL\n')
    summary = tools.annotation_summary(path)
    assert summary['gene_symbols'] == ['CONTROL'] and summary['consequences'] == ['stop_gained']
    path.write_text(path.read_text().replace('T|stop_gained|', 'T||'))
    with pytest.raises(ValueError, match='without a consequence'):
        tools.annotation_summary(path)


def test_spliced_fixture_builds_real_bgzf_tabix_and_retains_all_feature_rows(tmp_path):
    import pysam
    fixtures = spliced_control()
    for name, text in fixtures.items():
        (tmp_path / name).write_text(text)
    gff = tmp_path / 'control.gff3';compressed = tmp_path / 'control.gff3.gz'
    pysam.tabix_compress(str(gff), str(compressed), force=True)
    pysam.tabix_index(str(compressed), preset='gff', force=True)
    with pysam.TabixFile(str(compressed)) as source:
        rows = [row.split('\t') for row in source.fetch('CONTROL', 99, 389)]
    assert len(rows) == 6
    assert [int(row[3]) for row in rows] == [100, 100, 100, 100, 300, 300]
    assert [row[2] for row in rows] == ['gene', 'mRNA', 'exon', 'CDS', 'exon', 'CDS']
    assert tools.validate_reference(tmp_path / 'gff-control.vcf', tmp_path / 'control.fa', tmp_path / 'control.fa.fai') == 3
    with pysam.FastaFile(str(tmp_path / 'control.fa')) as fasta:
        coding = fasta.fetch('CONTROL', 99, 189) + fasta.fetch('CONTROL', 299, 389)
    assert len(coding) == 180 and coding.startswith('ATGAAA') and coding.endswith('TAA')
    # Independent codon changes establish the expected scientific assertions.
    assert coding[3:6] == 'AAA' and 'T' + coding[4:6] == 'TAA'
    assert coding[6:9] == 'GCT' and 'A' + coding[7:9] == 'ACT'
    assert coding[6:8] + 'C' == 'GCC'
