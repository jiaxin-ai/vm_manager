"""虚拟机：校验、资源计算、FIFO 排队、巡检、启动/停止/删除、实例锁（第 8、9、11.3、12、13 节）。"""
import json
import logging
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from backend import AGENT_PORT, RDP_PORT, SCRIPT_IN_CONTAINER, VNC_PORT, DockerUnavailable
from common import GIB, ApiError, from_iso, to_iso, utc_now

log = logging.getLogger(__name__)

VM_STATUSES = ("queued", "creating", "starting", "running", "stopping", "stopped", "deleting", "failed")
SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "start_windows.sh")
RETRY_INTERVAL_SECONDS = 60


def _docker_unavailable():
    return ApiError(503, "DOCKER_UNAVAILABLE", "Docker 服务不可用，请稍后重试")


def _vm_busy(status):
    return ApiError(409, "VM_BUSY", "虚拟机正在创建、停止或删除中，请稍后重试", {"status": status})


class VmManager:
    def __init__(self, db, backend, workspaces, recordings, settings, environments, instance_locks,
                 clock=utc_now, executor=None):
        self.db = db
        self.backend = backend
        self.workspaces = workspaces
        self.recordings = recordings
        self.settings = settings
        self.environments = {env["environment_id"]: env for env in environments}
        self.instance_locks = instance_locks
        self.clock = clock
        self.scheduler_lock = threading.Lock()
        self.executor = executor or ThreadPoolExecutor(max_workers=16, thread_name_prefix="vm-op")
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread = None
        self._recovery_pending = False
        self._docker_was_ok = True

    # ==================== 资源（8.3） ====================

    def _budgets(self):
        mem_total, _ = self.backend.meminfo()
        cpu_budget = self.backend.cpu_count() - self.settings["host_reserved_cpu"]
        mem_budget = mem_total // GIB - self.settings["host_reserved_ram_gb"]
        return cpu_budget, mem_budget

    def _footprint(self, vm):
        return vm["cpu_cores"], vm["ram_gb"] + self.settings["vm_overhead_ram_gb"]

    def host_resources(self):
        mem_total, mem_available = self.backend.meminfo()
        cpu_budget, mem_budget = self._budgets()
        reserved = self.db.reserved_vms()
        cpu_allocated = sum(self._footprint(vm)[0] for vm in reserved)
        mem_allocated = sum(self._footprint(vm)[1] for vm in reserved)
        mem_free_for_vms = mem_available / GIB - self.settings["host_reserved_ram_gb"]
        disk_total, disk_free = self.backend.disk_usage(self.settings["data_root"])
        disk_total, disk_free = disk_total // GIB, disk_free // GIB
        return {
            "cpu": {
                "total": self.backend.cpu_count(),
                "allocated": cpu_allocated,
                "available": max(0, cpu_budget - cpu_allocated),
            },
            "memory_gb": {
                "total": mem_total // GIB,
                "allocated": mem_allocated,
                "available": max(0, math.floor(min(mem_budget - mem_allocated, mem_free_for_vms))),
            },
            "disk_gb": {"total": disk_total, "used": disk_total - disk_free, "available": disk_free},
            "vms": {"running": len(reserved), "queued": self.db.count_queued()},
        }

    def _fits(self, vm):
        """队首的资源检查：CPU、内存、data_root 与 Docker 数据目录所在磁盘的剩余空间。"""
        resources = self.host_resources()
        cpu, mem = self._footprint(vm)
        if cpu > resources["cpu"]["available"] or mem > resources["memory_gb"]["available"]:
            return False
        min_free = self.settings["min_free_disk_gb"]
        for path in (self.settings["data_root"], self.backend.docker_root_dir()):
            if self.backend.disk_usage(path)[1] // GIB < min_free:
                return False
        return True

    def host_missing(self, env):
        """KVM、镜像、基础磁盘中不可用的项。检查镜像需要 Docker，无法连接时抛 DockerUnavailable。"""
        missing = []
        if not self.backend.kvm_available():
            missing.append("kvm")
        if not self.backend.image_exists(env["docker_image"]):
            missing.append("image")
        if not self.backend.file_exists(os.path.join(env["base_storage"], env["base_img"])):
            missing.append("base_disk")
        return missing

    def _check_host_ready(self, env):
        """接口用：先判断 Docker 能否连接，再检查 KVM、镜像、基础磁盘。"""
        if not self.backend.ping():
            raise _docker_unavailable()
        try:
            missing = self.host_missing(env)
        except DockerUnavailable:
            raise _docker_unavailable()
        if missing:
            raise ApiError(503, "HOST_NOT_READY", "宿主机环境未就绪", {"missing": missing})

    # ==================== 查询 ====================

    def _get(self, vm_id):
        vm = self.db.get_vm(vm_id)
        if vm is None:
            raise ApiError(404, "VM_NOT_FOUND", "虚拟机不存在", {"vm_id": vm_id})
        return vm

    def public_environments(self):
        return [{k: env[k] for k in ("environment_id", "os", "software", "min_spec")}
                for env in self.environments.values()]

    def list(self, project_id=None, task_id=None, statuses=None):
        return [
            {k: vm[k] for k in ("vm_id", "project_id", "task_id", "status")}
            for vm in self.db.list_vms(project_id, task_id, statuses)
        ]

    def detail(self, vm_id):
        vm = self._get(vm_id)
        env = json.loads(vm["environment_json"])
        expose = json.loads(vm["expose"])
        ports = json.loads(vm["ports"]) if vm["ports"] else {}
        workspace = self.db.get_workspace(vm["workspace_id"])
        recording = self.db.active_recording(vm_id)
        queue = None
        if vm["status"] == "queued":
            position = self.db.queue_position(vm_id)
            reason = (vm["queue_reason"] or "waiting_turn") if position == 1 else "waiting_turn"
            queue = {"position": position, "reason": reason, "expires_at": vm["queue_deadline"]}
        return {
            "vm_id": vm_id,
            "project_id": vm["project_id"],
            "task_id": vm["task_id"],
            "chat_id": vm["chat_id"],
            "status": vm["status"],
            "environment_id": vm["environment_id"],
            "os": env["os"],
            "software": env["software"],
            "spec": {"cpu_cores": vm["cpu_cores"], "ram_gb": vm["ram_gb"]},
            "expose": expose,
            "lifecycle": {
                "timeout_minutes": vm["timeout_minutes"],
                "queue_timeout_minutes": vm["queue_timeout_minutes"],
            },
            "container_id": vm["container_id"],
            "access": {
                "host": self.settings["access_host"],
                "vnc_port": ports.get("vnc") if expose["vnc"] else None,
                "rdp_port": ports.get("rdp") if expose["rdp"] else None,
                "agent_port": ports.get("agent"),
            },
            "mounts": {
                "workspace": {
                    "workspace_id": vm["workspace_id"],
                    "writable": workspace is not None and workspace["status"] == "writable",
                },
                "upstreams": [
                    {**u, "path": f"E:\\{u['task_id']}"} for u in json.loads(vm["upstreams"])
                ],
                "project_shared_path": self.workspaces.shared_dir(vm["project_id"]),
                "recordings_path": self.workspaces.recordings_dir(vm["project_id"], vm_id),
            },
            "current_recording_id": recording["recording_id"] if recording else None,
            "queue": queue,
            "created_at": vm["created_at"],
            "started_at": vm["started_at"],
            "expires_at": vm["expires_at"],
            "stopped_at": vm["stopped_at"],
            "stop_reason": vm["stop_reason"],
            "error": {"code": vm["error_code"], "message": vm["error_message"]} if vm["error_code"] else None,
        }

    # ==================== 创建（11.3） ====================

    def create(self, req):
        """req 已通过第 1 项（字段、类型、格式）校验。校验失败时不写数据库，也不创建目录。"""
        project_id, task_id = req["project_id"], req["task_id"]
        env = self.environments.get(req["environment_id"])
        if env is None:
            raise ApiError(422, "ENVIRONMENT_NOT_FOUND", "软件环境不存在", {
                "requested": req["environment_id"], "supported": list(self.environments),
            })
        spec = req["spec"]
        minimum = env["min_spec"]
        if spec["cpu_cores"] < minimum["cpu_cores"] or spec["ram_gb"] < minimum["ram_gb"]:
            raise ApiError(422, "BELOW_ENVIRONMENT_MINIMUM", "资源规格低于环境最低配置",
                           {"requested": spec, "minimum": minimum})
        cpu, mem = self._footprint(spec)
        cpu_budget, mem_budget = self._budgets()
        if cpu > cpu_budget or mem > mem_budget:
            raise ApiError(422, "REQUEST_EXCEEDS_CAPACITY", "资源规格超出宿主机总容量，即使宿主机空闲也无法创建", {
                "requested": spec,
                "required": {"cpu_cores": cpu, "ram_gb": mem},
                "capacity": {"cpu_cores": cpu_budget, "ram_gb": mem_budget},
            })
        upstreams = self.workspaces.resolve_upstreams(project_id, req["upstream_task_ids"])
        self._check_host_ready(env)

        # 全部通过：先算出所有路径（含 realpath 检查），再创建目录和记录
        vm_id = self.db.new_vm_id()
        shared_dir = self.workspaces.shared_dir(project_id)
        self.workspaces.task_dir(project_id, task_id)
        recordings_dir = self.workspaces.recordings_dir(project_id, vm_id)
        os.makedirs(shared_dir, exist_ok=True)
        workspace = self.workspaces.ensure(project_id, task_id)
        os.makedirs(recordings_dir, exist_ok=True)

        lifecycle = req["lifecycle"]
        timeout = lifecycle["timeout_minutes"] or self.settings["default_timeout_minutes"]
        queue_timeout = lifecycle["queue_timeout_minutes"] or self.settings["default_queue_timeout_minutes"]
        now = self.clock()
        position = self.db.insert_queued_vm({
            "vm_id": vm_id,
            "project_id": project_id,
            "task_id": task_id,
            "chat_id": req["chat_id"],
            "environment_id": env["environment_id"],
            "environment_json": json.dumps(env),
            "cpu_cores": spec["cpu_cores"],
            "ram_gb": spec["ram_gb"],
            "expose": json.dumps(req["expose"]),
            "timeout_minutes": timeout,
            "queue_timeout_minutes": queue_timeout,
            "workspace_id": workspace["workspace_id"],
            "upstreams": json.dumps(upstreams),
            "status": "queued",
            "created_at": to_iso(now),
            "queue_deadline": to_iso(now + timedelta(minutes=queue_timeout)),
        })
        log.info("vm %s queued (project=%s task=%s env=%s position=%d)",
                 vm_id, project_id, task_id, env["environment_id"], position)
        self.wake()
        return {"vm_id": vm_id, "status": "queued", "queue_position": position,
                "workspace_id": workspace["workspace_id"]}

    # ==================== 启动 ====================

    def _start_result(self, vm):
        if vm["status"] in ("queued", "creating", "starting", "running"):
            position = self.db.queue_position(vm["vm_id"]) if vm["status"] == "queued" else None
            return {"vm_id": vm["vm_id"], "status": vm["status"], "queue_position": position}
        if vm["status"] in ("stopping", "deleting"):
            raise _vm_busy(vm["status"])
        if vm["status"] == "failed":
            raise ApiError(409, "VM_FAILED", "虚拟机处于失败状态，不能启动，请删除后重新创建", {"vm_id": vm["vm_id"]})
        return None  # stopped：需要启动

    def start(self, vm_id):
        vm = self._get(vm_id)
        result = self._start_result(vm)
        if result is not None:
            return result
        lock = self.instance_locks.get(vm_id)
        if not lock.acquire(blocking=False):
            raise _vm_busy(vm["status"])
        try:
            vm = self._get(vm_id)
            result = self._start_result(vm)
            if result is not None:
                return result
            self._check_host_ready(json.loads(vm["environment_json"]))
            now = self.clock()
            position = self.db.requeue_vm(vm_id, {
                "started_at": None,
                "expires_at": None,
                "stopped_at": None,
                "stop_reason": None,
                "health_failures": 0,
                "queue_reason": None,
                "queue_deadline": to_iso(now + timedelta(minutes=vm["queue_timeout_minutes"])),
            })
            if position is None:
                return self._start_result(self._get(vm_id))
            log.info("vm %s re-queued (position=%d)", vm_id, position)
        finally:
            lock.release()
        self.wake()
        return {"vm_id": vm_id, "status": "queued", "queue_position": position}

    # ==================== 停止 ====================

    def stop(self, vm_id):
        vm = self._get(vm_id)
        if vm["status"] in ("creating", "deleting"):
            raise _vm_busy(vm["status"])
        lock = self.instance_locks.get(vm_id)
        if not lock.acquire(blocking=False):
            raise _vm_busy(vm["status"])
        try:
            vm = self._get(vm_id)
            status = vm["status"]
            if status in ("creating", "deleting"):
                raise _vm_busy(status)
            if status == "queued":
                if self.db.update_vm(vm_id, {"status": "stopped", "stop_reason": "cancelled",
                                             "stopped_at": to_iso(self.clock())}, statuses=["queued"]):
                    log.info("vm %s cancelled while queued", vm_id)
            elif status in ("starting", "running", "stopping") or (status == "failed" and vm["reserved"]):
                if not self.backend.ping():
                    raise _docker_unavailable()
                if status != "failed":
                    self.db.update_vm(vm_id, {"status": "stopping", "stop_reason": "manual"},
                                      statuses=["starting", "running", "stopping"])
                try:
                    self._stop_and_release(vm_id)
                except Exception:
                    log.exception("vm %s stop failed", vm_id)
                    self.db.update_vm(vm_id, {"last_retry_at": to_iso(self.clock())})
                    raise ApiError(500, "STOP_FAILED", "停止虚拟机未完成，可再次调用重试", {"vm_id": vm_id})
            vm = self._get(vm_id)
            return {"vm_id": vm_id, "status": vm["status"], "stop_reason": vm["stop_reason"],
                    "stopped_at": vm["stopped_at"]}
        finally:
            lock.release()

    def _stop_and_release(self, vm_id):
        """先结束采集，再停止容器；确认停止后 reserved=0。调用方持有实例锁。
        stopping → stopped；failed 保持 failed。失败时抛出异常，状态不变。"""
        vm = self.db.get_vm(vm_id)
        self.recordings.end_for_vm(vm)
        if vm["container_id"]:
            started = self.clock()
            self.backend.stop_container(vm["container_id"], self.settings["stop_timeout_seconds"])
            log.info("vm %s container stopped in %.1fs", vm_id, (self.clock() - started).total_seconds())
        fields = {"reserved": 0, "stopped_at": to_iso(self.clock()), "last_retry_at": None}
        if vm["status"] == "stopping":
            fields["status"] = "stopped"
        self.db.update_vm(vm_id, fields, statuses=[vm["status"]])
        log.info("vm %s %s, resources released", vm_id, fields.get("status", vm["status"]))

    # ==================== 删除 ====================

    def delete(self, vm_id):
        deleted = {"vm_id": vm_id, "status": "deleted"}
        vm = self.db.get_vm(vm_id)
        if vm is None:
            return deleted
        if vm["status"] == "creating":
            raise _vm_busy(vm["status"])
        lock = self.instance_locks.get(vm_id)
        if not lock.acquire(blocking=False):
            raise _vm_busy(vm["status"])
        try:
            vm = self.db.get_vm(vm_id)
            if vm is None:
                return deleted
            if vm["status"] == "creating":
                raise _vm_busy(vm["status"])
            if vm["status"] == "queued" and not vm["container_id"]:
                if self.db.delete_vm(vm_id, status="queued"):
                    log.info("vm %s deleted while queued", vm_id)
                    return deleted
                vm = self._get(vm_id)  # 刚好排队超时，按其他状态处理
            if vm["container_id"] and not self.backend.ping():
                raise _docker_unavailable()
            self.db.update_vm(vm_id, {"status": "deleting"},
                              statuses=[s for s in VM_STATUSES if s != "creating"])
            try:
                self._delete_locked(vm_id)
            except Exception:
                log.exception("vm %s delete failed", vm_id)
                self.db.update_vm(vm_id, {"last_retry_at": to_iso(self.clock())})
                raise ApiError(500, "DELETE_FAILED", "删除虚拟机未完成，记录仍保留，可再次调用重试", {"vm_id": vm_id})
            return deleted
        finally:
            lock.release()

    def _delete_locked(self, vm_id):
        """结束采集 → 停止容器 → remove(v=True) → 删除记录。不删除任何目录和采集记录。"""
        vm = self.db.get_vm(vm_id)
        self.recordings.end_for_vm(vm)
        if vm["container_id"]:
            self.backend.stop_container(vm["container_id"], self.settings["stop_timeout_seconds"])
            self.db.update_vm(vm_id, {"reserved": 0})
            self.backend.remove_container(vm["container_id"])
        self.db.delete_vm(vm_id)
        log.info("vm %s deleted", vm_id)

    # ==================== 容器（第 9 节） ====================

    def _container_args(self, vm):
        env = json.loads(vm["environment_json"])
        expose = json.loads(vm["expose"])
        project_id, vm_id = vm["project_id"], vm["vm_id"]
        volumes = {
            env["base_storage"]: {"bind": "/base_storage", "mode": "ro"},
            self.workspaces.task_dir(project_id, vm["task_id"]): {"bind": "/shared", "mode": "rw"},
            self.workspaces.shared_dir(project_id): {"bind": "/project", "mode": "rw"},
            self.workspaces.recordings_dir(project_id, vm_id): {"bind": "/recordings", "mode": "rw"},
            SCRIPT_PATH: {"bind": SCRIPT_IN_CONTAINER, "mode": "ro"},
        }
        for upstream in json.loads(vm["upstreams"]):
            volumes[self.workspaces.task_dir(project_id, upstream["task_id"])] = {
                "bind": f"/upstream/{upstream['task_id']}", "mode": "ro",
            }
        publish_ports = [AGENT_PORT]
        if expose["vnc"]:
            publish_ports.append(VNC_PORT)
        if expose["rdp"]:
            publish_ports.append(RDP_PORT)
        _, mem = self._footprint(vm)
        return {
            "vm_id": vm_id,
            "image": env["docker_image"],
            "environment": {
                "RAM_SIZE": f"{vm['ram_gb']}G",
                "CPU_CORES": str(vm["cpu_cores"]),
                "BASE_IMG": env["base_img"],
                "BASE_FORMAT": env["base_format"],
            },
            "volumes": volumes,
            "publish_ports": publish_ports,
            "cpu_cores": vm["cpu_cores"],
            "mem_bytes": mem * GIB,
        }

    def _provision(self, vm_id):
        """9.1：creating 状态下创建（或复用）并启动容器。调用方持有实例锁。"""
        vm = self.db.get_vm(vm_id)
        try:
            container_id = vm["container_id"]
            if not container_id:
                container_id = self.backend.create_container(**self._container_args(vm))
                self.db.update_vm(vm_id, {"container_id": container_id})
                log.info("vm %s container %s created", vm_id, container_id[:12])
            ports = self.backend.start_container(container_id)
            self.db.update_vm(vm_id, {"status": "starting", "ports": json.dumps(ports), "ever_started": 1},
                              statuses=["creating"])
            log.info("vm %s starting, ports %s", vm_id, ports)
        except Exception as exc:
            log.exception("vm %s provision failed", vm_id)
            self._provision_failed(vm_id, f"创建或启动容器失败：{exc}")

    def _provision_failed(self, vm_id, message):
        vm = self.db.get_vm(vm_id)
        fields = {"status": "failed", "error_code": "PROVISION_FAILED", "error_message": message}
        container_id = vm["container_id"]
        if not vm["ever_started"]:
            # 首次创建：删除已创建的容器，释放资源
            if container_id:
                try:
                    self.backend.stop_container(container_id, self.settings["stop_timeout_seconds"])
                    self.backend.remove_container(container_id)
                    fields.update(container_id=None, ports=None)
                except Exception:
                    log.exception("vm %s: removing container after provision failure failed", vm_id)
                    # reserved 保持 1，由巡检线程补做停止（12.1 第 6 步）
                    self.db.update_vm(vm_id, {**fields, "last_retry_at": to_iso(self.clock())},
                                      statuses=["creating"])
                    return
            self.db.update_vm(vm_id, {**fields, "reserved": 0}, statuses=["creating"])
            return
        # 重新启动失败：保留容器和 C 盘，确认容器已停止后释放资源
        self.db.update_vm(vm_id, {**fields, "stop_reason": "failure"}, statuses=["creating"])
        try:
            self.backend.stop_container(container_id, self.settings["stop_timeout_seconds"])
            self.db.update_vm(vm_id, {"reserved": 0, "stopped_at": to_iso(self.clock())})
        except Exception:
            log.exception("vm %s: stopping container after provision failure failed", vm_id)
            self.db.update_vm(vm_id, {"last_retry_at": to_iso(self.clock())})

    # ==================== 巡检线程（第 12 节） ====================

    def start_inspector(self):
        self._thread = threading.Thread(target=self._loop, name="inspector", daemon=True)
        self._thread.start()

    def stop_inspector(self):
        """服务退出时只停止巡检线程，不停止任何虚拟机。"""
        self._stopping.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
        self.executor.shutdown(wait=False)

    def wake(self):
        self._wake.set()

    def _loop(self):
        while not self._stopping.is_set():
            try:
                self.run_round()
            except Exception:
                log.exception("inspector round failed")
            self._wake.wait(self.settings["poll_interval_seconds"])
            self._wake.clear()

    def run_round(self):
        # 1. 列出容器；Docker 无法连接时跳过所有依赖 Docker 的步骤
        try:
            containers = self.backend.list_managed()
        except DockerUnavailable as exc:
            containers = None
            if self._docker_was_ok:
                log.warning("Docker unavailable, skipping docker-dependent steps: %s", exc)
        self._docker_was_ok = containers is not None
        if containers is not None and self._recovery_pending:
            self.recover_vms()
            self._recovery_pending = False
            containers = self.backend.list_managed()

        now = self.clock()
        self._expire_queued(now)  # 2
        if containers is not None:
            self._expire_running(now)  # 3
            self._check_health(containers)  # 4、5
            self._resume_interrupted(now)  # 6（虚拟机部分）
        self.workspaces.retry_pending()  # 6（任务空间）
        for vm_id in self.recordings.unconfirmed_running_vm_ids():  # 6（采集记录）
            self._submit_locked(vm_id, self.recordings.start_locked)
        self._schedule()  # 7

    def _handoff(self, vm_id, fn):
        """调用方已持有实例锁：交给后台线程执行 fn(vm_id)，结束后由后台线程释放锁。
        返回 False 表示没能提交，锁仍由调用方负责释放。"""
        lock = self.instance_locks.get(vm_id)

        def run():
            try:
                fn(vm_id)
            except Exception:
                log.exception("background operation %s on %s failed", getattr(fn, "__name__", fn), vm_id)
            finally:
                lock.release()

        try:
            self.executor.submit(run)
            return True
        except Exception:
            log.exception("cannot submit background operation for %s", vm_id)
            return False

    def _submit_locked(self, vm_id, fn):
        """尝试获取实例锁，拿到后交给后台执行；拿不到就跳过，下一轮再试。"""
        lock = self.instance_locks.get(vm_id)
        if not lock.acquire(blocking=False):
            return False
        if not self._handoff(vm_id, fn):
            lock.release()
            return False
        return True

    def _expire_queued(self, now):
        for vm in self.db.vms_with_status("queued"):
            if now > from_iso(vm["queue_deadline"]):
                if self.db.update_vm(vm["vm_id"], {
                    "status": "failed", "error_code": "QUEUE_TIMEOUT",
                    "error_message": f"排队超过 {vm['queue_timeout_minutes']} 分钟仍未开始创建",
                }, statuses=["queued"]):
                    log.warning("vm %s failed: QUEUE_TIMEOUT", vm["vm_id"])

    def _expire_running(self, now):
        for vm in self.db.vms_with_status("starting", "running"):
            if vm["expires_at"] and now >= from_iso(vm["expires_at"]):
                self._submit_locked(vm["vm_id"], self._expire)

    def _expire(self, vm_id):
        if not self.db.update_vm(vm_id, {"status": "stopping", "stop_reason": "expired"},
                                 statuses=["starting", "running"]):
            return
        log.info("vm %s expired, stopping", vm_id)
        try:
            self._stop_and_release(vm_id)
        except Exception:
            log.exception("vm %s expiry stop failed", vm_id)
            self.db.update_vm(vm_id, {"last_retry_at": to_iso(self.clock())})

    def _probe(self, vm):
        ports = json.loads(vm["ports"] or "{}")
        expose = json.loads(vm["expose"])
        env = json.loads(vm["environment_json"])
        return self.backend.probe(
            ports.get("agent"), env["probe_path"],
            ports.get("vnc") if expose["vnc"] else None,
            ports.get("rdp") if expose["rdp"] else None,
            self.settings["probe_timeout_seconds"],
        )

    def _check_health(self, containers):
        for vm in self.db.vms_with_status("starting", "running"):
            vm_id = vm["vm_id"]
            info = containers.get(vm_id)
            container_running = info is not None and info["running"]
            healthy = container_running and self._probe(vm)  # 探测不持锁
            lock = self.instance_locks.get(vm_id)
            if not lock.acquire(blocking=False):
                continue
            handed_off = False
            try:
                handed_off = self._update_after_check(vm_id, container_running, healthy)
            finally:
                if not handed_off:
                    lock.release()

    def _update_after_check(self, vm_id, container_running, healthy):
        """12.1 第 5 步。调用方持有实例锁；返回 True 表示锁已交给后台任务。"""
        vm = self.db.get_vm(vm_id)
        if vm is None or vm["status"] not in ("starting", "running"):
            return False
        if not container_running:
            return self._fail(vm, "CONTAINER_EXITED", "容器已退出", container_running=False)
        if vm["status"] == "starting":
            if healthy:
                if self.db.update_vm(vm_id, {"status": "running", "health_failures": 0}, statuses=["starting"]):
                    log.info("vm %s running", vm_id)
                    return self._handoff(vm_id, self._auto_start_recording)
            elif self.clock() > from_iso(vm["started_at"]) + timedelta(seconds=self.settings["boot_timeout_seconds"]):
                return self._fail(vm, "STARTUP_TIMEOUT",
                                  f"开始创建后 {self.settings['boot_timeout_seconds']} 秒仍未就绪")
            return False
        if healthy:
            if vm["health_failures"]:
                self.db.update_vm(vm_id, {"health_failures": 0}, statuses=["running"])
            return False
        failures = vm["health_failures"] + 1
        self.db.update_vm(vm_id, {"health_failures": failures}, statuses=["running"])
        log.warning("vm %s health check failed (%d in a row)", vm_id, failures)
        if failures >= self.settings["health_failure_threshold"]:
            return self._fail(vm, "HEALTH_CHECK_FAILED", f"运行中连续 {failures} 次检查失败")
        return False

    def _auto_start_recording(self, vm_id):
        self.recordings.start_locked(vm_id, None, manual=False)

    def _fail(self, vm, code, message, container_running=True):
        """starting/running → failed。调用方持有实例锁。
        容器仍在运行时，交给后台先结束采集、再停止容器（不删除），返回 True。"""
        fields = {"status": "failed", "error_code": code, "error_message": message, "stop_reason": "failure"}
        if not container_running:
            fields.update(reserved=0, stopped_at=to_iso(self.clock()))
        if not self.db.update_vm(vm["vm_id"], fields, statuses=[vm["status"]]):
            return False
        log.warning("vm %s failed: %s (%s)", vm["vm_id"], code, message)
        if not container_running:
            self.recordings.end_for_vm(vm, container_exited=True)
            return False
        return self._handoff(vm["vm_id"], self._stop_failed)

    def _stop_failed(self, vm_id):
        """failed 且 reserved=1：结束采集、停止容器（不删除），确认停止后释放资源。"""
        vm = self.db.get_vm(vm_id)
        if vm is None or vm["status"] != "failed" or not vm["reserved"]:
            return
        try:
            self._stop_and_release(vm_id)
        except Exception:
            log.exception("vm %s stop after failure failed", vm_id)
            self.db.update_vm(vm_id, {"last_retry_at": to_iso(self.clock())})

    def _resume_interrupted(self, now):
        """实例锁空闲的 stopping、deleting 记录，以及 reserved=1 的 failed 记录，按原来的意图继续。"""
        for vm in self.db.vms_with_status("stopping", "deleting", "failed"):
            if vm["status"] == "failed" and not vm["reserved"]:
                continue
            last = from_iso(vm["last_retry_at"])
            if last and (now - last).total_seconds() < RETRY_INTERVAL_SECONDS:
                continue
            action = {"stopping": self._resume_stop, "deleting": self._resume_delete,
                      "failed": self._stop_failed}[vm["status"]]
            self._submit_locked(vm["vm_id"], action)

    def _resume_stop(self, vm_id):
        vm = self.db.get_vm(vm_id)
        if vm is None or vm["status"] != "stopping":
            return
        try:
            self._stop_and_release(vm_id)
        except Exception:
            log.exception("vm %s resumed stop failed", vm_id)
            self.db.update_vm(vm_id, {"last_retry_at": to_iso(self.clock())})

    def _resume_delete(self, vm_id):
        vm = self.db.get_vm(vm_id)
        if vm is None or vm["status"] != "deleting":
            return
        try:
            self._delete_locked(vm_id)
        except Exception:
            log.exception("vm %s resumed delete failed", vm_id)
            self.db.update_vm(vm_id, {"last_retry_at": to_iso(self.clock())})

    def _set_queue_reason(self, vm, reason):
        if vm["queue_reason"] != reason:
            self.db.update_vm(vm["vm_id"], {"queue_reason": reason}, statuses=["queued"])

    def _schedule(self):
        """12.1 第 7 步：严格 FIFO，只看队首；每轮最多启动一台。"""
        while True:
            head = self.db.queue_head()
            if head is None:
                return
            vm_id = head["vm_id"]
            if not self.backend.ping():
                self._set_queue_reason(head, "docker_unavailable")
                return
            try:
                missing = self.host_missing(json.loads(head["environment_json"]))
            except DockerUnavailable:
                self._set_queue_reason(head, "docker_unavailable")
                return
            if missing:
                if self.db.update_vm(vm_id, {
                    "status": "failed", "error_code": "HOST_NOT_READY",
                    "error_message": f"宿主机环境未就绪，缺少：{', '.join(missing)}",
                }, statuses=["queued"]):
                    log.warning("vm %s failed: HOST_NOT_READY %s", vm_id, missing)
                continue  # 不阻塞后面的队列

            lock = self.instance_locks.get(vm_id)
            with self.scheduler_lock:
                try:
                    fits = self._fits(head)
                except DockerUnavailable:
                    self._set_queue_reason(head, "docker_unavailable")
                    return
                if not fits:
                    self._set_queue_reason(head, "insufficient_resources")
                    return
                if not lock.acquire(blocking=False):
                    return
                now = self.clock()
                claimed = self.db.update_vm(vm_id, {
                    "status": "creating",
                    "reserved": 1,
                    "queue_reason": None,
                    "started_at": to_iso(now),
                    "expires_at": to_iso(now + timedelta(minutes=head["timeout_minutes"])),
                    "last_retry_at": None,
                }, statuses=["queued"])
            if not claimed:
                lock.release()
                continue
            log.info("vm %s claimed from queue, creating", vm_id)
            try:
                self._provision(vm_id)
            finally:
                lock.release()
            return

    # ==================== 服务重启恢复（第 13 节） ====================

    def recover(self):
        """在开始接收请求之前执行。Docker 无法连接时不修改任何虚拟机的状态，等第一轮可连接的巡检再处理。"""
        try:
            self.recover_vms()
        except DockerUnavailable:
            log.warning("Docker unavailable at startup; VM recovery deferred to the inspector")
            self._recovery_pending = True
        self.workspaces.recover()

    def recover_vms(self):
        containers = self.backend.list_managed()
        known = set()
        for vm in self.db.list_vms():
            vm_id, status = vm["vm_id"], vm["status"]
            known.add(vm_id)
            if status in ("queued", "stopped"):
                continue
            # 1. 补齐容器信息
            info = self.backend.find_container(vm_id)
            if info is not None:
                fields = {"container_id": info["id"]}
                if info["running"] and info["ports"]["agent"] is not None:
                    fields["ports"] = json.dumps(info["ports"])
                self.db.update_vm(vm_id, fields)
            running = info is not None and info["running"]
            now = self.clock()
            # 2. 按状态处理
            if status == "creating":
                if running:
                    self.db.update_vm(vm_id, {"status": "starting", "ever_started": 1}, statuses=["creating"])
                    log.info("recover: vm %s creating -> starting", vm_id)
                elif not vm["ever_started"]:
                    fields = {"status": "failed", "error_code": "PROVISION_FAILED",
                              "error_message": "服务重启时首次创建被中断", "reserved": 0}
                    if info is not None:
                        try:
                            self.backend.remove_container(info["id"])
                            fields.update(container_id=None, ports=None)
                        except Exception:
                            log.exception("recover: removing leftover container of %s failed", vm_id)
                    self.db.update_vm(vm_id, fields, statuses=["creating"])
                    log.warning("recover: vm %s creating -> failed (PROVISION_FAILED)", vm_id)
                else:
                    self.db.update_vm(vm_id, {
                        "status": "queued", "reserved": 0, "started_at": None, "expires_at": None,
                        "queue_deadline": to_iso(now + timedelta(minutes=vm["queue_timeout_minutes"])),
                    }, statuses=["creating"])
                    log.info("recover: vm %s creating -> queued (restart interrupted, C: kept)", vm_id)
            elif status in ("starting", "running") and not running:
                self.db.update_vm(vm_id, {
                    "status": "failed", "error_code": "CONTAINER_EXITED", "error_message": "服务重启时容器已退出",
                    "reserved": 0, "stop_reason": "failure", "stopped_at": to_iso(now),
                }, statuses=[status])
                log.warning("recover: vm %s %s -> failed (CONTAINER_EXITED)", vm_id, status)
        for vm_id in containers:
            if vm_id not in known:
                log.warning("recover: managed container for unknown vm %s left untouched", vm_id)
        # 3. 采集记录
        self.recordings.recover()
