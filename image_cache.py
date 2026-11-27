"""Bounded immutable image cache. Originals remain on HDD; live/stopped VMs pin views."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

GIB = 1024 ** 3


def _json(path, value):
    tmp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    tmp.write_text(json.dumps(value, ensure_ascii=False))
    os.replace(tmp, path)


def _signature(path):
    s = path.stat()
    return [str(path.resolve()), s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns]


def _key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class ImageCache:
    def __init__(self, root, max_gb, reserve_gb):
        self.root = Path(root)
        self.limit = int(max_gb * GIB)
        self.reserve = int(reserve_gb * GIB)
        if self.limit <= 0 or self.reserve < 0:
            raise ValueError('Invalid image cache limits')
        for name in ('objects', 'views', 'leases'):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def _lock(self):
        with (self.root / '.lock').open('a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def _files(self, env):
        base = Path(env['base_storage'])
        names = {env['base_img']}
        names.update(p.name for p in base.glob('windows*') if p.is_file())
        if env.get('diff_img'):
            names.add(env['diff_img'])
            parent = Path(env['diff_img']).parent
            names.update(str(parent / p.name) for p in (base / parent).glob('windows*') if p.is_file())
        result = {}
        for name in sorted(names):
            rel = Path(name)
            if rel.is_absolute() or '..' in rel.parts:
                raise ValueError('Unsafe image cache path')
            source = base / rel
            sig = _signature(source)
            result[name] = {'source': str(source), 'signature': sig, 'key': _key(sig),
                            'bytes': source.stat().st_blocks * 512}
        return result

    def _views(self):
        result = []
        for p in (self.root / 'views').iterdir():
            if p.name.startswith('.'):
                continue
            manifest = p / '.cache.json'
            if manifest.exists():
                d = json.loads(manifest.read_text())
                result.append((p, d))
        return result

    def _pins(self):
        pins = set()
        for p in (self.root / 'leases').glob('*.json'):
            try:
                pins.add(json.loads(p.read_text())['view'])
            except FileNotFoundError:
                pass  # Container was already removed.
        return pins

    def _used(self):
        allocated = {}
        for p in (self.root / 'objects').iterdir():
            if p.is_file():
                st = p.stat()
                allocated[(st.st_dev, st.st_ino)] = st.st_blocks * 512
        return sum(allocated.values())

    def _gc(self, protected=()):
        used = set(protected)
        for _, d in self._views():
            used.update(d['objects'])
        for p in (self.root / 'objects').iterdir():
            if p.is_file() and p.name not in used:
                p.unlink()

    def _space(self, incoming, protected):
        pins = self._pins()
        self._gc(protected)
        victims = sorted(self._views(), key=lambda v: v[1]['last_used'])
        while self._used() + incoming > self.limit or shutil.disk_usage(self.root).free - incoming < self.reserve:
            victim = next(((p, d) for p, d in victims if p.name not in pins), None)
            if victim is None:
                raise RuntimeError('SSD 镜像缓存空间不足：正在使用或可恢复的虚拟机已锁定缓存；请删除不再需要的实例或增加缓存容量')
            victims.remove(victim)
            shutil.rmtree(victim[0])
            self._gc(protected)

    def prepare(self, env, vm_id):
        if Path(vm_id).name != vm_id:
            raise ValueError('Invalid VM ID')
        with self._lock():
            for partial in (self.root / "views").glob(".partial-*"):
                shutil.rmtree(partial)
            for partial in (self.root / "objects").glob(".partial-*"):
                partial.unlink()
            files = self._files(env)
            view_id = _key({name: f['key'] for name, f in files.items()})
            view = self.root / 'views' / view_id
            keys = {f['key'] for f in files.values()}
            # Durable pin before publishing a container. Pins survive daemon restarts.
            lease = self.root / 'leases' / (vm_id + '.json')
            old_lease = lease.read_text() if lease.exists() else None
            _json(lease, {'view': view_id, 'environment': env['environment_id'], 'created_at': time.time()})
            try:
                missing = {f['key']: f for f in files.values() if not (self.root / 'objects' / f['key']).exists()}
                self._space(sum(f['bytes'] for f in missing.values()), keys)
                if not view.exists():
                    for key, f in missing.items():
                        source = Path(f['source'])
                        target = self.root / 'objects' / key
                        tmp = target.with_name('.partial-' + key)
                        try:
                            subprocess.run(['cp', '--reflink=auto', '--sparse=always', '--', str(source), str(tmp)], check=True)
                            if _signature(source) != f['signature'] or tmp.stat().st_size != source.stat().st_size:
                                raise RuntimeError('镜像原件在复制期间发生变化，请重新启动任务')
                            with tmp.open('rb') as handle:
                                os.fsync(handle.fileno())
                            os.replace(tmp, target)
                        finally:
                            tmp.unlink(missing_ok=True)
                    self._space(0, keys)
                    stage = self.root / 'views' / ('.partial-' + view_id)
                    if stage.exists():
                        shutil.rmtree(stage)
                    stage.mkdir()
                    for name, f in files.items():
                        target = stage / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        os.link(self.root / 'objects' / f['key'], target)
                    _json(stage / '.cache.json', {'objects': sorted(keys), 'last_used': time.time(),
                                                'environment': env['environment_id'], 'files': files})
                    os.replace(stage, view)
                # Recheck source identity even on a hit; no stale version silently reused.
                if any(_signature(Path(f['source'])) != f['signature'] for f in files.values()):
                    raise RuntimeError('镜像原件在准备期间发生变化')
                d = json.loads((view / '.cache.json').read_text())
                d['last_used'] = time.time()
                _json(view / '.cache.json', d)
                return {**env, 'base_storage': str(view)}
            except Exception:
                if old_lease is not None:
                    lease.write_text(old_lease)
                else:
                    lease.unlink(missing_ok=True)
                partial_view = self.root / "views" / (".partial-" + view_id)
                if partial_view.exists():
                    shutil.rmtree(partial_view)
                # Aborted copies are not cache hits, and never delete pinned views.
                self._gc()
                raise

    def release(self, vm_id):
        if Path(vm_id).name != vm_id:
            raise ValueError("Invalid VM ID")
        (self.root / 'leases' / (vm_id + '.json')).unlink(missing_ok=True)

    def status(self):
        with self._lock():
            return {'root': str(self.root), 'used_bytes': self._used(), 'max_bytes': self.limit,
                    'reserve_bytes': self.reserve, 'pinned_views': len(self._pins()),
                    'views': len(self._views())}
