#!/usr/bin/env python3
"""Isolated checks: no VM, original containers, or host data mounts are used."""
import json
import subprocess
import uuid
import smbclient  # python -m pip install smbprotocol (host-side validation only)

def run(*args):
    return subprocess.check_output(args, text=True).strip()

def docker(*args):
    return run('docker', *args)

base = json.loads(docker('image', 'inspect', 'vmanager:1.0'))[0]
new = json.loads(docker('image', 'inspect', 'vmanager:1.1'))[0]
for key in ['Entrypoint', 'Cmd', 'Env', 'ExposedPorts', 'WorkingDir', 'Volumes', 'Healthcheck']:
    assert base['Config'].get(key) == new['Config'].get(key), key
assert new['RootFS']['Layers'][:len(base['RootFS']['Layers'])] == base['RootFS']['Layers']
script = '''import hashlib, json
from pathlib import Path
r=Path('/opt/runtime')
m=json.loads((r/'manifest.json').read_text())
for name, spec in m['components'].items():
    for f in spec['files']:
        assert hashlib.sha256((r/name/f['path']).read_bytes()).hexdigest()==f['sha256'], f
    print(name, len(spec['files']), 'files: SHA256 OK')
'''
print(docker('run', '--rm', '--network', 'none', '--entrypoint', 'python3', 'vmanager:1.1', '-c', script))
for path in ['/run/entry.sh', '/run/boot.sh', '/run/network.sh', '/run/disk.sh', '/oem/on-logon.ps1']:
    args = ('run', '--rm', '--network', 'none', '--entrypoint', 'sha256sum')
    assert docker(*args, 'vmanager:1.0', path) == docker(*args, 'vmanager:1.1', path), path
old_samba = docker('run', '--rm', '--network', 'none', '--entrypoint', 'cat', 'vmanager:1.0', '/run/samba.sh')
new_samba = docker('run', '--rm', '--network', 'none', '--entrypoint', 'cat', 'vmanager:1.1', '/run/samba.sh')
addition = 'addShare "/opt/runtime" "Runtime" "Bundled runtime (read only)" "no" || error "Failed to create share Runtime!"\n\n'
assert new_samba.replace(addition, '') == old_samba
print('Base layers, image configuration, WAA/network/disk scripts and four original shares unchanged')
name = 'vmanager11-check-' + uuid.uuid4().hex[:12]
docker('run', '-d', '--name', name, '--entrypoint', '/bin/bash', 'vmanager:1.1', '-c', 'sleep infinity')
try:
    info = json.loads(docker('inspect', name))[0]
    assert not info['Mounts']
    docker('exec', name, 'bash', '-c', 'export NETWORK=Y DHCP=Y IP=host.lan VM_NET_DEV=eth0 BOOT_MODE=windows_legacy; error() { echo "$*" >&2; exit 1; }; source /run/samba.sh')
    conf = docker('exec', name, 'testparm', '-s')
    for share, path, readonly in [('Data','/shared','No'), ('Upstream','/upstream','Yes'), ('Project','/project','No'), ('Recordings','/recordings','No'), ('Runtime','/opt/runtime','Yes')]:
        assert docker('exec', name, 'testparm', '-s', '--section-name='+share, '--parameter-name=path') == path
        assert docker('exec', name, 'testparm', '-s', '--section-name='+share, '--parameter-name=read only').lower() == readonly.lower()
    ip = info['NetworkSettings']['Networks']['bridge']['IPAddress']
    smbclient.ClientConfig(require_secure_negotiate=False)
    smbclient.register_session(ip, username='runtime-check-guest', password='', require_signing=False)
    def remote(share, path):
        return f'\\\\{ip}\\{share}\\{path}'
    for path in ['server/main.py', 'trajrec-bundle/recorder/integration/harnessstudio/install_recorder.ps1']:
        with smbclient.open_file(remote('Runtime', path), 'rb') as f:
            actual = f.read()
        expected = subprocess.check_output(['docker', 'exec', name, 'cat', '/opt/runtime/'+path])
        assert actual == expected, path
    for share in ['Runtime', 'Upstream']:
        try:
            with smbclient.open_file(remote(share, 'denied-write-test'), 'wb') as f:
                f.write(b'not allowed')
        except OSError as exc:
            assert getattr(exc, "ntstatus", None) == 0xC0000022, exc
        else:
            raise AssertionError(share + ' unexpectedly writable')
    for share in ['Data', 'Project', 'Recordings']:
        with smbclient.open_file(remote(share, 'isolated-write-test'), 'wb') as f:
            f.write(b'isolated test data')
        with smbclient.open_file(remote(share, 'isolated-write-test'), 'rb') as f:
            assert f.read() == b'isolated test data'
    smbclient.reset_connection_cache()
    print('SMB guest reads OK; Runtime writes denied; Data/Project/Recordings writes OK; Upstream read-only')
    print('PASS: Windows VM not started or accessed; Windows UNC/drive/API checks remain manual.')
finally:
    docker('rm', '-f', name)
assert json.loads(docker('image', 'inspect', 'vmanager:1.0'))[0]['Id'] == base['Id']
