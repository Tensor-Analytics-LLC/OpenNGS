"""Bounded DeepVariant WGS contract; the upstream Python/TensorFlow stays isolated."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess


def reference_gzi(reference):
    """Require the bound BGZF index before htslib can silently create one."""
    with Path(reference).open('rb') as source:
        compressed = source.read(2) == b'\x1f\x8b'
    if compressed:
        index = Path(str(reference) + '.gzi')
        if not index.is_file():
            raise ValueError('DeepVariant requires an explicit adjacent GZI companion for a BGZF reference')
        return index
    return None


def validate_inputs(reads, index, reference, reference_index, region=None):
    import pysam
    for path in [reads, index, reference, reference_index]:
        if not Path(path).is_file():
            raise ValueError('DeepVariant requires explicit BAM/BAI and FASTA/FAI companion files')
    gzi = reference_gzi(reference)
    with pysam.FastaFile(str(reference), filepath_index=str(reference_index),
                        **({'filepath_index_compressed': str(gzi)} if gzi else {})) as fasta:
        lengths = dict(zip(fasta.references, fasta.lengths))
    with pysam.AlignmentFile(str(reads), 'rb', index_filename=str(index)) as bam:
        if not bam.is_bam or not bam.has_index():
            raise ValueError('DeepVariant requires an indexed BAM')
        bam.check_index()
        header = bam.header.to_dict()
        if header.get('HD', {}).get('SO') != 'coordinate':
            raise ValueError('DeepVariant requires coordinate-sorted BAM')
        mapped_contigs = {row.contig for row in bam.get_index_statistics() if row.mapped}
        if not mapped_contigs or any(lengths.get(name) != size for name, size in zip(bam.references, bam.lengths) if name in mapped_contigs):
            raise ValueError('Alignment contigs and lengths must exactly match the bound reference')
        samples = {row.get('SM') for row in header.get('RG', [])}
        if len(samples) != 1 or not next(iter(samples)):
            raise ValueError('DeepVariant requires exactly one explicit BAM read-group sample')
        sample = next(iter(samples))
    if region:
        match = re.fullmatch(r'([A-Za-z0-9_.-]+):(\d+)-(\d+)', region)
        if not match or match[1] not in lengths or not 1 <= int(match[2]) <= int(match[3]) <= lengths[match[1]]:
            raise ValueError('DeepVariant region must be one valid one-based closed contig:start-end interval')
    return {'sample': sample, 'reference_contigs': len(lengths)}


def stage_inputs(args, staged, *, admitted_root='/inputs'):
    """Give native shell commands fixed names without copying large inputs."""
    sources = [(args.reads, 'reads.bam'), (args.index, 'reads.bam.bai'),
               (args.reference, 'reference.fa'), (args.reference_index, 'reference.fa.fai')]
    gzi = reference_gzi(args.reference)
    if gzi:
        sources.append((gzi, 'reference.fa.gzi'))
    admitted = Path(admitted_root).resolve(strict=True)
    resolved_sources = [(Path(source).resolve(strict=True), name) for source, name in sources]
    if any(not source.is_relative_to(admitted) or not source.is_file() for source, _ in resolved_sources):
        raise ValueError('DeepVariant staging accepts only admitted regular files under /inputs')
    staged.mkdir(exist_ok=True)
    # A plain reference must not inherit a prior compressed reference's index.
    for name in ['reads.bam', 'reads.bam.bai', 'reference.fa', 'reference.fa.fai', 'reference.fa.gzi']:
        target = staged / name
        if target.exists() or target.is_symlink():
            target.unlink()
    for source, name in resolved_sources:
        (staged / name).symlink_to(source)


def run(args):
    if args.model_type != 'WGS':
        raise ValueError('Only the DeepVariant 1.8.0 WGS model has a reviewed execution contract')
    if not 1 <= args.shards <= 16:
        raise ValueError('DeepVariant shard count must be between 1 and 16')
    validated = validate_inputs(args.reads, args.index, args.reference, args.reference_index, args.region)
    root = Path(args.work).resolve()
    root.mkdir(parents=True, exist_ok=True)
    staged = root / 'deepvariant-inputs'
    # The upstream launcher constructs internal shell commands. Canonical paths
    # keep arbitrary input filenames out of that shell, and advanced flags are
    # deliberately absent from this reviewed wrapper.
    # /inputs and /work are separate production volumes; include the admitted
    # adjacent GZI when compressed bytes are presented under reference.fa.
    stage_inputs(args, staged)
    intermediate = root / 'deepvariant-intermediate'
    intermediate.mkdir(exist_ok=True)
    output = root / 'calls.vcf.gz'
    env = {key: value for key, value in os.environ.items() if key not in ['PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV']}
    env.update(PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/conda/bin:/opt/conda/envs/bio/bin:/opt/deepvariant/bin',
               TMPDIR='/tmp', OMP_NUM_THREADS=str(args.shards), TF_NUM_INTRAOP_THREADS=str(args.shards),
               TF_NUM_INTEROP_THREADS='1')
    argv = ['/opt/deepvariant/bin/run_deepvariant', '--model_type=WGS',
            '--ref=' + str(staged / 'reference.fa'), '--reads=' + str(staged / 'reads.bam'),
            '--output_vcf=' + str(output), '--num_shards=' + str(args.shards),
            '--intermediate_results_dir=' + str(intermediate), '--postprocess_cpus=1']
    if args.region:
        argv.append('--regions=' + args.region)
    subprocess.run(argv, check=True, env=env)
    import pysam
    if not output.is_file() or not Path(str(output) + '.tbi').is_file():
        raise ValueError('DeepVariant did not produce both VCF and tabix companion')
    with pysam.VariantFile(str(output)) as vcf:
        if list(vcf.header.samples) != [validated['sample']]:
            raise ValueError('DeepVariant output sample does not match the input sample')
        count = sum(1 for _ in vcf)
    with pysam.TabixFile(str(output)) as tabix:
        indexed = sum(1 for contig in tabix.contigs for _ in tabix.fetch(contig))
    if indexed != count:
        raise ValueError('DeepVariant output index does not retrieve every VCF record')
    (root / 'calls.runtime.json').write_text(json.dumps({**validated, 'version': '1.8.0',
        'model': 'WGS', 'variant_records': count, 'tabix_records': indexed,
        'scope': 'Germline WGS model execution; no clinical or assay accuracy claim'}, indent=2))


def main():
    parser = argparse.ArgumentParser()
    for key in ['reads', 'index', 'reference', 'reference-index']:
        parser.add_argument('--' + key, required=True)
    parser.add_argument('--model-type', default='WGS')
    parser.add_argument('--shards', type=int, required=True)
    parser.add_argument('--region')
    parser.add_argument('--work', default='/work')
    run(parser.parse_args())


if __name__ == '__main__':
    main()
