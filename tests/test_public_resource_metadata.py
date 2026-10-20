import copy
import json
import time
import pytest
from backend import asset_rights, catalogue_view, config, data, db, entitlements, migration
from backend.asset_listing import list_assets
from backend.asset_presentation import describe as presentation
from backend.public_resource_metadata import KNOWN, describe, enrich

HPO_OBO='a5092cbdf605f568403cf7380d9173014015692433b2cc631bc5c1b053876b1b'
HPO_JSON='3b646565695329aa399e937883c68d5d424d0df5eaab2f22baa0e08d44fdbe87'
CLINVAR='1fb70c6fd2a66df441b10c389a122ff476e0f7c39edf58967848eed5bc599050'


def row(sha=HPO_OBO, name='hp.obo', family='hpo'):
    return {'id':'exact-resource','name':name,'family':family,'assembly':'GRCh38','format':'DATABASE',
            'bytes':123,'path':'s3://private/exact','metadata':{'sha256':sha,'version_id':'frozen-v1',
                'assembly':'GRCh38','original_note':'Preserve the original storage record'}}


@pytest.mark.parametrize(('sha','name'),[(HPO_OBO,'hp.obo'),(HPO_JSON,'hp.json')])
def test_hpo_facts_are_exact_content_and_assembly_independent_without_source_mutation(sha,name):
    source=row(sha,name);before=copy.deepcopy(source);value=enrich(source)
    assert source==before
    assert value['id']==source['id'] and value['path']==source['path'] and value['bytes']==source['bytes']
    assert value['assembly'] is None and value['metadata']['assembly'] is None
    assert value['metadata']['original_storage_assembly']=='GRCh38'
    assert value['metadata']['original_note']==before['metadata']['original_note']
    assert value['metadata']['release']=='2026-06-23'
    assert value['metadata']['version_id']=='frozen-v1'
    p=presentation(source)
    assert p['assembly_independent'] and p['assembly']=='' and p['role']=='Phenotype ontology'
    assert p['release']=='2026-06-23'
    assert '2026-06-23' in value['metadata']['public_resource']['acknowledgment']
    assert describe(row(sha,'renamed.bin','cloud'))['release']=='2026-06-23'


@pytest.mark.parametrize('name',['hp.obo','hp.json','clinvar.vcf.gz'])
def test_same_basename_or_self_declared_review_cannot_assign_a_known_release(name):
    value=row('b'*64,name)
    value['metadata']['public_resource']={'release':'2026-06-23','sha256':'b'*64}
    assert describe(value) is None
    observed=enrich(value)
    assert 'public_resource' not in observed['metadata']
    assert 'release' not in observed['metadata']
    assert presentation(observed)['release']==''
    assert observed['assembly']=='GRCh38'


def test_clinvar_header_date_binds_only_the_reviewed_digest():
    value=enrich(row(CLINVAR,'clinvar.vcf.gz','clinvar'))
    assert value['metadata']['release']=='2026-06-27'
    assert 'fileDate=20260627' in value['metadata']['public_resource']['release_basis']
    assert value['assembly']=='GRCh38'
    assert not asset_rights.describe(value)['required']
    assert asset_rights.describe(value)['release']=='2026-06-27'
    assert all(len(sha)==64 for sha in KNOWN)


def test_exact_hpo_is_public_conditional_use_without_an_administrator_grant(client,owner):
    for sha,name in [(HPO_OBO,'hp.obo'),(HPO_JSON,'hp.json')]:
        value=row(sha,name)
        rights=asset_rights.disclosure(owner['id'],value)
        assert rights['label']=='Public resource terms' and rights['release']=='2026-06-23'
        assert not rights['required'] and rights['execution_allowed']
        assert rights['requirements']==[] and asset_rights.require(owner['id'],value)==[]
        assert rights['permitted_uses']==[]
        assert 'unchanged' in rights['reason'] and 'cite' in rights['reason']
    assert db.query('SELECT * FROM data_rights_grants WHERE user_id=?',(owner['id'],))==[]
    assert asset_rights.describe(row('b'*64))['required']
    for name,family in [('dbNSFP5.3.1a_grch38.gz','dbnsfp'),('spliceai_scores.raw.snv.hg38.vcf.gz','spliceai')]:
        rights=asset_rights.disclosure(owner['id'],row('c'*64,name,family))
        assert rights['required'] and not rights['execution_allowed']
        assert rights['label']=='Academic / noncommercial'


