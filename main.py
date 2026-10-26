"""入口：读取配置、启动前检查、恢复状态、启动巡检线程、启动服务。

用法（需要 root）：python main.py [--settings settings.json] [--environments environments.json]
"""
import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time

import uvicorn

from api import create_app
from backend import Backend
from common import KeyedLocks
from db import Database
from recording_manager import RecordingManager
from vm_manager import BASE_STORAGE_IN_CONTAINER, VmManager, diff_boot_files
from workspace_manager import WorkspaceManager

ROOT = os.path.dirname(os.path.abspath(__file__))
PLACEHOLDER_HOST = "REPLACE_WITH_HOST_LAN_IP"
REQUIRED_SETTINGS = (
    "listen_host", "listen_port", "access_host", "data_root", "host_reserved_cpu", "host_reserved_ram_gb",
    "vm_overhead_ram_gb", "min_free_disk_gb", "default_timeout_minutes", "default_queue_timeout_minutes",
    "boot_timeout_seconds", "poll_interval_seconds", "probe_timeout_seconds", "health_failure_threshold",
    "stop_timeout_seconds", "recorder_start_path", "recorder_stop_path", "recorder_timeout_seconds",
)
REQUIRED_ENV_FIELDS = ("environment_id", "os", "software", "min_spec", "docker_image",
                       "base_storage", "base_img", "base_format", "probe_path")

log = logging.getLogger("main")


class StartupError(Exception):
    pass


def resolve(path):
    """相对路径一律按项目根目录解析。"""
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def load_config(settings_path, environments_path):
    with open(resolve(settings_path)) as f:
        settings = json.load(f)
    with open(resolve(environments_path)) as f:
        environments = json.load(f)
    missing = [k for k in REQUIRED_SETTINGS if k not in settings]
    if missing:
        raise StartupError(f"settings 缺少字段：{missing}")
    for env in environments:
        missing = [k for k in REQUIRED_ENV_FIELDS if k not in env]
        if missing:
            raise StartupError(f"环境 {env.get('environment_id')} 缺少字段：{missing}")
        env["base_storage"] = resolve(env["base_storage"])
    settings["data_root"] = resolve(settings["data_root"])
    return settings, environments


def check_chattr(data_root):
    """data_root 可写，且支持 chattr +i：加上 +i 后写入应当失败。"""
    try:
        os.makedirs(data_root, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix=".chattr-check-", dir=data_root)
        os.close(fd)
    except OSError as exc:
        raise StartupError(f"data_root 不可写：{data_root}：{exc}")
    try:
        result = subprocess.run(["chattr", "+i", path], capture_output=True, text=True)
        if result.returncode != 0:
            raise StartupError(f"data_root 不支持 chattr +i（需要 root 运行）：{result.stderr.strip()}")
        try:
            with open(path, "a") as f:
                f.write("x")
            writable = True
        except OSError:
            writable = False
        if writable:
            raise StartupError("data_root 上 chattr +i 未生效：加上 +i 后文件仍可写入")
    finally:
        subprocess.run(["chattr", "-i", path], capture_output=True)
        os.remove(path)


def qemu_img_info(qemu_img, path):
    result = subprocess.run([qemu_img, "info", "-U", "--output=json", path], capture_output=True, text=True)
    if result.returncode != 0:
        raise StartupError(f"qemu-img info 失败：{path}：{result.stderr.strip()}")
    return json.loads(result.stdout)


def check_diff_disk(qemu_img, env):
    """差分盘是 qcow2、未损坏，记录的后备盘正好是容器里的 /base_storage/<base_img>，后备格式与 base_format 一致。"""
    path = os.path.join(env["base_storage"], env["diff_img"])
    info = qemu_img_info(qemu_img, path)
    if info["format"] != "qcow2":
        raise StartupError(f"差分盘 {path} 的实际格式为 {info['format']}，应为 qcow2")
    if ((info.get("format-specific") or {}).get("data") or {}).get("corrupt"):
        raise StartupError(f"差分盘 {path} 带有损坏标记")
    backing = f"{BASE_STORAGE_IN_CONTAINER}/{env['base_img']}"
    if info.get("backing-filename") != backing:
        raise StartupError(f"差分盘 {path} 的后备盘为 {info.get('backing-filename')}，应为 {backing}")
    if info.get("backing-filename-format") != env["base_format"]:
        raise StartupError(f"差分盘 {path} 的后备盘格式为 {info.get('backing-filename-format')}，"
                           f"与 base_format={env['base_format']} 不一致")


