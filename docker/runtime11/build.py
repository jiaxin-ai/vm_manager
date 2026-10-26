#!/usr/bin/env python3
"""Snapshot both sources into a temporary context. Never edit source directories."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--server', type=Path, default=Path('/data/hdd/codes/HarnessStudio/server'))
p.add_argument('--recorder', type=Path, default=Path('/data/hdd/vm_manager/projects/trajrec-test-20261019/shared/trajrec-bundle'))
p.add_argument('--tag', default='vmanager:1.1')
a = p.parse_args()
base = subprocess.check_output(['docker', 'image', 'inspect', 'vmanager:1.0', '--format', '{{.Id}}'], text=True).strip()
if a.tag == 'vmanager:1.0':
    p.error('Refusing to replace vmanager:1.0')
for source, required in [(a.server, 'requirements.txt'), (a.server, 'main.py'), (a.recorder, 'recorder/integration/harnessstudio/install_recorder.ps1')]:
    if not (source / required).is_file():
        p.error(f'Missing {source / required}')
with tempfile.TemporaryDirectory(prefix='vmanager11-') as tmp:
    ctx = Path(tmp)
    runtime = ctx / 'runtime'
    runtime.mkdir()
    manifest = {'schema': 1, 'components': {}}
    for name, source in [('server', a.server), ('trajrec-bundle', a.recorder)]:
        shutil.copytree(source, runtime / name)
        entries = []
        for path in sorted((runtime / name).rglob('*')):
            if path.is_file():
                entries.append({'path': path.relative_to(runtime / name).as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
        manifest['components'][name] = {'hash': digest, 'files': entries}
    (runtime / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    for name in ['Update-Runtime.ps1', 'Install-WaaHook.ps1', 'README.md']:
        shutil.copy2(HERE / name, runtime / name)
    shutil.copy2(HERE / 'Dockerfile', ctx / 'Dockerfile')
    subprocess.run(['docker', 'build', '--pull=false', '--build-arg', 'BASE_IMAGE=vmanager:1.0', '-t', a.tag, str(ctx)], check=True)
    current = subprocess.check_output(['docker', 'image', 'inspect', 'vmanager:1.0', '--format', '{{.Id}}'], text=True).strip()
    assert current == base, 'Base image tag changed concurrently'
    print(f'Built {a.tag} from unchanged {base}')
