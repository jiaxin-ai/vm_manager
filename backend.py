"""封装 Docker、虚拟机内 Agent 服务以及宿主机信息的调用。测试时可替换为假实现。"""
import logging
import os
import shutil
import socket

import docker
import requests
from docker.errors import ImageNotFound, NotFound

log = logging.getLogger(__name__)

# 容器内部端口固定（4.1）
AGENT_PORT = 5000
VNC_PORT = 8006
RDP_PORT = 3389

LABEL_MANAGED = "vmm.managed"
LABEL_VM_ID = "vmm.vm_id"
SCRIPT_IN_CONTAINER = "/opt/vm-manager/start_windows.sh"


class DockerUnavailable(Exception):
    """Docker 无法连接。"""


class AgentCallFailed(Exception):
    """调用虚拟机内 Agent 服务失败（超时、连接失败、非 200）。"""


def _published_ports(attrs):
    published = (attrs.get("NetworkSettings") or {}).get("Ports") or {}

    def host_port(container_port):
        bindings = published.get(f"{container_port}/tcp")
        return int(bindings[0]["HostPort"]) if bindings else None

    return {"vnc": host_port(VNC_PORT), "rdp": host_port(RDP_PORT), "agent": host_port(AGENT_PORT)}


def _container_info(attrs):
    return {
        "id": attrs["Id"],
        "running": attrs["State"]["Status"] == "running",
        "ports": _published_ports(attrs),
    }


class Backend:
    def __init__(self, docker_timeout=180):
        self._docker_timeout = docker_timeout
        self._client = None
        self.http = requests.Session()
        self.http.trust_env = False  # 不继承环境代理

    # ---------- Docker ----------

    def _docker(self):
        if self._client is None:
            try:
                self._client = docker.from_env(timeout=self._docker_timeout)
            except Exception as exc:
                raise DockerUnavailable(str(exc)) from exc
        return self._client

    def ping(self):
        try:
            return bool(self._docker().ping())
        except Exception:
            return False

    def docker_root_dir(self):
        try:
            return self._docker().info()["DockerRootDir"]
        except Exception as exc:
            raise DockerUnavailable(str(exc)) from exc

    def image_exists(self, image):
        try:
            self._docker().images.get(image)
            return True
        except ImageNotFound:
            return False
        except Exception as exc:
            raise DockerUnavailable(str(exc)) from exc

    def list_managed(self):
        """本服务的全部容器：{vm_id: {"id", "running"}}。"""
        try:
            rows = self._docker().api.containers(all=True, filters={"label": f"{LABEL_MANAGED}=true"})
        except Exception as exc:
            raise DockerUnavailable(str(exc)) from exc
        return {
            (row.get("Labels") or {}).get(LABEL_VM_ID): {"id": row["Id"], "running": row["State"] == "running"}
            for row in rows
        }

    def find_container(self, vm_id):
        """按 label 找到虚拟机的容器，返回 inspect 的结果摘要；没有时返回 None。"""
        try:
            found = self._docker().containers.list(
                all=True, filters={"label": [f"{LABEL_MANAGED}=true", f"{LABEL_VM_ID}={vm_id}"]}
            )
        except Exception as exc:
            raise DockerUnavailable(str(exc)) from exc
        return _container_info(found[0].attrs) if found else None

    def create_container(self, vm_id, image, environment, volumes, publish_ports, cpu_cores, mem_bytes):
        container = self._docker().containers.create(
            image=image,
            name=f"vm-{vm_id}",
            entrypoint="/bin/bash",
            command=[SCRIPT_IN_CONTAINER],
            environment=environment,
            ports={f"{port}/tcp": None for port in publish_ports},
            platform="linux/amd64",
            devices=["/dev/kvm:/dev/kvm:rwm"],
            volumes=volumes,
            privileged=True,
            cap_add=["NET_ADMIN"],
            labels={LABEL_MANAGED: "true", LABEL_VM_ID: vm_id},
            nano_cpus=cpu_cores * 10**9,
            mem_limit=mem_bytes,
            memswap_limit=mem_bytes,
        )
        return container.id

    def start_container(self, container_id):
        """启动容器并读出宿主机端口：{"vnc", "rdp", "agent"}。"""
        container = self._docker().containers.get(container_id)
        container.start()
        container.reload()
        ports = _published_ports(container.attrs)
        if ports["agent"] is None:
            raise RuntimeError(f"container {container_id} has no published agent port")
        return ports

    def stop_container(self, container_id, timeout):
        """停止容器，并用 reload() 确认已退出。容器不存在时视为已停止。"""
        try:
            container = self._docker().containers.get(container_id)
        except NotFound:
            return
        container.stop(timeout=timeout)
        container.reload()
        if container.status == "running":
            raise RuntimeError(f"container {container_id} is still running after stop")

    def remove_container(self, container_id):
        """删除容器及其匿名卷。容器不存在时视为已删除。"""
        try:
            self._docker().containers.get(container_id).remove(v=True)
        except NotFound:
            pass

    # ---------- 宿主机 ----------

    def kvm_available(self):
        return os.path.exists("/dev/kvm")

    def file_exists(self, path):
        return os.path.isfile(path)

    def cpu_count(self):
        return os.cpu_count()

    def meminfo(self):
        """返回 (MemTotal, MemAvailable)，单位字节。"""
        values = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, rest = line.split(":", 1)
                values[key] = int(rest.split()[0]) * 1024
        return values["MemTotal"], values["MemAvailable"]

    def disk_usage(self, path):
        """返回 (total, free)，单位字节。"""
        usage = shutil.disk_usage(path)
        return usage.total, usage.free

    # ---------- Agent 服务 ----------

    def probe(self, agent_port, probe_path, vnc_port, rdp_port, timeout):
        """健康检查（12.2）。全部通过返回 True；第一项失败即返回 False。"""
        try:
            resp = self.http.get(f"http://127.0.0.1:{agent_port}{probe_path}", timeout=timeout)
            if resp.status_code != 200 or resp.json().get("status") != "Probe successful":
                return False
            if vnc_port is not None:
                resp = self.http.get(f"http://127.0.0.1:{vnc_port}/", timeout=timeout, allow_redirects=False)
                if not 200 <= resp.status_code < 400:
                    return False
            if rdp_port is not None:
                with socket.create_connection(("127.0.0.1", rdp_port), timeout=timeout):
                    pass
            return True
        except Exception as exc:  # 超时、连接失败、内容不是 JSON 等
            log.debug("probe failed on agent port %s: %s", agent_port, exc)
            return False

    def agent_post(self, agent_port, path, body, timeout):
        """调用采集开始/停止接口；不是 HTTP 200 时抛 AgentCallFailed。"""
        try:
            resp = self.http.post(f"http://127.0.0.1:{agent_port}{path}", json=body, timeout=timeout)
        except Exception as exc:
            raise AgentCallFailed(f"{path}: {exc}") from exc
        if resp.status_code != 200:
            raise AgentCallFailed(f"{path}: HTTP {resp.status_code}")
