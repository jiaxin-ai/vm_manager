"""SQLite 存储：建表、查询、条件更新（第 7 节）。

数据库是唯一的状态来源。每个线程使用独立的连接；所有状态变更都用条件更新（CAS）。
"""
import secrets
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS vms (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    vm_id TEXT UNIQUE NOT NULL,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    chat_id TEXT,
    environment_id TEXT NOT NULL,
    environment_json TEXT NOT NULL,
    cpu_cores INTEGER NOT NULL,
    ram_gb INTEGER NOT NULL,
    expose TEXT NOT NULL,
    timeout_minutes INTEGER NOT NULL,
    queue_timeout_minutes INTEGER NOT NULL,
    workspace_id TEXT NOT NULL,
    upstreams TEXT NOT NULL,
    status TEXT NOT NULL,
    container_id TEXT,
    ports TEXT,
    reserved INTEGER NOT NULL DEFAULT 0,
    health_failures INTEGER NOT NULL DEFAULT 0,
    ever_started INTEGER NOT NULL DEFAULT 0,
    queue_order INTEGER,
    queue_deadline TEXT,
    queue_reason TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    expires_at TEXT,
    stopped_at TEXT,
    stop_reason TEXT,
    error_code TEXT,
    error_message TEXT,
    last_retry_at TEXT
);
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    pending_op TEXT,
    path TEXT NOT NULL,
    source_workspace_id TEXT,
    created_at TEXT NOT NULL,
    frozen_at TEXT,
    last_retry_at TEXT,
    UNIQUE (project_id, task_id)
);
CREATE TABLE IF NOT EXISTS recordings (
    recording_id TEXT PRIMARY KEY,
    vm_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    chat_id TEXT,
    status TEXT NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    start_attempts INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    result_path TEXT NOT NULL,
    error TEXT
);
"""


class Database:
    def __init__(self, path):
        self.path = path
        self._local = threading.local()

    # ---------- 基础 ----------

    def conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def init_schema(self):
        self.conn().executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        conn = self.conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def _one(self, sql, params=()):
        row = self.conn().execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql, params=()):
        return [dict(row) for row in self.conn().execute(sql, params).fetchall()]

    def _insert(self, conn, table, row):
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(row.values()))

    def _update(self, table, key_column, key, fields, statuses=None):
        """条件更新：给出 statuses 时，要求当前状态在其中。返回是否恰好更新了一行。"""
        sql = f"UPDATE {table} SET {', '.join(f'{k}=?' for k in fields)} WHERE {key_column}=?"
        params = [*fields.values(), key]
        if statuses:
            sql += f" AND status IN ({', '.join('?' for _ in statuses)})"
            params += list(statuses)
        return self.conn().execute(sql, params).rowcount == 1

    def _id_exists(self, sql, candidate):
        return self._one(sql, (candidate,)) is not None

    def new_vm_id(self):
        # 同时检查采集记录，避免新虚拟机复用已删除虚拟机的 ID 和采集目录
        while True:
            candidate = f"vm_{secrets.token_hex(4)}"
            if not (
                self._id_exists("SELECT 1 FROM vms WHERE vm_id=?", candidate)
                or self._id_exists("SELECT 1 FROM recordings WHERE vm_id=?", candidate)
            ):
                return candidate

    def new_workspace_id(self):
        while True:
            candidate = f"ws_{secrets.token_hex(4)}"
            if not self._id_exists("SELECT 1 FROM workspaces WHERE workspace_id=?", candidate):
                return candidate

    def new_recording_id(self):
        while True:
            candidate = f"rec_{secrets.token_hex(4)}"
            if not self._id_exists("SELECT 1 FROM recordings WHERE recording_id=?", candidate):
                return candidate

    # ---------- vms ----------

    def get_vm(self, vm_id):
        return self._one("SELECT * FROM vms WHERE vm_id=?", (vm_id,))

    def list_vms(self, project_id=None, task_id=None, statuses=None):
        sql, params = "SELECT * FROM vms WHERE 1=1", []
        if project_id is not None:
            sql += " AND project_id=?"
            params.append(project_id)
        if task_id is not None:
            sql += " AND task_id=?"
            params.append(task_id)
        if statuses:
            sql += f" AND status IN ({', '.join('?' for _ in statuses)})"
            params += list(statuses)
        return self._all(sql + " ORDER BY seq DESC", params)

    def vms_with_status(self, *statuses):
        return self._all(
            f"SELECT * FROM vms WHERE status IN ({', '.join('?' for _ in statuses)}) ORDER BY seq",
            statuses,
        )

    def reserved_vms(self):
        return self._all("SELECT * FROM vms WHERE reserved=1")

    def count_queued(self):
        return self._one("SELECT COUNT(*) AS n FROM vms WHERE status='queued'")["n"]

    def _queue_position(self, conn, vm_id):
        row = conn.execute(
            "SELECT COUNT(*) FROM vms WHERE status='queued' AND queue_order <= "
            "(SELECT queue_order FROM vms WHERE vm_id=?)",
            (vm_id,),
        ).fetchone()
        return row[0]

    def queue_position(self, vm_id):
        return self._queue_position(self.conn(), vm_id)

    def queue_head(self):
        return self._one("SELECT * FROM vms WHERE status='queued' ORDER BY queue_order LIMIT 1")

    def _next_queue_order(self, conn):
        return conn.execute("SELECT COALESCE(MAX(queue_order), 0) + 1 FROM vms").fetchone()[0]

    def insert_queued_vm(self, row):
        """写入一条 queued 记录，在同一个事务中分配 queue_order 并计算排队序号。"""
        with self.transaction() as conn:
            row = dict(row, queue_order=self._next_queue_order(conn))
            self._insert(conn, "vms", row)
            return self._queue_position(conn, row["vm_id"])

    def requeue_vm(self, vm_id, fields):
        """stopped → queued，排到队尾。返回排队序号；状态已变化时返回 None。"""
        with self.transaction() as conn:
            fields = dict(fields, status="queued", queue_order=self._next_queue_order(conn))
            sets = ", ".join(f"{k}=?" for k in fields)
            cursor = conn.execute(
                f"UPDATE vms SET {sets} WHERE vm_id=? AND status='stopped'", (*fields.values(), vm_id)
            )
            if cursor.rowcount != 1:
                return None
            return self._queue_position(conn, vm_id)

    def update_vm(self, vm_id, fields, statuses=None):
        return self._update("vms", "vm_id", vm_id, fields, statuses)

    def delete_vm(self, vm_id, status=None):
        sql, params = "DELETE FROM vms WHERE vm_id=?", [vm_id]
        if status is not None:
            sql += " AND status=?"
            params.append(status)
        return self.conn().execute(sql, params).rowcount == 1

    # ---------- workspaces ----------

    def get_workspace(self, workspace_id):
        return self._one("SELECT * FROM workspaces WHERE workspace_id=?", (workspace_id,))

    def get_workspace_by_task(self, project_id, task_id):
        return self._one(
            "SELECT * FROM workspaces WHERE project_id=? AND task_id=?", (project_id, task_id)
        )

    def list_workspaces(self, project_id=None, task_id=None, status=None):
        sql, params = "SELECT * FROM workspaces WHERE 1=1", []
        for column, value in (("project_id", project_id), ("task_id", task_id), ("status", status)):
            if value is not None:
                sql += f" AND {column}=?"
                params.append(value)
        return self._all(sql + " ORDER BY created_at DESC, rowid DESC", params)

    def workspaces_with_pending_op(self):
        return self._all("SELECT * FROM workspaces WHERE pending_op IS NOT NULL")

    def insert_workspace(self, row):
        self._insert(self.conn(), "workspaces", row)

    def update_workspace(self, workspace_id, fields):
        return self._update("workspaces", "workspace_id", workspace_id, fields)

    def vms_in_project(self, project_id):
        return self._all("SELECT * FROM vms WHERE project_id=? ORDER BY seq", (project_id,))

    # ---------- recordings ----------

    def get_recording(self, recording_id):
        return self._one("SELECT * FROM recordings WHERE recording_id=?", (recording_id,))

    def active_recording(self, vm_id):
        """这台虚拟机 recording 状态的记录（同一时间最多一条）。"""
        return self._one(
            "SELECT * FROM recordings WHERE vm_id=? AND status='recording' ORDER BY rowid DESC LIMIT 1",
            (vm_id,),
        )

    def recordings_in_progress(self):
        return self._all("SELECT * FROM recordings WHERE status='recording' ORDER BY rowid")

    def list_recordings(self, project_id=None, task_id=None, vm_id=None):
        sql, params = "SELECT * FROM recordings WHERE 1=1", []
        for column, value in (("project_id", project_id), ("task_id", task_id), ("vm_id", vm_id)):
            if value is not None:
                sql += f" AND {column}=?"
                params.append(value)
        return self._all(sql + " ORDER BY started_at DESC, rowid DESC", params)

    def insert_recording(self, row):
        self._insert(self.conn(), "recordings", row)

    def update_recording(self, recording_id, fields, statuses=None):
        return self._update("recordings", "recording_id", recording_id, fields, statuses)