def startup_checks(settings, environments, backend):
    """4.3：任一项不满足，服务启动失败。"""
    ids = [env["environment_id"] for env in environments]
    if len(ids) != len(set(ids)):
        raise StartupError(f"environment_id 重复：{ids}")
    for env in environments:
        if not env["base_img"] or "/" in env["base_img"] or ".." in env["base_img"]:
            raise StartupError(f"base_img 只能是文件名：{env['base_img']!r}")
        if "diff_img" in env:
            parts = env["diff_img"].split("/") if isinstance(env["diff_img"], str) else []
            if len(parts) != 2 or any(part in ("", ".", "..") for part in parts):
                raise StartupError(f"diff_img 只能是「子目录/文件名」：{env['diff_img']!r}")

    if not settings["access_host"] or settings["access_host"] == PLACEHOLDER_HOST:
        raise StartupError("access_host 仍是占位值，请填写调用方能访问到的宿主机 IP 或域名")

    if not backend.kvm_available():
        raise StartupError("/dev/kvm 不存在")
    if not backend.ping():
        raise StartupError("无法连接 Docker")
    for image in sorted({env["docker_image"] for env in environments}):
        if not backend.image_exists(image):
            raise StartupError(f"镜像不在本地：{image}（服务运行期间不拉取镜像）")

    for env in environments:
        path = os.path.join(env["base_storage"], env["base_img"])
        if not os.path.isfile(path):
            raise StartupError(f"基础磁盘不存在：{path}")
        if "diff_img" in env:
            path = os.path.join(env["base_storage"], env["diff_img"])
            if not os.path.isfile(path):
                raise StartupError(f"差分盘不存在：{path}")
            # 启动文件在容器里覆盖挂载到 /base_storage 下的同名文件上：同名文件必须已经存在，
            # 否则 Docker 要在只读挂载里新建挂载点，创建容器会失败
            for name in diff_boot_files(env):
                if not os.path.isfile(os.path.join(env["base_storage"], name)):
                    raise StartupError(f"{os.path.dirname(path)} 里的 {name} 在 {env['base_storage']} 下没有同名文件")

    check_chattr(settings["data_root"])

    qemu_img = shutil.which("qemu-img")
    if qemu_img is None:
        if any("diff_img" in env for env in environments):
            raise StartupError("宿主机没有 qemu-img，无法检查差分盘")
        log.warning("宿主机没有 qemu-img，跳过基础磁盘格式检查")
        return
    for env in environments:
        path = os.path.join(env["base_storage"], env["base_img"])
        actual = qemu_img_info(qemu_img, path)["format"]
        if actual != env["base_format"]:
            raise StartupError(f"{path} 的实际格式为 {actual}，与 base_format={env['base_format']} 不一致")
        if "diff_img" in env:
            check_diff_disk(qemu_img, env)


def main():
    parser = argparse.ArgumentParser(description="VM Manager v1")
    parser.add_argument("--settings", default="settings.json")
    parser.add_argument("--environments", default="environments.json")
    args = parser.parse_args()

    logging.Formatter.converter = time.gmtime
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)sZ %(levelname)s [%(threadName)s] %(name)s: %(message)s")

    try:
        settings, environments = load_config(args.settings, args.environments)
        backend = Backend(docker_timeout=max(180, settings["stop_timeout_seconds"] + 60))
        startup_checks(settings, environments, backend)
    except StartupError as exc:
        log.error("启动前检查失败：%s", exc)
        sys.exit(1)

    data_root = settings["data_root"]
    os.makedirs(os.path.join(data_root, "projects"), exist_ok=True)
    db = Database(os.path.join(data_root, "vmm.db"))
    db.init_schema()

    instance_locks = KeyedLocks()
    workspaces = WorkspaceManager(db, data_root)
    recordings = RecordingManager(db, backend, workspaces, settings, instance_locks)
    vms = VmManager(db, backend, workspaces, recordings, settings, environments, instance_locks)

    vms.recover()
    vms.start_inspector()
    log.info("VM Manager listening on %s:%s (access_host=%s, data_root=%s)",
             settings["listen_host"], settings["listen_port"], settings["access_host"], data_root)
    app = create_app(vms, workspaces, recordings, on_shutdown=vms.stop_inspector)
    uvicorn.run(app, host=settings["listen_host"], port=settings["listen_port"], workers=1, log_config=None)


if __name__ == "__main__":
    main()
