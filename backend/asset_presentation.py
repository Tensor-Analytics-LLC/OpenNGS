"""Evidence-based display roles; never promote a cache fragment to a database."""
import json
import re
from .public_resource_metadata import enrich as enrich_resource

INDEX_SUFFIX = re.compile(r'\.(?:bai|crai|tbi|csi|fai|gzi|dict|amb|ann|bwt(?:\.2bit\.64)?|pac|sa|bt2l?|ht2l?)$', re.I)
VEP_RANGE = re.compile(r'^(\d+)-(\d+)(_reg)?\.gz$', re.I)


def describe(row, source_path=''):
    row = enrich_resource(row)
    raw = row.get('metadata') or {}
    meta = json.loads(raw) if isinstance(raw, str) else raw
    name = str(row.get('name') or '').replace('\\', '/').rsplit('/', 1)[-1]
    family = str(row.get('family') or meta.get('family') or '').lower()
    fmt = row.get('format') or meta.get('format') or ''
    identity = meta.get('source_identity') if isinstance(meta.get('source_identity'), dict) else {}
    result = {'basename': name, 'family': family, 'format': fmt,
              'assembly': row.get('assembly') or meta.get('assembly') or '',
              'release': str(meta.get('release') or identity.get('release') or ''),
              'primary_eligible': True, 'role': '', 'component': False}
    resource = meta.get('public_resource')
    if resource:
        result.update(public_resource=resource, assembly_independent=resource['assembly_independent'])
        if resource['family']=='hpo':
            result.update(assembly='', role='Phenotype ontology', assembly_label='Assembly independent')
    if not result['release']:
        pattern = {'gnomad': r'gnomad\.(?:exomes|genomes)\.v(\d+(?:\.\d+)+)\.',
                   'dbnsfp': r'dbnsfp[._-]?(\d+(?:\.\d+)*[ac]?)',
                   'giab': r'_v(\d+(?:\.\d+)+)_benchmark'}.get(family)
        match = re.search(pattern, name, re.I) if pattern else None
        if match:
            result['release'] = match[1]
    if fmt == 'INDEX' or INDEX_SUFFIX.search(name):
        result.update(role='Companion index', primary_eligible=False, component=True)
    if family == 'vep':
        # Only retained scientific path segments become public metadata.
        # The host root, username and unrelated directory names are discarded.
        path = str(source_path or meta.get('original_path') or row.get('path') or '').replace('\\', '/')
        match = re.search(r'(?:^|/)(homo_sapiens(?:_merged|_refseq)?)/(\d+)_(GRCh3[78])(?:/([^/]+))?', path)
        if match:
            result.update(species=match[1], release=match[2], assembly=match[3])
            if match[4] and re.fullmatch(r'(?:chr)?(?:[0-9]+|X|Y|M|MT|[A-Z]{1,4}_?\d+(?:\.\d+)?)', match[4]):
                result['contig'] = match[4]
        span = VEP_RANGE.fullmatch(name)
        if span:
            result.update(role='Regulatory cache fragment' if span[3] else 'Transcript cache fragment',
                          interval=f'{span[1]}–{span[2]}', primary_eligible=False, component=True)
        elif name == 'all_vars.gz':
            result.update(role='Variant cache fragment', primary_eligible=False, component=True)
        elif name in {'info.txt', 'chr_synonyms.txt'}:
            result.update(role='Cache metadata' if name == 'info.txt' else 'Contig synonym table',
                          primary_eligible=False, component=True)
        elif not result['component']:
            receipt = meta.get('bundle_verification') if isinstance(meta.get('bundle_verification'), dict) else {}
            digest = meta.get('sha256')
            verified = (receipt.get('state') == 'verified-complete' and receipt.get('tool') == 'vep'
                        and bool(re.fullmatch(r'[a-f0-9]{64}', str(digest or '')))
                        and receipt.get('sha256') == digest
                        and bool(re.fullmatch(r'[a-f0-9]{64}', str(receipt.get('manifest_sha256') or ''))))
            result.update(role='Verified cache bundle' if verified else 'Cache archive · completeness unverified' if name.endswith(('.tar.gz','.tar','.tgz','.zip')) else 'Cache component',
                          primary_eligible=verified, component=not verified)
    if family == 'exomiser' and not result['component']:
        lower = name.lower()
        if lower.startswith(('test-analysis', 'readme', 'licence', 'license')) or lower.endswith(('.yml','.yaml','.properties','.conf','.ini')):
            result.update(role='Example configuration' if lower.startswith('test-analysis') else 'Licence document' if lower.startswith(('licence','license')) else 'Configuration / documentation',
                          primary_eligible=False, component=True)
        elif re.fullmatch(r'\d+_(?:hg19|hg38)_(?:variants|genome)\.mv\.db', lower):
            result.update(role='Variant data component' if '_variants.' in lower else 'Genome data component', release=lower.split('_')[0])
        elif re.fullmatch(r'\d+_phenotype\.mv\.db', lower):
            result.update(role='Phenotype data component', release=lower.split('_')[0])
        elif re.fullmatch(r'\d+_(?:hg19|hg38|phenotype)\.zip', lower):
            result.update(role='Phenotype archive' if '_phenotype.' in lower else 'Genome archive', release=lower.split('_')[0])
        elif lower.startswith('exomiser-cli-') and lower.endswith('.zip'):
            result.update(role='Software distribution', primary_eligible=False, component=True)
        elif lower == 'all_sources_all_frequencies_genes_to_phenotype.txt':
            result['role'] = 'Gene–phenotype association table'
        elif lower == 'hp.obo':
            result['role'] = 'Phenotype ontology'
    return result
