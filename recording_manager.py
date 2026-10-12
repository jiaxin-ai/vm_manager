"""轨迹采集：采集记录、启停、状态（第 10 节）。"""
import json
import logging
import os

from backend import AgentCallFailed
from common import ApiError, to_iso, utc_now

log = logging.getLogger(__name__)

MAX_START_ATTEMPTS = 3
ERROR_VM_EXITED = "虚拟机已退出，采集未正常结束"
ERROR_NOT_RUNNING_AT_RESTART = "服务重启时虚拟机不在运行"

PUBLIC_FIELDS = ("recording_id", "vm_id", "project_id", "task_id", "chat_id",
                 "status", "started_at", "stopped_at", "result_path", "error")


class RecordingManager:
    def __init__(self, db, backend, workspaces, settings, instance_locks, clock=utc_now):
        self.db = db
        self.backend = backend
        self.workspaces = workspaces
        self.settings = settings
        self.instance_locks = instance_locks  # 与 VmManager 共用的实例锁
        self.clock = clock

    # ---------- 查询 ----------

    @staticmethod
    def public(rec):
        return {k: rec[k] for k in PUBLIC_FIELDS}

    def get(self, recording_id):
        rec = self.db.get_recording(recording_id)
        if rec is None:
            raise ApiError(404, "RECORDING_NOT_FOUND", "采集记录不存在", {"recording_id": recording_id})
        return rec

    def list(self, project_id=None, task_id=None, vm_id=None):
        return [self.public(rec) for rec in self.db.list_recordings(project_id, task_id, vm_id)]

    # ---------- 开始采集（10.2） ----------

    def start(self, vm_id, chat_id=None):
        """手动开始：拿不到实例锁时返回 409 VM_NOT_RUNNING。"""
        vm = self.db.get_vm(vm_id)
        if vm is None:
            raise ApiError(404, "VM_NOT_FOUND", "虚拟机不存在", {"vm_id": vm_id})
        lock = self.instance_locks.get(vm_id)
        if not lock.acquire(blocking=False):
            raise ApiError(409, "VM_NOT_RUNNING", "虚拟机不在 running 状态", {"status": vm["status"]})
        try:
            return self.start_locked(vm_id, chat_id, manual=True)
        finally:
            lock.release()

    def start_locked(self, vm_id, chat_id=None, manual=False):
        """手动开始、自动开始共用（10.2）。调用方持有实例锁。
        自动开始时，巡检线程在 starting → running 的同一把实例锁内把锁交给后台线程执行本函数；
        巡检线程重试未确认的记录时也走这里。"""
        vm = self.db.get_vm(vm_id)
        if vm is None or vm["status"] != "running":
            if manual and vm is None:
                raise ApiError(404, "VM_NOT_FOUND", "虚拟机不存在", {"vm_id": vm_id})
            if manual:
                raise ApiError(409, "VM_NOT_RUNNING", "虚拟机不在 running 状态", {"status": vm["status"]})
            return None

        rec = self.db.active_recording(vm_id)
        if rec is not None and rec["confirmed"]:
            return self.public(rec)
        if rec is None:
            recording_id = self.db.new_recording_id()
            path = self.workspaces.recording_dir(vm["project_id"], vm_id, recording_id)
            os.makedirs(path, exist_ok=True)
            rec = {
                "recording_id": recording_id,
                "vm_id": vm_id,
                "project_id": vm["project_id"],
                "task_id": vm["task_id"],
                "chat_id": chat_id if chat_id is not None else vm["chat_id"],
                "status": "recording",
                "confirmed": 0,
                "start_attempts": 0,
                "started_at": to_iso(self.clock()),
                "stopped_at": None,
                "result_path": path,
                "error": None,
            }
            self.db.insert_recording(rec)
            log.info("recording %s created for %s", recording_id, vm_id)

        self._call_start(vm, rec, manual)
        return self.public(self.db.get_recording(rec["recording_id"]))

    def _call_start(self, vm, rec, manual):
        recording_id = rec["recording_id"]
        body = {
            "recording_id": recording_id,
            "output_dir": f"G:\\{recording_id}",
            "project_id": rec["project_id"],
            "task_id": rec["task_id"],
            "vm_id": rec["vm_id"],
            "chat_id": rec["chat_id"],
        }
        try:
            self._agent(vm, self.settings["recorder_start_path"], body)
        except AgentCallFailed as exc:
            attempts = rec["start_attempts"] + 1
            log.warning("recording %s start attempt %d failed: %s", recording_id, attempts, exc)
            if attempts >= MAX_START_ATTEMPTS:
                # 先用同一个 recording_id 停止一次，确保没有遗留的采集进程
                try:
                    self._agent(vm, self.settings["recorder_stop_path"], {"recording_id": recording_id})
                except AgentCallFailed as stop_exc:
                    log.warning("recording %s cleanup stop failed: %s", recording_id, stop_exc)
                self.db.update_recording(recording_id, {
                    "start_attempts": attempts,
                    "status": "failed",
                    "stopped_at": to_iso(self.clock()),
                    "error": f"开始采集失败 {attempts} 次：{exc}",
                }, statuses=["recording"])
            else:
                self.db.update_recording(recording_id, {"start_attempts": attempts}, statuses=["recording"])
            if manual:
                raise ApiError(500, "RECORDING_START_FAILED", "虚拟机内的采集程序启动失败", {"vm_id": rec["vm_id"]})
            return
        self.db.update_recording(recording_id, {"confirmed": 1}, statuses=["recording"])
        log.info("recording %s started", recording_id)

    # ---------- 结束采集（10.3） ----------

    def stop(self, recording_id):
        """手动停止：等待实例锁后重新读取记录。"""
        rec = self.get(recording_id)
        with self.instance_locks.get(rec["vm_id"]):
            rec = self.db.get_recording(recording_id)
            if rec["status"] != "recording":
                return self.public(rec)
            vm = self.db.get_vm(rec["vm_id"])
            if vm is None or vm["status"] != "running":
                self._mark_failed(rec, ERROR_VM_EXITED)
            else:
                try:
                    self._agent(vm, self.settings["recorder_stop_path"], {"recording_id": recording_id})
                except AgentCallFailed as exc:
                    log.warning("recording %s stop failed: %s", recording_id, exc)
                    raise ApiError(500, "RECORDING_STOP_FAILED", "虚拟机内的采集程序停止失败，可再次调用重试",
                                   {"recording_id": recording_id})
                self._mark_stopped(rec)
            return self.public(self.db.get_recording(recording_id))

    def end_for_vm(self, vm, container_exited=False):
        """离开 running 时自动结束采集。调用方已持有实例锁；失败不阻塞后续流程。"""
        rec = self.db.active_recording(vm["vm_id"])
        if rec is None:
            return
        if container_exited:
            self._mark_failed(rec, ERROR_VM_EXITED)
            return
        try:
            self._agent(vm, self.settings["recorder_stop_path"], {"recording_id": rec["recording_id"]})
        except AgentCallFailed as exc:
            log.warning("recording %s auto stop failed: %s", rec["recording_id"], exc)
            self._mark_failed(rec, f"停止采集失败：{exc}")
            return
        self._mark_stopped(rec)

    def _mark_stopped(self, rec):
        self.db.update_recording(rec["recording_id"], {"status": "stopped", "stopped_at": to_iso(self.clock())},
                                 statuses=["recording"])
        log.info("recording %s stopped", rec["recording_id"])

    def _mark_failed(self, rec, error):
        self.db.update_recording(rec["recording_id"], {
            "status": "failed", "stopped_at": to_iso(self.clock()), "error": error,
        }, statuses=["recording"])
        log.warning("recording %s failed: %s", rec["recording_id"], error)

    def _agent(self, vm, path, body):
        agent_port = json.loads(vm["ports"] or "{}").get("agent")
        self.backend.agent_post(agent_port, path, body, self.settings["recorder_timeout_seconds"])

    # ---------- 巡检与恢复 ----------

    def unconfirmed_running_vm_ids(self):
        """所属虚拟机为 running、状态为 recording 且未确认的记录对应的虚拟机。"""
        result = []
        for rec in self.db.recordings_in_progress():
            vm = self.db.get_vm(rec["vm_id"])
            if not rec["confirmed"] and vm is not None and vm["status"] == "running":
                result.append(rec["vm_id"])
        return result

    def recover(self):
        """13 第 3 步：虚拟机已不在 running 的进行中记录改为 failed。"""
        for rec in self.db.recordings_in_progress():
            vm = self.db.get_vm(rec["vm_id"])
            if vm is None or vm["status"] != "running":
                self._mark_failed(rec, ERROR_NOT_RUNNING_AT_RESTART)
