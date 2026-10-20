"""Bounded format runtime contract, independent of application publication state."""
FAMILY = 'portable-formats-v1'
VERSION = '1.0.0'
BASE_IMAGE = '509505638328.dkr.ecr.us-east-1.amazonaws.com/openngs-demo@sha256:a8b97ecfe7212926fceffabc9956f7f358969b2c1d7e5497c15a8340f61c3b75'
TOOLS = {'vcf.bcf','bcf.vcf','contig.rename','contig.rename.bed','gtf.bed','bed.gtf','intervals.bed','reference.dictionary'}
ASSERTIONS = {
    'vcf.bcf': {'records':6,'samples':['SAMPLE_A','SAMPLE_B'],'multiallelic_records':1,
        'full_record_roundtrip':True,'header_schema_preserved':True,'undeclared_header_rejected':True},
    'bcf.vcf': {'records':6,'samples':['SAMPLE_A','SAMPLE_B'],'multiallelic_records':1,
        'full_record_roundtrip':True,'header_schema_preserved':True},
    'contig.rename': {'records':6,'breakend_orientations':4,'multiallelic_genotypes_preserved':True,
        'coordinates_unchanged':True,'partner_contigs_renamed':True,'header_attributes_preserved':True,
        'collisions_rejected':True,'malformed_breakend_rejected':True,'unknown_style_rejected':True,'liftover':False},
    'contig.rename.bed': {'records':3,'all_noncontig_columns_preserved':True,'bed12_blocks_preserved':True,
        'directives_preserved':True,'coordinate_system_unchanged':True,'collision_rejected':True,'liftover':False},
    'gtf.bed': {'records':3,'zero_based_half_open_intervals':[[0,1],[9,12],[20,22]],'strands':['+','-','.'],
        'malformed_columns_rejected':True,'invalid_strand_rejected':True,'gff_fasta_tail_not_parsed':True},
    'bed.gtf': {'records':4,'one_based_closed_intervals':[[1,1],[10,12],[21,23],[28,30]],'strands':['+','-','-','-'],
        'bed12_exons_split':True,'generated_ids_group_blocks':True,'zero_length_rejected':True,'invalid_blocks_rejected':True},
    'intervals.bed': {'records':3,'zero_based_half_open_intervals':[[0,1],[9,12],[20,22]],
        'typed_dictionary_rejected':True,'legacy_header_only_rejected':True},
    'reference.dictionary': {'format':'REFERENCE_DICTIONARY','contigs':['1','2'],'lengths':[32,24],
        'uppercase_sequence_md5_verified':True,'contains_no_interval_rows':True},
}
ISOLATION = {'uid':1000,'root_write_denied':True,'input_write_denied':True,'effective_capabilities_zero':True,
             'root_mount_read_only':True,'input_mount_read_only':True,
             'no_new_privileges':True,'no_default_ipv4_route':True,'work_writable':True,'tmp_writable':True}
SOURCE_FILES = ['backend/portable.py','backend/scientific.py','backend/format_operators.py',
                'backend/format_contract.py','scripts/qualify_portable_formats.py','infra/portable-formats.Dockerfile']
SCOPE = ('Eight format and interval contracts on tiny known controls, including negative cases. '
         'Contig renaming does not perform liftover. GTF/BED conversion projects intervals and '
         'does not preserve complete annotation attributes or infer transcript biology. '
         'No full-assay, scientific accuracy or clinical validation claim.')
