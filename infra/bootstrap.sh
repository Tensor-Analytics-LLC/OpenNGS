#!/bin/bash
set -euo pipefail
umask 077
exec > /var/log/openngs-bootstrap.log 2>&1
export AWS_DEFAULT_REGION=us-east-1 AWS_USE_DUALSTACK_ENDPOINT=true
mkdir -p /etc/amazon/ssm /etc/systemd/journald.conf.d
printf '%s\n' '{"Agent":{"UseDualStackEndpoint":true},"Ssm":{"Endpoint":"ssm.us-east-1.api.aws"},"Mgs":{"Endpoint":"ssmmessages.us-east-1.api.aws"}}' > /etc/amazon/ssm/amazon-ssm-agent.json
systemctl restart amazon-ssm-agent
dnf install -y python3.13 python3.13-pip
id openngs >/dev/null 2>&1 || useradd --system --home-dir /var/lib/openngs --create-home openngs
mkdir -p /opt/openngs /var/lib/openngs /var/cache/openngs
aws s3 cp "s3://${NGS_BUCKET}/${NGS_SOURCE_KEY:-${NGS_RELEASE}/source.tar.gz}" /tmp/openngs-source.tar.gz --region us-east-1
tar -xzf /tmp/openngs-source.tar.gz -C /opt/openngs
chmod -R a+rX /opt/openngs
python3.13 -m venv /opt/openngs/runtime
/opt/openngs/runtime/bin/pip install --disable-pip-version-check -r /opt/openngs/requirements.lock
aws secretsmanager get-secret-value --endpoint-url https://secretsmanager.us-east-1.amazonaws.com --secret-id "$NGS_DB_SECRET" --query SecretString --output text > /var/lib/openngs/db-secret.json
aws s3 cp "s3://${NGS_BUCKET}/${NGS_RELEASE}/state.tar.gz" /tmp/openngs-state.tar.gz
tar -xzf /tmp/openngs-state.tar.gz -C /var/lib/openngs
NGS_BUCKET="$NGS_BUCKET" NGS_RELEASE="$NGS_RELEASE" NGS_DB_HOST="$NGS_DB_HOST" NGS_ORIGIN_TOKEN="$NGS_ORIGIN_TOKEN" /opt/openngs/runtime/bin/python - <<'PY'
import json,os,shlex
from pathlib import Path
from urllib.parse import quote
secret=json.loads(Path('/var/lib/openngs/db-secret.json').read_text())
values={'NGS_DEPLOYMENT':'cloud','NGS_DEMO':'1','NGS_AWS_PROFILE':'','AWS_REGION':'us-east-1','AWS_USE_DUALSTACK_ENDPOINT':'true',
 'NGS_STATE_DIR':'/var/lib/openngs','NGS_DATA_DIR':'/var/cache/openngs','NGS_ALLOWED_HOSTS':'*',
 'NGS_DATABASE_URL':'postgresql://'+quote(secret['username'],safe='')+':'+quote(secret['password'],safe='')+'@'+os.environ['NGS_DB_HOST']+':5432/openngs?sslmode=verify-full&sslrootcert=/opt/openngs/infra/rds-ca.pem',
 'NGS_ORIGIN_TOKEN':os.environ['NGS_ORIGIN_TOKEN'],'NGS_BUCKET':os.environ['NGS_BUCKET'],'NGS_KMS_KEY':os.environ['NGS_KMS_KEY'],'NGS_FIXED_MONTHLY_USD':'110',
 'NGS_SOURCE_URL':'https://github.com/Tensor-Analytics-LLC/OpenNGS',
 'NGS_TAX_FRACTION':'.18','NGS_MONTHLY_LIMIT_USD':'200','NGS_BUDGET_SAFETY_USD':'15','NGS_MAX_JOB_USD':'2'}
for key in ['NGS_DEMO_QUEUE','NGS_DEMO_JOB_DEFINITION','NGS_DEMO_IMAGE','NGS_SAMPLE_MANIFEST_SHA']:
 if os.environ.get(key):values[key]=os.environ[key]
Path('/var/lib/openngs/runtime.env').write_text(''.join(k+'='+shlex.quote(v)+'\n' for k,v in values.items()))
Path('/var/lib/openngs/db-secret.json').unlink()
PY
set -a; source /var/lib/openngs/runtime.env; set +a
cd /opt/openngs
PYTHONPATH=. runtime/bin/python scripts/import_cloud_state.py /var/lib/openngs/studio.sqlite
chown -R openngs:openngs /var/lib/openngs /var/cache/openngs
chmod 700 /var/lib/openngs
cat > /etc/systemd/system/openngs.service <<'UNIT'
[Unit]
Description=OpenNGS interactive API
After=network-online.target
Wants=network-online.target
[Service]
User=openngs
Group=openngs
WorkingDirectory=/opt/openngs
EnvironmentFile=/var/lib/openngs/runtime.env
ExecStart=/opt/openngs/runtime/bin/python -m uvicorn backend.api:app --host 0.0.0.0 --port 8080 --no-access-log --limit-concurrency 160 --timeout-keep-alive 5
Restart=always
RestartSec=5
MemoryMax=2G
TasksMax=200
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/openngs /var/cache/openngs
LimitNOFILE=4096
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/journald.conf.d/openngs.conf <<'JOURNAL'
[Journal]
SystemMaxUse=100M
MaxRetentionSec=7day
RateLimitIntervalSec=30s
RateLimitBurst=200
JOURNAL
systemctl daemon-reload
systemctl enable --now openngs
