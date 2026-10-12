"""目录与任务空间：路径规则、创建、冻结/解冻、复制、挂载关联（第 5、6 节）。"""
import json
import logging
import os
import re
import secrets
import shutil
import subprocess

from common import ApiError, KeyedLocks, from_iso, to_iso, utc_now

log = logging.getLogger(__name__)

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RETRY_INTERVAL_SECONDS = 60


class WorkspaceManager:
    def __init__(self, db, data_root, clock=utc_now):
        self.db = db
        self.clock = clock
        self.projects_root = os.path.join(os.path.realpath(data_root), "projects")
        self.create_locks = KeyedLocks()  # key: (project_id, task_id)
        self.workspace_locks = KeyedLocks()  # key: workspace_id

    # ---------- 路径规则（5.1） ----------

    def _path(self, *parts):
        for part in parts:
            if not ID_PATTERN.match(part):
                raise ValueError(f"illegal path component: {part!r}")
        path = os.path.join(self.projects_root, *parts)
        if not os.path.realpath(path).startswith(self.projects_root + os.sep):
            raise ValueError(f"path escapes {self.projects_root}: {path}")
        return path

    def tasks_root(self, project_id):
        return os.path.join(self._path(project_id), "tasks")

    def task_dir(self, project_id, task_id):
        return self._path(project_id, "tasks", task_id)

    def shared_dir(self, project_id):
        return self._path(project_id, "shared")

    def recordings_dir(self, project_id, vm_id):
        return self._path(project_id, "recordings", vm_id)

    def recording_dir(self, project_id, vm_id, recording_id):
        return self._path(project_id, "recordings", vm_id, recording_id)

    # ---------- 查询 ----------

    def get(self, workspace_id):
        ws = self.db.get_workspace(workspace_id)
        if ws is None:
            raise ApiError(404, "WORKSPACE_NOT_FOUND", "任务空间不存在", {"workspace_id": workspace_id})
        return ws

    def list(self, project_id=None, task_id=None, status=None):
        rows = self.db.list_workspaces(project_id, task_id, status)
        return [self.summary(ws) for ws in rows]

    def summary(self, ws):
        return {k: ws[k] for k in ("workspace_id", "project_id", "task_id", "status", "path")}

    def detail(self, ws):
        return {
            **self.summary(ws),
            "source_workspace_id": ws["source_workspace_id"],
            "mounted_by": self.mounted_by(ws),
            "created_at": ws["created_at"],
            "frozen_at": ws["frozen_at"],
        }

    def mounted_by(self, ws):
        """6.4：未删除的虚拟机中，以它为 D 盘或在 upstreams 中引用它的。"""
        result = []
        for vm in self.db.vms_in_project(ws["project_id"]):
            if vm["workspace_id"] == ws["workspace_id"]:
                result.append({"vm_id": vm["vm_id"], "path": "D:"})
            elif any(u["workspace_id"] == ws["workspace_id"] for u in json.loads(vm["upstreams"])):
                result.append({"vm_id": vm["vm_id"], "path": f"E:\\{ws['task_id']}"})
        return result

    @staticmethod
    def is_frozen(ws):
        """pending_op 不为空时一律视为未冻结（6.2）。"""
        return ws["status"] == "frozen" and ws["pending_op"] is None

    def resolve_upstreams(self, project_id, task_ids):
        """创建虚拟机的第 5、6 项校验。返回 [{"task_id", "workspace_id"}]。"""
        found = {tid: self.db.get_workspace_by_task(project_id, tid) for tid in task_ids}
        missing = [tid for tid, ws in found.items() if ws is None]
        if missing:
            raise ApiError(422, "UPSTREAM_NOT_FOUND", "上游任务没有任务空间", {"task_ids": missing})
        not_frozen = [tid for tid, ws in found.items() if not self.is_frozen(ws)]
        if not_frozen:
            raise ApiError(409, "UPSTREAM_NOT_FROZEN", "上游任务的任务空间尚未冻结", {"task_ids": not_frozen})
        return [{"task_id": tid, "workspace_id": ws["workspace_id"]} for tid, ws in found.items()]

    # ---------- 创建（6.1） ----------

    def create(self, project_id, task_id):
        """显式创建：已有记录时返回 409 WORKSPACE_EXISTS。"""
        with self.create_locks.get((project_id, task_id)):
            existing = self.db.get_workspace_by_task(project_id, task_id)
            if existing is not None:
                raise ApiError(409, "WORKSPACE_EXISTS", "该任务已有任务空间",
                               {"task_id": task_id, "workspace_id": existing["workspace_id"]})
            return self.detail(self._create_locked(project_id, task_id))

    def ensure(self, project_id, task_id):
        """创建虚拟机时使用：有记录就复用，没有就自动创建。"""
        with self.create_locks.get((project_id, task_id)):
            existing = self.db.get_workspace_by_task(project_id, task_id)
            return existing if existing is not None else self._create_locked(project_id, task_id)

    def _create_locked(self, project_id, task_id):
        path = self.task_dir(project_id, task_id)
        os.makedirs(path, exist_ok=True)
        ws = {
            "workspace_id": self.db.new_workspace_id(),
            "project_id": project_id,
            "task_id": task_id,
            "status": "writable",
            "pending_op": None,
            "path": path,
            "source_workspace_id": None,
            "created_at": to_iso(self.clock()),
            "frozen_at": None,
            "last_retry_at": None,
        }
        self.db.insert_workspace(ws)
        log.info("workspace %s created at %s", ws["workspace_id"], path)
        return ws

    # ---------- 冻结与解冻（6.2） ----------

    def freeze(self, workspace_id):
        return self._change_permission(workspace_id, "freeze")

    def unfreeze(self, workspace_id):
        return self._change_permission(workspace_id, "unfreeze")

    def _change_permission(self, workspace_id, op):
        self.get(workspace_id)
        with self.workspace_locks.get(workspace_id):
            self.db.update_workspace(workspace_id, {"pending_op": op})
            try:
                self._finish_pending(self.db.get_workspace(workspace_id))
            except Exception:
                raise ApiError(500, "INTERNAL_ERROR", "修改任务空间权限失败，将自动重试")
            return self.detail(self.db.get_workspace(workspace_id))

    def _finish_pending(self, ws):
        """执行 pending_op 对应的 chattr；成功后更新状态并清空 pending_op。调用方持有任务空间锁。"""
        op = ws["pending_op"]
        try:
            self.chattr("+i" if op == "freeze" else "-i", ws["path"])
        except Exception:
            log.exception("chattr for %s (%s) failed", ws["workspace_id"], op)
            self.db.update_workspace(ws["workspace_id"], {"last_retry_at": to_iso(self.clock())})
            raise
        if op == "freeze":
            fields = {"status": "frozen", "frozen_at": ws["frozen_at"] or to_iso(self.clock())}
        else:
            fields = {"status": "writable", "frozen_at": None}
        self.db.update_workspace(ws["workspace_id"], {**fields, "pending_op": None, "last_retry_at": None})
        log.info("workspace %s %s done", ws["workspace_id"], op)

    def chattr(self, flag, path):
        result = subprocess.run(["chattr", "-R", flag, path], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"chattr -R {flag} {path} failed: {result.stderr.strip()}")

    def retry_pending(self, ignore_interval=False):
        """巡检线程和服务重启时，补做未完成的冻结/解冻。"""
        now = self.clock()
        for ws in self.db.workspaces_with_pending_op():
            last = from_iso(ws["last_retry_at"])
            if not ignore_interval and last and (now - last).total_seconds() < RETRY_INTERVAL_SECONDS:
                continue
            with self.workspace_locks.get(ws["workspace_id"]):
                ws = self.db.get_workspace(ws["workspace_id"])
                if ws["pending_op"] is None:
                    continue
                try:
                    self._finish_pending(ws)
                except Exception:
                    pass  # 已记录日志，下次再试

    # ---------- 复制（6.3） ----------

    def copy(self, source_id, project_id, task_id):
        target = self.task_dir(project_id, task_id)
        with self.create_locks.get((project_id, task_id)), self.workspace_locks.get(source_id):
            source = self.get(source_id)
            if not self.is_frozen(source):
                raise ApiError(409, "WORKSPACE_NOT_FROZEN", "任务空间尚未冻结", {"workspace_id": source_id})
            existing = self.db.get_workspace_by_task(project_id, task_id)
            if existing is not None or os.path.lexists(target):
                raise ApiError(409, "WORKSPACE_EXISTS", "该任务已有任务空间", {
                    "task_id": task_id,
                    "workspace_id": existing["workspace_id"] if existing else None,
                })

            tasks_root = self.tasks_root(project_id)
            os.makedirs(tasks_root, exist_ok=True)
            tmp = os.path.join(tasks_root, f".{task_id}.copying-{secrets.token_hex(4)}")
            try:
                os.mkdir(tmp)
                result = subprocess.run(
                    ["cp", "-a", "--reflink=auto", f"{source['path']}/.", f"{tmp}/"],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"cp failed: {result.stderr.strip()}")
                os.rename(tmp, target)
            except Exception:
                log.exception("copy %s -> %s failed", source_id, target)
                shutil.rmtree(tmp, ignore_errors=True)
                raise ApiError(500, "INTERNAL_ERROR", "复制任务空间失败")

            ws = {
                "workspace_id": self.db.new_workspace_id(),
                "project_id": project_id,
                "task_id": task_id,
                "status": "writable",
                "pending_op": None,
                "path": target,
                "source_workspace_id": source_id,
                "created_at": to_iso(self.clock()),
                "frozen_at": None,
                "last_retry_at": None,
            }
            try:
                self.db.insert_workspace(ws)
            except Exception:
                # 改名成功、写数据库失败：删除目标目录，否则之后会一直返回 WORKSPACE_EXISTS
                log.exception("copy %s -> %s: database insert failed", source_id, target)
                shutil.rmtree(target, ignore_errors=True)
                raise ApiError(500, "INTERNAL_ERROR", "复制任务空间失败")
            log.info("workspace %s copied to %s (%s)", source_id, ws["workspace_id"], target)
            return self.detail(ws)

    # ---------- 服务重启恢复（13 第 4、5 步） ----------

    def recover(self):
        self.retry_pending(ignore_interval=True)
        self.cleanup_copy_residue()

    def cleanup_copy_residue(self):
        if not os.path.isdir(self.projects_root):
            return
        stamp = self.clock().strftime("%Y%m%dT%H%M%SZ")
        for project_id in sorted(os.listdir(self.projects_root)):
            tasks_root = os.path.join(self.projects_root, project_id, "tasks")
            if not ID_PATTERN.match(project_id) or not os.path.isdir(tasks_root):
                continue
            for name in sorted(os.listdir(tasks_root)):
                path = os.path.join(tasks_root, name)
                if not os.path.isdir(path) or os.path.islink(path):
                    continue
                try:
                    if name.startswith(".") and ".copying-" in name:
                        shutil.rmtree(path)
                        log.warning("removed copy residue %s", path)
                    elif ID_PATTERN.match(name) and self.db.get_workspace_by_task(project_id, name) is None:
                        orphan = os.path.join(tasks_root, f".{name}.orphan-{stamp}")
                        os.rename(path, orphan)
                        log.warning("task dir without record renamed: %s -> %s", path, orphan)
                except Exception:
                    log.exception("cleanup of %s failed", path)
