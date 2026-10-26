"""启动前检查（4.3）：任一项不满足都启动失败。"""
import copy
import os
import shutil
import subprocess

import pytest

import main
from conftest import SETTINGS, FakeBackend, make_diff_environment, make_environments


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


# ---------- 基础盘 + 差分盘 ----------

needs_qemu_img = pytest.mark.skipif(shutil.which("qemu-img") is None, reason="qemu-img not installed")


def make_diff_disk(path, backing="/base_storage/base_c.img", backing_format="raw"):
    """-u：后备盘路径是容器里的路径，宿主机上不存在，不去打开它。"""
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-u", "-F", backing_format, "-b", backing, path, "1M"],
                   check=True)


@pytest.fixture
def diff_setup(setup):
    settings, envs, backend = setup
    base = envs[0]["base_storage"]
    subprocess.run(["qemu-img", "create", "-q", "-f", "raw", os.path.join(base, "base_c.img"), "1M"], check=True)
    os.mkdir(os.path.join(base, "soft-c"))
    make_diff_disk(os.path.join(base, "soft-c", "data.qcow2"))
    for folder in (base, os.path.join(base, "soft-c")):
        for name in ("windows.mac", "windows_secure.tpm"):
            with open(os.path.join(folder, name), "w") as f:
                f.write(folder)
    envs.append(make_diff_environment(base))
    return settings, envs, backend


@needs_qemu_img
def test_diff_disk_checks_pass(diff_setup):
    settings, envs, backend = diff_setup
    main.startup_checks(settings, envs, backend)


def _soft_c(envs, name):
    return os.path.join(envs[2]["base_storage"], "soft-c", name)


@needs_qemu_img
@pytest.mark.parametrize("breaker, message", [
    (lambda e: e[2].update(diff_img="data.qcow2"), "diff_img"),
    (lambda e: e[2].update(diff_img="../soft-c/data.qcow2"), "diff_img"),
    (lambda e: e[2].update(diff_img="a/soft-c/data.qcow2"), "diff_img"),
    (lambda e: e[2].update(diff_img="/soft-c/data.qcow2"), "diff_img"),
    (lambda e: os.remove(_soft_c(e, "data.qcow2")), "差分盘不存在"),
    (lambda e: os.remove(os.path.join(e[2]["base_storage"], "base_c.img")), "基础磁盘不存在"),
    (lambda e: open(_soft_c(e, "windows.extra"), "w").close(), "windows.extra"),
    (lambda e: (os.remove(_soft_c(e, "data.qcow2")), open(_soft_c(e, "data.qcow2"), "wb").write(b"\0" * 4096)),
     "应为 qcow2"),
    (lambda e: (os.remove(_soft_c(e, "data.qcow2")), make_diff_disk(_soft_c(e, "data.qcow2"), "/other/base_c.img")),
     "后备盘为 /other/base_c.img"),
    (lambda e: (os.remove(_soft_c(e, "data.qcow2")), make_diff_disk(_soft_c(e, "data.qcow2"), backing_format="qcow2")),
     "后备盘格式为 qcow2"),
])
def test_diff_disk_check_failures(diff_setup, breaker, message):
    settings, envs, backend = diff_setup
    breaker(envs)
    with pytest.raises(main.StartupError, match=message):
        main.startup_checks(settings, envs, backend)


@needs_qemu_img
def test_diff_disk_corrupt_flag(diff_setup, monkeypatch):
    settings, envs, backend = diff_setup
    real = main.qemu_img_info

    def info(qemu_img, path):
        result = real(qemu_img, path)
        if path.endswith(".qcow2"):
            result["format-specific"]["data"]["corrupt"] = True
        return result

    monkeypatch.setattr(main, "qemu_img_info", info)
    with pytest.raises(main.StartupError, match="损坏"):
        main.startup_checks(settings, envs, backend)


@needs_qemu_img
def test_diff_disk_requires_qemu_img(diff_setup, monkeypatch):
    settings, envs, backend = diff_setup
    monkeypatch.setattr(main.shutil, "which", lambda name: None)
    with pytest.raises(main.StartupError, match="qemu-img"):
        main.startup_checks(settings, envs, backend)
    main.startup_checks(settings, envs[:2], backend)  # 只有老格式的环境时，没有 qemu-img 只跳过格式检查
