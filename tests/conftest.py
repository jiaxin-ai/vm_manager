"""单元测试的假实现与测试夹具：假的 Docker / Agent / 宿主机、可控时钟、同步执行器。"""
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from api import create_app  # noqa: E402
from backend import AgentCallFailed, DockerUnavailable  # noqa: E402
from common import GIB, KeyedLocks  # noqa: E402
from db import Database  # noqa: E402
from recording_manager import RecordingManager  # noqa: E402
from vm_manager import VmManager  # noqa: E402
from workspace_manager import WorkspaceManager  # noqa: E402

SETTINGS = {
    "listen_host": "127.0.0.1",
    "listen_port": 8000,
    "access_host": "10.0.0.10",
    "host_reserved_cpu": 2,
    "host_reserved_ram_gb": 4,
    "vm_overhead_ram_gb": 1,
    "min_free_disk_gb": 50,
    "default_timeout_minutes": 120,
    "default_queue_timeout_minutes": 30,
    "boot_timeout_seconds": 900,
    "poll_interval_seconds": 2,
    "probe_timeout_seconds": 3,
    "health_failure_threshold": 3,
    "health_failure_seconds": 0,  # most tests fail a VM after 3 checks; test_reboot.py uses the 300 s default
    "stop_timeout_seconds": 120,
    "recorder_start_path": "/recording/start",
    "recorder_stop_path": "/recording/stop",
    "recorder_timeout_seconds": 60,
}


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 10, 11, 10, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class SyncExecutor:
    """后台任务同步执行，测试结果可预期。"""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def shutdown(self, wait=True):
        pass


class FakeBackend:
    def __init__(self):
        self.docker_up = True
        self.kvm = True
        self.images = {"img:v1"}
        self.containers = {}  # container_id -> dict
        self.cpu = 16
        self.mem_total = 64 * GIB
        self.mem_available = 60 * GIB
        self.disk_total = 2000 * GIB
        self.disk_free = {"default": 1000 * GIB}
        self.unhealthy = set()  # vm_id
        self.fail_create = self.fail_start = self.fail_stop = self.fail_remove = False
        self.agent_calls = []  # (path, body)
        self.recorder_fail = set()  # 失败的路径
        self.recorder_gate = None  # threading.Event：设置后，开始采集会阻塞到事件被 set
        self.recorder_entered = threading.Event()
        self.stop_calls = []
        self._next_port = 40000
        self._seq = 0

    def _check(self):
        if not self.docker_up:
            raise DockerUnavailable("docker is down")

    # ---- Docker ----
    def ping(self):
        return self.docker_up

    def docker_root_dir(self):
        self._check()
        return "/docker-root"

    def image_exists(self, image):
        self._check()
        return image in self.images

    def list_managed(self):
        self._check()
        return {c["vm_id"]: {"id": cid, "running": c["running"]} for cid, c in self.containers.items()}

    def find_container(self, vm_id):
        self._check()
        for cid, c in self.containers.items():
            if c["vm_id"] == vm_id:
                return {"id": cid, "running": c["running"],
                        "ports": c["ports"] if c["running"] else {"vnc": None, "rdp": None, "agent": None}}
        return None

    def create_container(self, vm_id, image, environment, volumes, publish_ports, cpu_cores, mem_bytes):
        self._check()
        if self.fail_create:
            raise RuntimeError("create failed")
        self._seq += 1
        cid = f"cid{self._seq:04d}{vm_id}"
        self.containers[cid] = {
            "vm_id": vm_id, "running": False, "ports": {}, "image": image, "environment": environment,
            "volumes": volumes, "publish_ports": publish_ports, "cpu_cores": cpu_cores, "mem_bytes": mem_bytes,
        }
        return cid

    def add_container(self, vm_id, running=True):
        """模拟服务之外已经存在的带 label 容器。"""
        self._seq += 1
        cid = f"cid{self._seq:04d}{vm_id}"
        self.containers[cid] = {"vm_id": vm_id, "running": running, "ports": self._ports([5000, 8006, 3389]),
                                "publish_ports": [5000, 8006, 3389]}
        return cid

    def _ports(self, publish_ports):
        ports = {"vnc": None, "rdp": None, "agent": None}
        for port, key in ((5000, "agent"), (8006, "vnc"), (3389, "rdp")):
            if port in publish_ports:
                self._next_port += 1
                ports[key] = self._next_port
        return ports

    def start_container(self, cid):
        self._check()
        if self.fail_start:
            raise RuntimeError("start failed")
        c = self.containers[cid]
        c["running"] = True
        c["ports"] = self._ports(c["publish_ports"])
        return dict(c["ports"])

    def stop_container(self, cid, timeout):
        self._check()
        self.stop_calls.append(cid)
        if self.fail_stop:
            raise RuntimeError("stop failed")
        if cid in self.containers:
            self.containers[cid]["running"] = False

    def remove_container(self, cid):
        self._check()
        if self.fail_remove:
            raise RuntimeError("remove failed")
        self.containers.pop(cid, None)

    def exit_container(self, vm_id):
        for c in self.containers.values():
            if c["vm_id"] == vm_id:
                c["running"] = False

    def container_of(self, vm_id):
        return next((cid for cid, c in self.containers.items() if c["vm_id"] == vm_id), None)

    # ---- 宿主机 ----
    def kvm_available(self):
        return self.kvm

    def file_exists(self, path):
        return os.path.isfile(path)

    def cpu_count(self):
        return self.cpu

    def meminfo(self):
        return self.mem_total, self.mem_available

    def disk_usage(self, path):
        return self.disk_total, self.disk_free.get(path, self.disk_free["default"])

    # ---- Agent ----
    def probe(self, agent_port, probe_path, vnc_port, rdp_port, timeout):
        for c in self.containers.values():
            if c["ports"].get("agent") == agent_port and c["running"]:
                return c["vm_id"] not in self.unhealthy and probe_path == "/probe"
        return False

    def agent_post(self, agent_port, path, body, timeout):
        self.agent_calls.append((path, body))
        if path.endswith("start") and self.recorder_gate is not None:
            self.recorder_entered.set()
            self.recorder_gate.wait(10)
        if path in self.recorder_fail:
            raise AgentCallFailed(f"{path}: HTTP 404")

    def calls(self, suffix):
        return [body for path, body in self.agent_calls if path.endswith(suffix)]


