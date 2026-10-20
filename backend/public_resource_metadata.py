"""Read-time facts for reviewed exact public files; never rewrite source records."""
import copy
import json

HPO_TERMS = 'https://hpo.jax.org/license'
HPO_CITATION = 'https://doi.org/10.1093/nar/gkad1005'
KNOWN = {
    'a5092cbdf605f568403cf7380d9173014015692433b2cc631bc5c1b053876b1b':
        ('hpo', 'hp.obo', '2026-06-23', 'Reviewed HPO data-version header'),
    '3b646565695329aa399e937883c68d5d424d0df5eaab2f22baa0e08d44fdbe87':
        ('hpo', 'hp.json', '2026-06-23', 'Reviewed HPO JSON version metadata'),
    '1fb70c6fd2a66df441b10c389a122ff476e0f7c39edf58967848eed5bc599050':
        ('clinvar', 'clinvar.vcf.gz', '2026-06-27', 'Reviewed VCF fileDate=20260627 header'),
}


def metadata(row):
    value = row.get('metadata') or {}
    return json.loads(value) if isinstance(value, str) else value


def describe(row):
    meta = metadata(row)
    sha = str(row.get('digest') or meta.get('sha256') or row.get('sha256') or '').removeprefix('sha256:')
    observed = KNOWN.get(sha)
    if not observed:
        return None
    family, name, release, header = observed
    result = {'family': family, 'canonical_name': name, 'release': release, 'sha256': sha,
              'release_basis': header + '; exact SHA-256 match', 'assembly_independent': family == 'hpo'}
    if family == 'hpo':
        result.update(name='Human Phenotype Ontology', provider='Human Phenotype Ontology Consortium',
            terms_url=HPO_TERMS, citation_url=HPO_CITATION,
            citation='Gargano et al., Nucleic Acids Research (2024), doi:10.1093/nar/gkad1005',
            acknowledgment='This service uses the Human Phenotype Ontology (version ' + release + ').',
            conditions='Acknowledge and cite the HPO Consortium, display the file release, and retain the ontology content and relationships unchanged.',
            terms_basis_url='https://raw.githubusercontent.com/TheJacksonLaboratory/hpo-web/main/src/app/static/resources/license/license.component.html')
    else:
        result.update(name='ClinVar', provider='NCBI ClinVar',
            terms_url='https://www.ncbi.nlm.nih.gov/clinvar/docs/maintenance_use/',
            citation_url='https://pubmed.ncbi.nlm.nih.gov/29165669/',
            citation='Landrum et al., Nucleic Acids Research (2018), PMID:29165669',
            acknowledgment='ClinVar is acknowledged as the source of this release.',
            conditions='Retain release information and attribute ClinVar when copying or distributing its data.')
    return result


def enrich(row):
    """Return a copy with scientific facts, retaining original storage provenance."""
    result = dict(row)
    string_metadata = isinstance(row.get('metadata'), str)
    original_meta = metadata(row)
    resource = describe({**row, 'metadata': original_meta})
    if not resource and 'public_resource' not in original_meta:
        return result
    meta = copy.deepcopy(original_meta)
    # A source annotation cannot self-certify an exact public-file review.
    meta.pop('public_resource', None)
    if resource:
        original_release = meta.get('release')
        if original_release and original_release != resource['release']:
            meta.setdefault('original_declared_release', original_release)
        meta.update(public_resource=resource, release=resource['release'])
        result['family'] = resource['family']
        if resource['assembly_independent']:
            original = row.get('assembly') or meta.get('assembly')
            if original:
                meta.setdefault('original_storage_assembly', original)
            result['assembly'] = None
            meta['assembly'] = None
    result['metadata'] = json.dumps(meta, sort_keys=True, separators=(',', ':')) if string_metadata else meta
    return result