def test_old_generic_hpo_review_is_not_a_fake_attestation_and_score_restrictions_survive(client,owner):
    value=row();value['metadata']['data_rights']=[{'family':'hpo','sha256':HPO_OBO,'identity':'old-hpo-review','release':'hp.obo'}]
    assert asset_rights.disclosure(owner['id'],value)['requirements']==[]
    restricted={'family':'dbnsfp','sha256':'d'*64,'identity':'restricted-score','release':'5.3.1a'}
    value['metadata']['data_rights'].append(restricted)
    rights=asset_rights.disclosure(owner['id'],value)
    assert rights['requirements']==[{**restricted,'acknowledged':False,'acknowledged_use':None}]
    assert not rights['execution_allowed']


def test_ready_migration_alias_and_catalogue_get_semantics_without_persisting_enrichment(client,owner,monkeypatch):
    uid=owner['id'];now=time.time();canonical=row();canonical.update(id='hpo-primary',user_id=uid,mtime=now)
    canonical['metadata'].update(cloud_dataset_id='hpo-dataset',dataset_id='hpo-dataset',uploaded=True)
    alias=copy.deepcopy(canonical);alias.update(id='hpo-alias',name='hp-copy.obo',path='/old/storage/GRCh38/hp.obo')
    alias['metadata'].pop('sha256');alias['metadata'].pop('version_id')
    fields=['id','user_id','path','name','format','bytes','mtime','family','assembly']
    with db.tx() as c:
        for value in [canonical,alias]:
            c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)',tuple(value[k] for k in fields)+(db.encode(value['metadata']),))
            c.execute('INSERT INTO migration_assets VALUES(?,?,?,?,?,?)',(value['id'],uid,'hpo-dataset','hpo-primary',value['path'],db.encode(value)))
        c.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('hpo-dataset',uid,'hpo-primary','hp.obo',123,HPO_OBO,'Ready','exact','frozen-v1',db.encode({'assembly':'GRCh38','unchanged_manifest':True}),now,now))
    originals=db.query('SELECT * FROM assets ORDER BY id');manifest=db.query('SELECT manifest FROM datasets',one=True)['manifest']
    resolved=migration.resolve_asset(uid,db.query('SELECT * FROM assets WHERE id=?',('hpo-alias',),one=True))
    assert resolved['id']=='hpo-alias' and resolved['path']==canonical['path'] and resolved['assembly'] is None
    assert json.loads(resolved['metadata'])['original_storage_assembly']=='GRCh38'
    assets=list_assets(uid,cloud=True,node_id='resource.hpo')
    assert len(assets)==2 and all(a['assembly'] is None for a in assets)
    assert all(a['data_rights']['execution_allowed'] and not a['data_rights']['requirements'] for a in assets)
    assert list_assets(uid,cloud=True,filters={'release':'2026-06-23'})
    assert not list_assets(uid,cloud=True,filters={'assembly':'GRCh38'})
    monkeypatch.setattr(config,'CLOUD',True)
    monkeypatch.setattr(entitlements,'restricted',lambda _:False)
    monkeypatch.setattr(data,'settings',lambda _: {'rights_approved':[],'research_profile':{'state':'Ready'}})
    catalogue_view.invalidate()
    try:
        node=next(n for n in catalogue_view.view(uid)['nodes'] if n['id']=='resource.hpo')
        assert node['asset_authorized_count']==2 and node['availability']['state']=='Ready'
    finally:catalogue_view.invalidate()
    assert db.query('SELECT * FROM assets ORDER BY id')==originals
    assert db.query('SELECT manifest FROM datasets',one=True)['manifest']==manifest
    assert db.query('SELECT * FROM data_rights_grants WHERE user_id=?',(uid,))==[]
