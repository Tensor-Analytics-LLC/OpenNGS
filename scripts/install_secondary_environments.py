"""Independent bounded solves; a failed optional family never becomes ready."""
import json
from pathlib import Path
import shutil
import subprocess
import time

def main():
    recipes=json.loads(Path('/opt/studio/secondary-environments.json').read_text())
    records={}
    for name,spec in recipes.items():
        started=time.monotonic(); prefix='/opt/'+name
        try:
            result=subprocess.run(['micromamba','create','-y','-p',prefix,'-c','conda-forge','-c','bioconda',
                '--strict-channel-priority',*spec['packages']],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=300)
            if result.returncode: raise RuntimeError(result.stdout.decode(errors='replace')[-1800:])
            lock=subprocess.check_output(['micromamba','list','-p',prefix,'--explicit'])
            Path('/opt/studio/'+name+'-explicit.lock').write_bytes(lock)
            records[name]={'status':'installed','seconds':round(time.monotonic()-started,2),'packages':spec['packages']}
        except Exception as error:
            shutil.rmtree(prefix,ignore_errors=True)
            records[name]={'status':'unavailable','seconds':round(time.monotonic()-started,2),'error':str(error)[-2000:]}
        subprocess.run(['micromamba','clean','--all','--yes'],stdout=subprocess.DEVNULL,check=False)
        print(json.dumps({name:records[name]}),flush=True)
    Path('/opt/studio/secondary-install.json').write_text(json.dumps(records,indent=2))

if __name__=='__main__':main()
