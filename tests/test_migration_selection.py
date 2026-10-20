import copy
import pytest
from scripts.migrate_inventory import transfer_selection


def test_coordinated_defer_preserves_plan_and_default_priority():
    plan={'objects':[{'sha256':'a'*64,'bytes':10}, {'sha256':'b'*64,'bytes':20,'cloud_source':{'bucket':'public'}}, {'sha256':'c'*64,'bytes':1}]}
    before=copy.deepcopy(plan)
    assert [o['sha256'] for o in transfer_selection(plan)]==['b'*64,'c'*64,'a'*64]
    assert [o['sha256'] for o in transfer_selection(plan,defer_sha256=['b'*64])]==['c'*64,'a'*64]
    assert [o['sha256'] for o in transfer_selection(plan,limit=2,defer_sha256=['c'*64])]==['a'*64]
    assert plan==before


@pytest.mark.parametrize('defer',[['unknown'],['d'*64],['../'+'a'*64]])
def test_defer_rejects_unknown_or_non_content_identity(defer):
    with pytest.raises(ValueError,match='exact registered'):
        transfer_selection({'objects':[{'sha256':'a'*64,'bytes':1}]},defer_sha256=defer)
