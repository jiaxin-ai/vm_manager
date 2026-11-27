import os
from pathlib import Path
import subprocess

import pytest
from image_cache import ImageCache


def environment(tmp_path, name='one'):
    root = tmp_path / name
    root.mkdir()
    (root / 'data.img').write_bytes(os.urandom(16384))
    return {'environment_id': name, 'base_storage': str(root), 'base_img': 'data.img'}


def test_cache_hit_source_change_and_pin_safe_eviction(tmp_path):
    cache = ImageCache(tmp_path / 'cache', 1, 0)
    env = environment(tmp_path)
    first = cache.prepare(env, 'vm_one')
    second = cache.prepare(env, 'vm_two')
    assert first == second
    assert len(list((cache.root / 'objects').iterdir())) == 1
    Path(env['base_storage'], 'data.img').write_bytes(os.urandom(16384))
    changed = cache.prepare(env, 'vm_three')
    assert changed != first
    cache.limit = cache._used()
    other = environment(tmp_path, 'other')
    with pytest.raises(RuntimeError, match='空间不足'):
        cache.prepare(other, 'vm_four')
    assert Path(first['base_storage'], 'data.img').exists()
    cache.release('vm_one')
    cache.release('vm_two')
    cache.prepare(other, 'vm_four')
    assert not Path(first['base_storage']).exists()
    assert Path(changed['base_storage']).exists()


def test_copy_failure_does_not_publish_or_pin(tmp_path, monkeypatch):
    cache = ImageCache(tmp_path / 'cache', 1, 0)
    env = environment(tmp_path)
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, 'cp')
    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        cache.prepare(env, 'vm_one')
    assert not cache._views()
    assert not cache._pins()
    assert cache._used() == 0


def test_shared_base_is_hardlinked_across_software_views(tmp_path):
    cache = ImageCache(tmp_path / 'cache', 1, 0)
    env = environment(tmp_path)
    a = cache.prepare(env, 'vm_a')
    diff = Path(env['base_storage'], 'software')
    diff.mkdir()
    (diff / 'data.qcow2').write_bytes(os.urandom(4096))
    b = cache.prepare({**env, 'diff_img': 'software/data.qcow2'}, 'vm_b')
    assert Path(a['base_storage'], 'data.img').stat().st_ino == Path(b['base_storage'], 'data.img').stat().st_ino
    assert Path(b['base_storage'], 'software/data.qcow2').exists()


def test_cache_prepare_is_background_and_does_not_consume_boot_timeout(h):
    from common import from_iso
    pending = []
    released = []
    class Executor:
        def submit(self, fn): pending.append(fn)
    class Cache:
        def prepare(self, env, vm_id):
            h.clock.advance(1000)  # Longer than Windows boot timeout.
            return env
        def release(self, vm_id): released.append(vm_id)
    h.vms.image_cache = Cache()
    h.vms.executor = Executor()
    vm_id = h.create_ok()
    h.rounds(1)
    assert h.status(vm_id) == 'creating'
    assert pending and not h.backend.containers
    pending.pop(0)()
    assert h.status(vm_id) == 'starting'
    assert from_iso(h.vm(vm_id)['started_at']) == h.clock()
    h.vms.executor = h.executor
    h.rounds(1)
    assert h.status(vm_id) == 'running'
    h.client.post(f'/v1/vms/{vm_id}/stop')
    assert not released  # Stopped instances still need their backing images.
    assert h.client.delete(f'/v1/vms/{vm_id}').status_code == 200
    assert released == [vm_id]


def test_adopted_ssd_file_does_not_recopy_after_hardlink_metadata_changes(tmp_path, monkeypatch):
    cache = ImageCache(tmp_path / 'cache', 1, 0)
    env = environment(tmp_path)
    f = cache._files(env)['data.img']
    obj = cache.root / 'objects' / f['key']
    os.link(f['source'], obj)
    def unexpected_copy(*args, **kwargs):
        raise AssertionError('Existing SSD blocks must be reused')
    monkeypatch.setattr(subprocess, 'run', unexpected_copy)
    cached = cache.prepare(env, 'vm_adopted')
    assert Path(cached['base_storage'], 'data.img').stat().st_ino == obj.stat().st_ino
    allocated = cache._used()
    os.link(obj, cache.root / 'objects' / 'second-identity')
    assert cache._used() == allocated
