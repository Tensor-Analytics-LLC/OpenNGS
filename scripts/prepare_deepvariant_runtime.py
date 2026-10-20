"""Freeze the documented 68MB public fixture and eight pinned binary wheels locally."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', default='.state/deepvariant-build')
    args = parser.parse_args()
    root = Path(args.directory)
    fixtures = root / 'fixtures'
    fixtures.mkdir(parents=True, exist_ok=True)
    recipe = json.loads(Path('infra/deepvariant-runtime.json').read_text())
    for row in recipe['fixtures']:
        target = fixtures / row['name']
        if target.exists() and target.stat().st_size == row['bytes'] and hashlib.sha256(target.read_bytes()).hexdigest() == row['sha256']:
            continue
        request = row['url'] + '?generation=' + row['generation']
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read(row['bytes'] + 1)
        if len(raw) != row['bytes'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ValueError('Pinned public fixture identity changed: ' + row['name'])
        target.write_bytes(raw)
    wheels = root / 'wheels'
    wheels.mkdir(exist_ok=True)
    subprocess.run([sys.executable, '-m', 'pip', 'download', '--use-deprecated=legacy-certs', '--dest', str(wheels),
        '--only-binary=:all:', '--platform', 'manylinux_2_28_x86_64', '--python-version', '310',
        '--implementation', 'cp', '--abi', 'cp310', '--require-hashes', '--no-deps',
        '-r', 'infra/deepvariant-requirements.lock'], check=True)
    print(json.dumps({'directory': str(root), 'fixture_bytes': sum(r['bytes'] for r in recipe['fixtures']),
                      'wheel_bytes': sum(p.stat().st_size for p in wheels.glob('*.whl'))}))


if __name__ == '__main__':
    main()
