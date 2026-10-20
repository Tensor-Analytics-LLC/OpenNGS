import os
import tempfile
from pathlib import Path

TEST_ROOT=Path(tempfile.mkdtemp(prefix='openngs-tests-'))
os.environ['NGS_STATE_DIR']=str(TEST_ROOT/'state')
os.environ['NGS_DATA_DIR']=str(TEST_ROOT/'data')

import pytest
from fastapi.testclient import TestClient
from backend.api import app,AUTH_ATTEMPTS
from backend import db

@pytest.fixture
def client():
    AUTH_ATTEMPTS.clear()
    # This database lives in a unique test-only temporary directory.
    with db.tx() as c:
        for table in ['research_allowances','usage_holds','download_holds','cost_allocations','mfa','multipart_transfers','datasets','cost_reservations','billing_checks','sessions','settings','graphs','events','attempts','artifacts','runs','assets','scans','secrets','transfers','custom_tools','quarantine','controls','audit','users']:c.execute('DELETE FROM '+table)
    with TestClient(app,base_url='http://testserver') as c:
        c.headers.update({'Origin':'http://testserver','X-NGS-Client':'studio'})
        yield c

@pytest.fixture
def owner(client):
    r=client.post('/api/auth/signup',json={'email':'owner@example.test','password':'Test-only-password-7341!'});assert r.status_code==200,r.text
    client.headers['X-CSRF-Token']=r.json()['csrf']
    return r.json()['user']
