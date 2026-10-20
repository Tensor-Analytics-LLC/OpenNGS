import re
import httpx

ACCEPT='application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json'

def resolve(image):
    if not isinstance(image,str) or len(image)>512 or not re.fullmatch(r'[a-zA-Z0-9./_:@-]+',image):raise ValueError('Invalid OCI image reference')
    if '@sha256:' in image:
        if not re.fullmatch(r'.+@sha256:[a-f0-9]{64}',image):raise ValueError('Invalid SHA-256 image digest')
        return {'image':image,'digest':image.rsplit('@',1)[1],'source':'Supplied digest; publication still requires review'}
    host='quay.io' if image.startswith('quay.io/') else 'registry-1.docker.io'
    name=image.removeprefix('quay.io/') if host=='quay.io' else image.removeprefix('docker.io/')
    if host!='quay.io' and name.split('/')[0].count('.'):
        raise ValueError('Automatic registry resolution supports public Quay and Docker Hub. Supply an immutable digest for an approved private registry.')
    if ':' in name:name,tag=name.rsplit(':',1)
    else:tag='latest'
    if '/' not in name:name='library/'+name
    headers={'Accept':ACCEPT}
    with httpx.Client(timeout=15,follow_redirects=False) as client:
        if host=='registry-1.docker.io':
            auth=client.get('https://auth.docker.io/token',params={'service':'registry.docker.io','scope':'repository:'+name+':pull'});auth.raise_for_status();headers['Authorization']='Bearer '+auth.json()['token']
        result=client.get(f'https://{host}/v2/{name}/manifests/{tag}',headers=headers)
        if result.status_code==401 and host=='quay.io':
            challenge=result.headers.get('www-authenticate','');match=re.search(r'realm="([^"]+)"',challenge)
            if not match or not match.group(1).startswith('https://quay.io/'):raise ValueError('Untrusted registry authentication challenge')
            token=client.get(match.group(1),params={'service':'quay.io','scope':'repository:'+name+':pull'});token.raise_for_status();headers['Authorization']='Bearer '+token.json()['token']
            result=client.get(f'https://{host}/v2/{name}/manifests/{tag}',headers=headers)
        if result.status_code!=200:raise ValueError('Registry did not resolve this tag (HTTP '+str(result.status_code)+'). Select a published upstream release.')
        digest=result.headers.get('Docker-Content-Digest')
        if not digest or not re.fullmatch(r'sha256:[a-f0-9]{64}',digest):raise ValueError('Registry did not return a verifiable digest')
        platforms=[m.get('platform',{}) for m in result.json().get('manifests',[])]
    repo=('quay.io/' if host=='quay.io' else 'docker.io/')+name
    return {'image':repo+'@'+digest,'digest':digest,'source_tag':image,'platforms':platforms,'source':'Registry manifest; immutable tag resolution, not a vulnerability/rights approval'}