def make_environments(base_storage):
    return [
        {
            "environment_id": "env-a",
            "os": "windows11",
            "software": [{"name": "AutoCAD", "version": "2026"}],
            "min_spec": {"cpu_cores": 2, "ram_gb": 2},
            "docker_image": "img:v1",
            "base_storage": base_storage,
            "base_img": "base_a.img",
            "base_format": "raw",
            "probe_path": "/probe",
        },
        {
            "environment_id": "env-b",
            "os": "windows11",
            "software": [{"name": "SolidWorks", "version": "2025"}],
            "min_spec": {"cpu_cores": 4, "ram_gb": 8},
            "docker_image": "img:v1",
            "base_storage": base_storage,
            "base_img": "base_b.img",
            "base_format": "raw",
            "probe_path": "/probe",
        },
    ]


class Harness:
    def __init__(self, tmp_path, executor=None, **settings_overrides):
        self.tmp = tmp_path
        self.data_root = str(tmp_path / "data")
        self.base_storage = str(tmp_path / "base")
        os.makedirs(os.path.join(self.data_root, "projects"))
        os.makedirs(self.base_storage)
        for name in ("base_a.img", "base_b.img"):
            open(os.path.join(self.base_storage, name), "w").close()
        self.settings = dict(SETTINGS, data_root=self.data_root, **settings_overrides)
        self.environments = make_environments(self.base_storage)
        self.clock = FakeClock()
        self.backend = FakeBackend()
        self.frozen = set()  # 假 chattr 记录的 +i 路径
        self.chattr_calls = []
        self.chattr_fail = False
        self.executor = executor or SyncExecutor()
        self._build()

    def _build(self):
        self.db = Database(os.path.join(self.data_root, "vmm.db"))
        self.db.init_schema()
        self.locks = KeyedLocks()
        self.ws = WorkspaceManager(self.db, self.data_root, self.clock)
        self.ws.chattr = self._fake_chattr
        self.rec = RecordingManager(self.db, self.backend, self.ws, self.settings, self.locks, self.clock)
        self.vms = VmManager(self.db, self.backend, self.ws, self.rec, self.settings, self.environments,
                             self.locks, self.clock, self.executor)
        self.client = TestClient(create_app(self.vms, self.ws, self.rec), raise_server_exceptions=False)

    def restart(self):
        """模拟服务重启：内存状态（锁、线程）丢失，数据库和容器保留。"""
        self._build()
        self.vms.recover()

    def _fake_chattr(self, flag, path):
        self.chattr_calls.append((flag, path))
        if self.chattr_fail:
            raise RuntimeError("chattr failed")
        (self.frozen.add if flag == "+i" else self.frozen.discard)(path)

    # ---- 便捷方法 ----
    def body(self, **overrides):
        body = {"project_id": "p1", "task_id": "t1", "environment_id": "env-a",
                "spec": {"cpu_cores": 4, "ram_gb": 8}}
        body.update(overrides)
        return body

    def create(self, **overrides):
        return self.client.post("/v1/vms", json=self.body(**overrides))

    def create_ok(self, **overrides):
        resp = self.create(**overrides)
        assert resp.status_code == 200, resp.text
        return resp.json()["data"]["vm_id"]

    def vm(self, vm_id):
        return self.db.get_vm(vm_id)

    def status(self, vm_id):
        row = self.db.get_vm(vm_id)
        return row["status"] if row else None

    def detail(self, vm_id):
        return self.client.get(f"/v1/vms/{vm_id}").json()["data"]

    def rounds(self, n=1):
        for _ in range(n):
            self.vms.run_round()

    def run_vm(self, **overrides):
        """创建一台虚拟机并推进到 running。"""
        vm_id = self.create_ok(**overrides)
        self.rounds(2)
        assert self.status(vm_id) == "running", self.vm(vm_id)
        return vm_id

    def workspace(self, project_id="p1", task_id="t1", frozen=False):
        ws = self.client.post("/v1/workspaces", json={"project_id": project_id, "task_id": task_id}).json()["data"]
        if frozen:
            assert self.client.post(f"/v1/workspaces/{ws['workspace_id']}/freeze").status_code == 200
        return ws["workspace_id"]

    def project_tree(self):
        result = []
        for root, dirs, files in os.walk(os.path.join(self.data_root, "projects")):
            result += [os.path.join(root, d) for d in dirs] + [os.path.join(root, f) for f in files]
        return result


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def error_code(resp):
    return resp.json()["error"]["code"]
