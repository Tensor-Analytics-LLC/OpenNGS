"""Freeze small, provenance-labelled public slices; never the SSD inventory."""
import hashlib
import json
import os
from pathlib import Path
from backend import portable

os.environ['NGS_USE_BUNDLED_SAMPLES']='0'
root=Path('demo-data');root.mkdir(exist_ok=True)
outputs={}
for tool in ['sample.fastq','sample.rna','sample.giab','sample.somatic','sample.clinvar','sample.alignments','reference.tiny']:
    folder=root/tool;folder.mkdir(exist_ok=True)
    outputs[tool]=portable.execute({'tool':tool,'params':{},'inputs':{},'workdir':str(folder),'threads':1})

local=str(root.resolve())
encoded=json.dumps(outputs)
encoded=encoded.replace(local,'/opt/studio/demo-data').replace(chr(34)+'demo-data/',chr(34)+'/opt/studio/demo-data/')
manifest={'schema':1,'outputs':json.loads(encoded),'files':[],'license_note':'Application MIT licence does not replace tool/data licences. Public source citations are retained; engineering samples are explicitly synthetic.'}
for p in root.rglob('*'):
    if p.is_file() and p.name!='manifest.json':manifest['files'].append({'path':str(p.relative_to(root)),'bytes':p.stat().st_size,'sha256':portable.sha(p)})
(root/'manifest.json').write_text(json.dumps(manifest,indent=2))
print(json.dumps({'files':len(manifest['files']),'bytes':sum(x['bytes'] for x in manifest['files']),'sample_nodes':len(outputs),'manifest_sha256':portable.sha(root/'manifest.json')}))
