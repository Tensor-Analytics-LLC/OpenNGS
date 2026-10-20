import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get('NGS_STATE_DIR', ROOT / '.state')).expanduser().resolve()
STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
DATA = Path(os.environ.get('NGS_DATA_DIR', STATE / 'data')).expanduser().resolve()
DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
REGION = os.environ.get('AWS_REGION', 'us-east-1')
PROFILE = os.environ.get('NGS_AWS_PROFILE', os.environ.get('AWS_PROFILE',''))
CLOUD = os.environ.get('NGS_DEPLOYMENT', 'desktop') == 'cloud'
DEMO = os.environ.get('NGS_DEMO', '0') == '1'
DATABASE_URL = os.environ.get('NGS_DATABASE_URL', '')
ALLOWED_HOSTS = os.environ.get('NGS_ALLOWED_HOSTS', '127.0.0.1,localhost,testserver').split(',')
FRONTEND = ROOT / 'frontend'
MAX_BODY = 2 * 1024 * 1024
TRIAL_SECONDS = 8 * 3600
TERMINAL = {'succeeded', 'failed', 'cancelled', 'interrupted'}

def aws_session():
    import boto3
    session=boto3.Session(profile_name=PROFILE if PROFILE and not CLOUD else None, region_name=REGION)
    if CLOUD:
        from botocore.config import Config
        session._session.set_default_client_config(Config(use_dualstack_endpoint=True,connect_timeout=5,read_timeout=20,retries={'mode':'adaptive','max_attempts':3}))
    return session
