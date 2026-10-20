import time
import pytest
from backend import data,db


def add_asset(uid,name,family='gnomad',assembly='GRCh38'):
    aid=db.ident('asset_')
    with db.tx() as c:
        c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)',(aid,uid,'s3://verified-test-bucket/tenants/'+uid+'/datasets/'+aid,name,'VCF',100,time.time(),family,assembly,'{}'))
    return aid


def test_gnomad_genomes_chr22_is_not_suggested_as_exome_or_other_chromosome_replacement(owner):
    uid=owner['id'];aid=add_asset(uid,'gnomad.exomes.v4.1.sites.chr3.vcf.bgz')
    candidates=data.resolve(uid,'gnomad',asset_id=aid)
    public=next(x for x in candidates if x['kind']=='public')
    assert public['source_identity']['cohort']=='genomes' and public['source_identity']['scope']=='chr22'
    assert public['compatibility']=='different_identity' and not public['automatic_substitution_allowed']
    assert 'not a replacement' in public['reason']
    original=next(x for x in candidates if x['id']==aid)
    assert original['source_identity']['cohort']=='exomes' and original['source_identity']['scope']=='chr3'
    assert original['source_identity']['release']=='4.1'


def test_mutable_clinvar_url_never_matches_original_snapshot(owner):
    uid=owner['id'];aid=add_asset(uid,'clinvar.vcf.gz','clinvar')
    candidates=data.resolve(uid,'clinvar',asset_id=aid)
    public=next(x for x in candidates if x['kind']=='public')
    assert public['compatibility']=='unverified_mutable_release'
    assert public['source_identity']['mutable'] and not public['automatic_substitution_allowed']
    assert 'release date unpinned' in public['source_identity']['release']
    original=next(x for x in candidates if x['id']==aid)
    assert original['source_identity']['release']=='unrecorded original snapshot'


def test_resolution_requires_owned_target_and_does_not_auto_select_family_only(owner):
    uid=owner['id'];aid=add_asset(uid,'gnomad.genomes.v4.1.sites.chr22.vcf.bgz')
    with pytest.raises(ValueError,match='workspace'):data.resolve('foreign','gnomad',asset_id=aid)
    with pytest.raises(ValueError,match='Unknown source'):data.resolve(uid,'gnomad',source_id='ncbi-clinvar')
    assert all(x['compatibility']=='family_only_not_interchangeable' for x in data.resolve(uid,'gnomad'))
    candidates=data.resolve(uid,'gnomad',source_id='gnomad-v4-chr22')
    same=next(x for x in candidates if x['id']==aid)
    assert same['compatibility']=='declared_identity_requires_verification'
    assert not same['automatic_substitution_allowed']
