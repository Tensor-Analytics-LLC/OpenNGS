import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from . import db
from .config import STATE, aws_session
from .data import settings

def password_hash(password):
    salt=secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,600000)
    return 'pbkdf2_sha256$600000$'+salt.hex()+'$'+digest.hex()

def verify_password(password,encoded):
    try:
        _,iterations,salt,digest=encoded.split('$')
        actual=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),int(iterations))
        return hmac.compare_digest(actual,bytes.fromhex(digest))
    except (ValueError,TypeError):return False

def session_for(token):
    if not token:return None
    hashed=hashlib.sha256(token.encode()).hexdigest()
    return db.query("SELECT u.*,s.csrf,s.expires FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>? AND u.role!='retired'",(hashed,time.time()),one=True)

def issue_session(c,uid):
    token=secrets.token_urlsafe(40);csrf=secrets.token_urlsafe(32)
    c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),uid,csrf,time.time()+12*3600))
    return token,csrf

def public_user(user):
    from .entitlements import limits
    allowance=limits(user['id']);result={**{k:user[k] for k in ['id','email','role','tier','frozen','quota_seconds','used_seconds','reserved_seconds']},'entitlements':allowance}
    from .config import CLOUD
    if CLOUD:
        from .budget import period
        rows=db.query("SELECT state,SUM(CASE WHEN state='reserved' THEN reserved ELSE used END) AS seconds FROM usage_holds WHERE user_id=? AND (month=? OR state='reserved') GROUP BY state",(user['id'],period()))
        result.update(lifetime_used_seconds=user['used_seconds'],quota_seconds=allowance['monthly_cpu_seconds'],used_seconds=sum(int(x['seconds']) for x in rows if x['state']!='reserved'),reserved_seconds=sum(int(x['seconds']) for x in rows if x['state']=='reserved'),quota_period=period())
    return result

def local_master():
    path=STATE/'vault.master'
    try:
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'wb') as f:f.write(secrets.token_bytes(32))
    except FileExistsError:pass
    if path.stat().st_mode & 0o077:raise ValueError('Vault master file permissions are unsafe')
    value=path.read_bytes()
    if len(value)!=32:raise ValueError('Invalid vault master key')
    return value

PROVIDERS={'customapi','cosmic','franklin','oncokb','openai','sentieon','dragen','hgmd','annovar'}

def put_secret(user,payload):
    provider=payload.get('provider');label=payload.get('label','').strip();value=payload.get('value','')
    bindings=payload.get('bindings',[])
    if user['frozen']:raise ValueError('Workspace is frozen')
    if provider not in PROVIDERS or not 1<=len(label)<=100 or not isinstance(value,str) or not 4<=len(value)<=16384:raise ValueError('Provider, label or credential is invalid')
    if not isinstance(bindings,list) or not all(isinstance(x,str) and len(x)<=100 for x in bindings):raise ValueError('Invalid node bindings')
    old=db.query('SELECT * FROM secrets WHERE user_id=? AND label=?',(user['id'],label),one=True)
    sid=old['id'] if old else db.ident('secret_')
    version=old['version']+1 if old else 1
    context={'tenant':user['id'],'secret':sid,'provider':provider,'version':str(version)}
    aad=db.encode(context).encode();kms=settings(user['id'])['kms_key_arn'];envelope=None
    if kms:
        result=aws_session().client('kms').generate_data_key(KeyId=kms,KeySpec='AES_256',EncryptionContext=context)
        key=result['Plaintext'];envelope=base64.b64encode(result['CiphertextBlob']).decode();mode='aws-kms'
    else:key=local_master();mode='local-encrypted'
    nonce=secrets.token_bytes(12)
    encrypted=base64.b64encode(nonce+AESGCM(key).encrypt(nonce,value.encode(),aad)).decode()
    with db.tx() as c:
        c.execute('INSERT INTO secrets(id,user_id,provider,label,binding,encrypted,key_envelope,mode,version,revoked,created) VALUES(?,?,?,?,?,?,?,?,?,0,?) ON CONFLICT(user_id,label) DO UPDATE SET provider=excluded.provider,binding=excluded.binding,encrypted=excluded.encrypted,key_envelope=excluded.key_envelope,mode=excluded.mode,version=excluded.version,revoked=0,created=excluded.created',(sid,user['id'],provider,label,db.encode(bindings),encrypted,envelope,mode,version,time.time()))
        db.audit(c,user['id'],'vault.rotated' if old else 'vault.created',sid,{'provider':provider,'version':version,'mode':mode})
    value=None;key=None
    return sid

def secret_metadata(uid):
    rows=db.query('SELECT id,provider,label,binding,mode,version,revoked,created FROM secrets WHERE user_id=? ORDER BY created DESC',(uid,))
    for r in rows:r['bindings']=json.loads(r.pop('binding'))
    return rows

def credential_for(uid,provider,node_id):
    for r in db.query('SELECT * FROM secrets WHERE user_id=? AND provider=? AND revoked=0',(uid,provider)):
        bindings=json.loads(r['binding'])
        if node_id in bindings:return r
    return None

def unwrap(uid,sid,node_id):
    user=db.query('SELECT * FROM users WHERE id=?',(uid,),one=True)
    if not user or user['frozen']:raise ValueError('Credential access revoked')
    row=db.query('SELECT * FROM secrets WHERE id=? AND user_id=? AND revoked=0',(sid,uid),one=True)
    if not row or node_id not in json.loads(row['binding']):raise ValueError('Credential not bound to this node')
    context={'tenant':uid,'secret':sid,'provider':row['provider'],'version':str(row['version'])}
    with db.tx() as c:db.audit(c,uid,'vault.decrypt.intent',sid,{'version':row['version'],'node':node_id})
    if row['mode']=='aws-kms':
        result=aws_session().client('kms').decrypt(CiphertextBlob=base64.b64decode(row['key_envelope']),EncryptionContext=context)
        key=result['Plaintext']
    else:key=local_master()
    blob=base64.b64decode(row['encrypted']);plain=AESGCM(key).decrypt(blob[:12],blob[12:],db.encode(context).encode()).decode()
    with db.tx() as c:db.audit(c,uid,'vault.decrypt.completed',sid,{'version':row['version'],'node':node_id})
    return plain

def verify_audit():
    previous='0'*64;count=0
    for r in db.query('SELECT * FROM audit ORDER BY seq'):
        expected=hashlib.sha256(b'NGS-LOCAL-AUDIT-v1\0'+bytes.fromhex(previous)+r['body'].encode()).hexdigest()
        if r['previous_hash']!=previous or not hmac.compare_digest(r['hash'],expected):return {'valid':False,'records':count,'failed_seq':r['seq']}
        previous=r['hash'];count+=1
    checkpoint=db.query("SELECT value FROM controls WHERE key='audit:last-anchor'",one=True)
    return {'valid':True,'records':count,'head':previous,'scope':'Database hash chain; immutable archive verification is available separately to administrators.','checkpoint':json.loads(checkpoint['value']) if checkpoint else None}
