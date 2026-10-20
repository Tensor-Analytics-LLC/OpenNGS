"""RFC 6238 TOTP, replay protection and single-use recovery codes."""
import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from urllib.parse import quote
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from . import data,db
from .config import CLOUD,aws_session

def initialize():
    with db.tx() as c:
        c.execute('CREATE TABLE IF NOT EXISTS mfa(user_id TEXT PRIMARY KEY,seed TEXT NOT NULL,mode TEXT NOT NULL,enabled BIGINT NOT NULL DEFAULT 0,last_counter BIGINT NOT NULL DEFAULT -1,recovery TEXT NOT NULL,created REAL NOT NULL)')

def totp(seed,counter):
    digest=hmac.new(base64.b32decode(seed),struct.pack('>Q',counter),hashlib.sha1).digest()
    offset=digest[-1]&15
    return str((struct.unpack('>I',digest[offset:offset+4])[0]&0x7fffffff)%1000000).zfill(6)

def encrypt(uid,seed):
    kms=data.settings(uid)['kms_key_arn']
    if kms:
        blob=aws_session().client('kms').encrypt(KeyId=kms,Plaintext=seed.encode(),EncryptionContext={'tenant':uid,'purpose':'totp'})['CiphertextBlob']
        return base64.b64encode(blob).decode(),'kms'
    if CLOUD:raise ValueError('Cloud MFA requires a configured KMS key')
    from .security import local_master
    nonce=secrets.token_bytes(12)
    return base64.b64encode(nonce+AESGCM(local_master()).encrypt(nonce,seed.encode(),uid.encode())).decode(),'local'

def decrypt(row):
    blob=base64.b64decode(row['seed'])
    if row['mode']=='kms':return aws_session().client('kms').decrypt(CiphertextBlob=blob,EncryptionContext={'tenant':row['user_id'],'purpose':'totp'})['Plaintext'].decode()
    from .security import local_master
    return AESGCM(local_master()).decrypt(blob[:12],blob[12:],row['user_id'].encode()).decode()

def setup(user):
    old=db.query('SELECT * FROM mfa WHERE user_id=?',(user['id'],),one=True)
    if old and old['enabled']:raise ValueError('MFA is already enabled; use your existing authenticator or a recovery code')
    seed=base64.b32encode(secrets.token_bytes(20)).decode();encrypted,mode=encrypt(user['id'],seed)
    with db.tx() as c:
        c.execute('INSERT INTO mfa VALUES(?,?,?,0,-1,?,?) ON CONFLICT(user_id) DO UPDATE SET seed=excluded.seed,mode=excluded.mode,recovery=excluded.recovery,created=excluded.created',(user['id'],encrypted,mode,'[]',time.time()))
        db.audit(c,user['id'],'mfa.setup.started',user['id'])
    return {'secret':seed,'uri':'otpauth://totp/'+quote('OpenNGS:'+user['email'],safe='')+'?secret='+seed+'&issuer=OpenNGS&algorithm=SHA1&digits=6&period=30'}

def check(uid,code,enabling=False):
    row=db.query('SELECT * FROM mfa WHERE user_id=?',(uid,),one=True)
    if not row or (not row['enabled'] and not enabling):return not enabling
    if not isinstance(code,str) or len(code)>64:return False
    counter=int(time.time()//30);seed=decrypt(row)
    matched=next((n for n in [counter-1,counter,counter+1] if hmac.compare_digest(totp(seed,n),code)),None)
    hashed=hashlib.sha256(code.encode()).hexdigest()
    with db.tx() as c:
        live=c.execute('SELECT * FROM mfa WHERE user_id=?',(uid,)).fetchone()
        if live['seed']!=row['seed']:return False
        if matched is not None and matched>live['last_counter']:
            c.execute('UPDATE mfa SET last_counter=? WHERE user_id=?',(matched,uid));return True
        import json
        recovery=json.loads(live['recovery'])
        if not enabling and hashed in recovery:
            recovery.remove(hashed);c.execute('UPDATE mfa SET recovery=? WHERE user_id=?',(db.encode(recovery),uid))
            db.audit(c,uid,'mfa.recovery.used',uid);return True
    return False

def enable(user,code):
    if not check(user['id'],code,enabling=True):raise ValueError('Authenticator code is invalid or already used')
    codes=[secrets.token_hex(8) for _ in range(10)]
    with db.tx() as c:
        c.execute('UPDATE mfa SET enabled=1,recovery=? WHERE user_id=?',(db.encode([hashlib.sha256(x.encode()).hexdigest() for x in codes]),user['id']))
        db.audit(c,user['id'],'mfa.enabled',user['id'])
    return {'enabled':True,'recovery_codes':codes}

initialize()
