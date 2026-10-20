from backend import db, migration


def seed(uid, count=205):
    with db.tx() as c:
        for i in range(count):
            c.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                      (f'ds_page_{i:03}',uid,None,f'file {i:03}.vcf',10,'a'*64,
                       'Ready' if i%2==0 else 'Verifying',f'tenants/{uid}/{i}',
                       'v1','{}',i//3,i//3))
        c.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                  ('ds_foreign','other-owner',None,'private foreign file',50,'b'*64,
                   'Ready','foreign','foreign-version','{}',99999,99999))


def test_keyset_reaches_every_owned_dataset_without_duplicates(client,owner):
    seed(owner['id'])
    identifiers=[];after=''
    while True:
        response=client.get('/api/operations/datasets',params={'limit':71,'after':after})
        assert response.status_code==200,response.text
        page=response.json()
        assert page['total']==205 and len(page['datasets'])<=71
        identifiers.extend(x['id'] for x in page['datasets'])
        after=page['next_cursor']
        if not after:
            break
    assert len(identifiers)==len(set(identifiers))==205
    assert 'ds_foreign' not in identifiers
    assert identifiers==[f'ds_page_{i:03}' for i in reversed(range(205))]
    assert client.get('/api/operations/datasets',params={'limit':101}).status_code==422


def test_search_cursor_is_bound_to_query_and_ready_filter(client,owner):
    seed(owner['id'])
    page=client.get('/api/operations/datasets',params={'q':'FILE 0','state':'Ready','limit':7}).json()
    assert page['total']==50 and all(row['state']=='Ready' for row in page['datasets'])
    second=client.get('/api/operations/datasets',params={'q':'FILE 0','state':'Ready','limit':7,'after':page['next_cursor']})
    assert second.status_code==200
    assert not set(x['id'] for x in page['datasets'])&set(x['id'] for x in second.json()['datasets'])
    invalid=client.get('/api/operations/datasets',params={'q':'different','state':'Ready','after':page['next_cursor']})
    assert invalid.status_code==422
    assert client.get('/api/operations/datasets',params={'after':'not-a-valid-cursor'}).status_code==422


def test_alias_search_and_local_inventory_exclude_only_verified_migrations(client,owner):
    seed(owner['id'],2)
    with db.tx() as c:
        for i,name in enumerate(['Exact_100% reference.fa','still-local.fa']):
            ident='alias_paging_'+str(i)
            c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (ident,owner['id'],'/source/'+name,name,'FASTA',10,1,'reference','GRCh38','{}'))
            c.execute('INSERT INTO migration_assets VALUES(?,?,?,?,?,?)',
                      (ident,owner['id'],f'ds_page_{i:03}',ident,'/source/'+name,'{}'))
    found=client.get('/api/operations/datasets',params={'q':'100% reference'}).json()
    assert [d['id'] for d in found['datasets']]==['ds_page_000']
    assert found['preparing_inventory']=={'files':1,'bytes':10}
    # SQL wildcard characters in an entered filename are literal.
    assert client.get('/api/operations/datasets',params={'q':'Exact_100%'}).json()['total']==1
    assert client.get('/api/operations/datasets',params={'q':'ExactX100Y'}).json()['total']==0
