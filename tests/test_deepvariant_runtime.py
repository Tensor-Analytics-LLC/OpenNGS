"""Local input/companion rejection tests; actual model inference runs in AWS."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import pysam
import pytest
from backend import compiler, registry
from backend.deepvariant_tools import run, stage_inputs, validate_inputs
from backend.deepvariant_runtime import (CASES, FAMILY, FIXTURE_MANIFEST_SHA256,
    ISOLATION, LEGACY_CASES, MODEL_FILES, REFERENCE_SHA256, REQUIREMENTS_SHA256, UPSTREAM_IMAGE, VERSION, validate_release)


def alignment(tmp_path, *, sample='SAMPLE', length=100, sort='coordinate', second_sample=False):
    reference = tmp_path / 'reference.fa'
    reference.write_text('>chr1\n' + 'A' * 100 + '\n')
    pysam.faidx(str(reference))
    path = tmp_path / 'reads.bam'
    groups = [{'ID': 'rg', 'SM': sample}]
    if second_sample:
        groups.append({'ID': 'other', 'SM': 'SECOND'})
    header = {'HD': {'VN': '1.6', 'SO': sort}, 'SQ': [{'SN': 'chr1', 'LN': length}], 'RG': groups}
    with pysam.AlignmentFile(str(path), 'wb', header=header) as target:
        read = pysam.AlignedSegment()
        read.query_name = 'read1'
        read.query_sequence = 'A' * 10
        read.query_qualities = pysam.qualitystring_to_array('I' * 10)
        read.reference_id = 0
        read.reference_start = 10
        read.mapping_quality = 60
        read.cigarstring = '10M'
        read.set_tag('RG', 'rg')
        target.write(read)
    pysam.index(str(path))
    return path, Path(str(path) + '.bai'), reference, Path(str(reference) + '.fai')


def test_real_bam_and_fasta_companions_validate(tmp_path):
    values = alignment(tmp_path)
    assert validate_inputs(*values, 'chr1:1-100') == {'sample': 'SAMPLE', 'reference_contigs': 1}
    values[1].unlink()
    with pytest.raises(ValueError, match='explicit BAM/BAI'):
        validate_inputs(*values)


@pytest.mark.parametrize('filename', ['compressed.fa.gz', 'compressed.fa'])
def test_actual_bgzf_reference_has_fifth_link_and_random_access_without_copy(tmp_path, filename):
    bam, bai, plain, _ = alignment(tmp_path)
    reference = tmp_path / filename
    pysam.tabix_compress(str(plain), str(reference), force=True)
    pysam.faidx(str(reference))
    fai, gzi = Path(str(reference) + '.fai'), Path(str(reference) + '.gzi')
    original = {path: path.read_bytes() for path in [bam, bai, reference, fai, gzi]}
    assert validate_inputs(bam, bai, reference, fai, 'chr1:1-100')['sample'] == 'SAMPLE'
    staged = tmp_path / 'staged'
    args = argparse.Namespace(reads=bam, index=bai, reference=reference, reference_index=fai)
    stage_inputs(args, staged, admitted_root=tmp_path)
    assert {p.name for p in staged.iterdir()} == {'reads.bam', 'reads.bam.bai', 'reference.fa', 'reference.fa.fai', 'reference.fa.gzi'}
    assert all(p.is_symlink() for p in staged.iterdir())
    assert (staged / 'reference.fa.gzi').resolve() == gzi
    assert (staged / 'reference.fa').read_bytes()[:2] == b'\x1f\x8b'
    with pysam.FastaFile(str(staged / 'reference.fa')) as handle:
        assert handle.fetch('chr1', 20, 30) == 'A' * 10
    assert all(path.read_bytes() == before for path, before in original.items())
    # No expanded FASTA or generated index appeared in the canonical folder.
    assert len(list(staged.iterdir())) == 5
    outside = tmp_path.parent / (tmp_path.name + '-unadmitted.gzi')
    outside.write_bytes(gzi.read_bytes())
    gzi.unlink()
    gzi.symlink_to(outside)
    with pytest.raises(ValueError, match='admitted regular files'):
        stage_inputs(args, staged, admitted_root=tmp_path)


def test_missing_bgzf_gzi_rejects_before_htslib_or_native_launch(tmp_path, monkeypatch):
    bam, bai, plain, _ = alignment(tmp_path)
    reference = tmp_path / 'compressed.fa'
    pysam.tabix_compress(str(plain), str(reference), force=True)
    pysam.faidx(str(reference))
    gzi = Path(str(reference) + '.gzi')
    gzi.unlink()
    def unexpected(*args, **kwargs):
        pytest.fail('Missing GZI must be rejected before opening FASTA or launching the model')
    monkeypatch.setattr(pysam, 'FastaFile', unexpected)
    monkeypatch.setattr('backend.deepvariant_tools.subprocess.run', unexpected)
    with pytest.raises(ValueError, match='explicit adjacent GZI companion'):
        run(argparse.Namespace(model_type='WGS', shards=1, reads=bam, index=bai,
            reference=reference, reference_index=Path(str(reference) + '.fai'), region=None, work=tmp_path / 'work'))
    assert not gzi.exists() and not (tmp_path / 'work').exists()


def test_plain_reference_ignores_extension_and_retains_four_links(tmp_path):
    bam, bai, plain, fai = alignment(tmp_path)
    reference = tmp_path / 'actually-plain.fa.gz'
    reference.write_bytes(plain.read_bytes())
    assert validate_inputs(bam, bai, reference, fai)['sample'] == 'SAMPLE'
    staged = tmp_path / 'staged'
    staged.mkdir()
    (staged / 'reference.fa.gzi').symlink_to(tmp_path / 'prior.gzi')
    stage_inputs(argparse.Namespace(reads=bam, index=bai, reference=reference, reference_index=fai),
                 staged, admitted_root=tmp_path)
    assert len(list(staged.iterdir())) == 4
    assert all(p.is_symlink() for p in staged.iterdir())
    assert not (staged / 'reference.fa.gzi').is_symlink()


@pytest.mark.parametrize(('options', 'message'), [
    ({'length': 101}, 'contigs and lengths'),
    ({'second_sample': True}, 'exactly one explicit'),
    ({'sort': 'queryname'}, 'coordinate-sorted'),
])
def test_incompatible_actual_alignment_is_rejected(tmp_path, options, message):
    with pytest.raises(ValueError, match=message):
        validate_inputs(*alignment(tmp_path, **options))


@pytest.mark.parametrize('region', ['chr1:0-20', 'chr1:1-101', 'chr2:1-20', 'chr1:1-20;echo', 'chr1:1-20 --dry_run=true'])
def test_regions_are_real_bounded_intervals_and_cannot_be_upstream_shell_arguments(tmp_path, region):
    with pytest.raises(ValueError, match='one valid one-based'):
        validate_inputs(*alignment(tmp_path), region)


def test_unqualified_model_is_rejected_before_any_native_execution():
    with pytest.raises(ValueError, match='Only the DeepVariant 1.8.0 WGS'):
        run(argparse.Namespace(model_type='WES'))


def test_compiler_binds_both_companions_and_exposes_output_index(tmp_path):
    bam, bai, ref, fai = alignment(tmp_path)
    inputs = {'alignment': [{'path': str(bam), 'format': 'INDEXED_BAM', 'metadata': {'index': str(bai)}}],
              'reference': [{'path': str(ref), 'format': 'FASTA', 'metadata': {'index': str(fai)}}]}
    spec = registry.BASE['deepvariant']
    plan = compiler.compile_tool(spec, {'params': {'interval': 'chr1:1-100'}}, inputs, tmp_path)
    assert len(plan['bindings']) == 4
    argv = plan['steps'][0]['argv']
    assert argv[:3] == ['/usr/bin/python3', '-m', 'backend.deepvariant_tools']
    assert argv[argv.index('--index') + 1].endswith('/reads.bam.bai')
    output = plan['outputs']['variants'][0]
    assert output['format'] == 'INDEXED_VCF'
    assert output['metadata']['index'] == str(tmp_path / 'calls.vcf.gz.tbi')
    for node in [{'params': {'model_type': 'PACBIO'}}, {'advanced': '--dry_run=true'}, {'params': {'extra': '--output_vcf=/tmp/other'}}]:
        with pytest.raises(ValueError):
            compiler.compile_tool(spec, node, inputs, tmp_path)
    inputs['alignment'][0]['metadata'].clear()
    with pytest.raises(ValueError, match='companion bindings'):
        compiler.compile_tool(spec, {}, inputs, tmp_path)


def test_compiler_already_binds_real_bgzf_gzi_adjacent_to_the_reference(tmp_path):
    bam, bai, plain, _ = alignment(tmp_path)
    reference = tmp_path / 'reference.fa.gz'
    pysam.tabix_compress(str(plain), str(reference), force=True)
    pysam.faidx(str(reference))
    inputs = {'alignment': [{'path': str(bam), 'format': 'INDEXED_BAM', 'metadata': {'index': str(bai)}}],
              'reference': [{'path': str(reference), 'format': 'FASTA',
                  'metadata': {'index': str(reference) + '.fai', 'index_paths': [str(reference) + '.gzi']}}]}
    plan = compiler.compile_tool(registry.BASE['deepvariant'], {}, inputs, tmp_path)
    assert len(plan['bindings']) == 5
    bindings = dict(plan['bindings'])
    assert bindings[str(reference) + '.gzi'] == bindings[str(reference)] + '.gzi'


def release():
    """Validator-only record; never scientific execution evidence."""
    outputs = [{'name': name, 'bytes': 10, 'sha256': 'a' * 64} for name in ['calls.vcf.gz', 'calls.vcf.gz.tbi', 'calls.runtime.json']]
    cases = [{'id': ident, 'status': 'passed', 'assertions': {'nonzero_exit': True}} for ident in sorted(CASES - {'wgs-truth-control'})]
    cases.append({'id': 'wgs-truth-control', 'status': 'passed', 'outputs': outputs,
        'assertions': {'truth_allele_matches': 10, 'variant_records': 10, 'indexed_records': 10, 'input_staging_symlinks': 5,
                       'reference_bgzf': True, 'explicit_reference_gzi': True, 'reference_lossless': True}})
    value={'family': FAMILY, 'version': VERSION, 'upstream_image': UPSTREAM_IMAGE,
        'image': '123456789012.dkr.ecr.us-east-1.amazonaws.com/openngs-demo@sha256:' + 'b' * 64,
        'source_sha256': 'c' * 64, 'requirements_sha256': REQUIREMENTS_SHA256,
        'fixture_manifest_sha256': FIXTURE_MANIFEST_SHA256, 'models': ['WGS'], 'tools': ['deepvariant'],
        'qualification': {'version': VERSION, 'upstream_image': UPSTREAM_IMAGE, 'cases': cases,
                          'prepared_reference': {'source_sha256': REFERENCE_SHA256, 'uncompressed_sha256': REFERENCE_SHA256,
                                                 'sha256': 'f' * 64, 'gzi_sha256': '1' * 64},
                          'models': {'WGS': {name:[{'path':path,'bytes':size,'sha256':sha} for path,(size,sha) in files.items()]
                                             for name,files in MODEL_FILES.items()}}, 'dependency_lock_sha256': REQUIREMENTS_SHA256}}
    value['isolation_qualification']={'image':value['image'],'source_sha256':'e'*64,'restrictions':ISOLATION,'cases':cases}
    return value


def test_publication_requires_complete_model_positive_and_actual_rejection_evidence():
    assert validate_release(release())['tools'] == ['deepvariant']
    changes = [lambda r: r.update(models=['WGS', 'WES']),
        lambda r: r['qualification']['cases'].pop(),
        lambda r: r['qualification']['cases'][0]['assertions'].update(nonzero_exit=False),
        lambda r: r['qualification']['cases'][-1]['assertions'].update(truth_allele_matches=0),
        lambda r: r['qualification']['cases'][-1]['assertions'].update(indexed_records=9),
        lambda r: r['qualification']['cases'][-1]['outputs'].pop(),
        lambda r: r.update(requirements_sha256='d' * 64),
        lambda r: r.pop('isolation_qualification'),
        lambda r: r['isolation_qualification'].update(image='different-image'),
        lambda r: r['qualification']['cases'][-1]['assertions'].update(input_staging_symlinks=0),
        lambda r: r['qualification']['cases'][-1]['assertions'].update(reference_bgzf=False),
        lambda r: r['qualification']['cases'][-1]['assertions'].pop('explicit_reference_gzi'),
        lambda r: r['qualification']['cases'][-1]['assertions'].update(reference_lossless=False),
        lambda r: r['qualification']['prepared_reference'].update(uncompressed_sha256='0' * 64),
        lambda r: r['qualification']['prepared_reference'].pop('gzi_sha256'),
        lambda r: r['qualification']['models']['WGS']['wgs'][0].update(sha256='0'*64)]
    for change in changes:
        bad = copy.deepcopy(release())
        change(bad)
        with pytest.raises(ValueError):
            validate_release(bad)


def test_legacy_five_case_plain_fasta_release_and_candidate_remain_valid():
    value = release()
    cases = value['qualification']['cases']
    cases[:] = [case for case in cases if case['id'] in LEGACY_CASES]
    positive = cases[-1]['assertions']
    for key in ['reference_bgzf', 'explicit_reference_gzi', 'reference_lossless']:
        positive.pop(key)
    positive['input_staging_symlinks'] = 4
    value['qualification'].pop('prepared_reference')
    assert validate_release(value) is value
    value.pop('isolation_qualification')
    assert validate_release(value, require_isolation=False) is value


def test_bgzf_proof_cannot_drop_missing_gzi_control_to_appear_legacy():
    value = release()
    value['qualification']['cases'][:] = [case for case in value['qualification']['cases'] if case['id'] != 'missing-gzi']
    with pytest.raises(ValueError, match='additional missing-GZI rejection'):
        validate_release(value, require_isolation=False)


def test_exact_fixture_and_dependency_files_match_reviewed_publication_identity():
    assert hashlib.sha256(Path('infra/deepvariant-runtime.json').read_bytes()).hexdigest() == FIXTURE_MANIFEST_SHA256
    assert hashlib.sha256(Path('infra/deepvariant-requirements.lock').read_bytes()).hexdigest() == REQUIREMENTS_SHA256
    recipe = json.loads(Path('infra/deepvariant-runtime.json').read_text())
    assert recipe['upstream_image'] == UPSTREAM_IMAGE and recipe['models'] == ['WGS']


def test_separate_tenant_definition_preserves_isolation_and_existing_runtime():
    from infra.research_stack import template
    core = '123456789012.dkr.ecr.us-east-1.amazonaws.com/openngs-demo@sha256:' + 'd' * 64
    result = template('subnet', 'security', 'execution', core, 'repo', 'shared', 'key',
                      'boundary', 'https://example.test', 'topic', deepvariant=release())
    original = result['Resources']['Definition']['Properties']['ContainerProperties']
    separate = result['Resources']['DeepVariantDefinition']['Properties']['ContainerProperties']
    assert original['Image'] == core and separate['Image'] == release()['image']
    assert separate['Command'] == ['/usr/bin/python3', '/opt/studio/cloud_worker.py']
    for key in ['ReadonlyRootFilesystem', 'User', 'EphemeralStorage', 'MountPoints', 'JobRoleArn', 'NetworkConfiguration']:
        assert separate[key] == original[key]
    assert separate['ReadonlyRootFilesystem'] and separate['EphemeralStorage']['SizeInGiB'] == 200
    assert json.loads(result['Outputs']['DeepVariantModels']['Value']) == ['WGS']
    invalid = release()
    invalid['qualification']['cases'][-1]['assertions']['truth_allele_matches'] = 0
    with pytest.raises(ValueError, match='actual model output'):
        template('subnet', 'security', 'execution', core, 'repo', 'shared', 'key',
                 'boundary', 'https://example.test', 'topic', deepvariant=invalid)


def test_tenant_route_requires_published_proof_with_the_exact_same_image(client):
    from backend import db, provisioning
    proof = release()
    outputs = {'DeepVariantImage': proof['image'], 'DeepVariantDefinition': 'reviewed-definition',
               'DeepVariantTools': '["deepvariant"]', 'DeepVariantModels': '["WGS"]',
               'DeepVariantSource': proof['source_sha256']}
    with pytest.raises(ValueError, match='exact published image'):
        provisioning.deepvariant_routes(outputs)
    with db.tx() as connection:
        connection.execute('INSERT INTO controls VALUES(?,?)', ('runtime-release:' + FAMILY, db.encode(proof)))
    route = provisioning.deepvariant_routes(outputs)['deepvariant']
    assert route['source_grants_version'] == 1 and route['qualified_models'] == ['WGS']
    assert route['image'] == proof['image']
    for field, value in [('DeepVariantImage', proof['image'][:-1] + 'f'),
                         ('DeepVariantSource', 'e' * 64), ('DeepVariantModels', '["WGS","WES"]')]:
        with pytest.raises(ValueError, match='exact published image'):
            provisioning.deepvariant_routes({**outputs, field: value})
    assert provisioning.deepvariant_routes({}) == {}
