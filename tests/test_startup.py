"""启动前检查（4.3）：任一项不满足都启动失败。"""
import copy
import os
import shutil

import pytest

import main
from conftest import SETTINGS, FakeBackend, make_environments


@pytest.fixture
def setup(tmp_path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    for name in ("base_a.img", "base_b.img"):
        (base / name).write_bytes(b"\0" * 1024)
    settings = dict(SETTINGS, data_root=str(tmp_path / "data"))
    monkeypatch.setattr(main, "check_chattr", lambda data_root: None)  # 非 root 无法 chattr +i
    return settings, make_environments(str(base)), FakeBackend()


def test_all_checks_pass(setup):
    settings, envs, backend = setup
    main.startup_checks(settings, envs, backend)


@pytest.mark.parametrize("breaker, message", [
    (lambda s, e, b: e.append(copy.deepcopy(e[0])), "environment_id 重复"),
    (lambda s, e, b: e[0].update(base_img="../x.img"), "base_img"),
    (lambda s, e, b: e[0].update(base_img="sub/x.img"), "base_img"),
    (lambda s, e, b: s.update(access_host="REPLACE_WITH_HOST_LAN_IP"), "access_host"),
    (lambda s, e, b: setattr(b, "kvm", False), "/dev/kvm"),
    (lambda s, e, b: setattr(b, "docker_up", False), "Docker"),
    (lambda s, e, b: b.images.clear(), "镜像"),
    (lambda s, e, b: os.remove(os.path.join(e[1]["base_storage"], "base_b.img")), "基础磁盘不存在"),
    (lambda s, e, b: e[0].update(base_format="qcow2"), "base_format"),
])
def test_check_failures(setup, breaker, message):
    settings, envs, backend = setup
    if message == "base_format" and shutil.which("qemu-img") is None:
        pytest.skip("qemu-img not installed")
    breaker(settings, envs, backend)
    with pytest.raises(main.StartupError, match=message):
        main.startup_checks(settings, envs, backend)


def test_chattr_check_fails_without_privilege(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("以 root 运行时 chattr +i 会成功")
    with pytest.raises(main.StartupError, match="chattr"):
        main.check_chattr(str(tmp_path / "data"))
    assert os.listdir(tmp_path / "data") == []  # 临时文件已删除


def test_load_config_resolves_relative_paths(tmp_path):
    settings, envs = main.load_config("settings.json", "environments.json")
    assert settings["access_host"] != "REPLACE_WITH_HOST_LAN_IP"
    assert all(os.path.isabs(e["base_storage"]) for e in envs)
    assert os.path.isabs(settings["data_root"])


def test_data_root_not_writable(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root 可以写任何目录")
    readonly = tmp_path / "ro"
    readonly.mkdir()
    readonly.chmod(0o555)
    try:
        with pytest.raises(main.StartupError, match="不可写"):
            main.check_chattr(str(readonly / "data"))
    finally:
        readonly.chmod(0o755)
