"""任务空间：自动创建与并发创建、冻结/解冻幂等、pending_op、复制及失败清理、mounted_by。"""
import os
import threading

from conftest import Harness, error_code


def ws_row(h, ws_id):
    return h.db.get_workspace(ws_id)


def test_auto_create_and_reuse(h):
    first = h.create().json()["data"]
    second = h.create().json()["data"]
    assert first["workspace_id"] == second["workspace_id"]
    ws = ws_row(h, first["workspace_id"])
    assert ws["status"] == "writable" and ws["path"] == os.path.join(h.data_root, "projects/p1/tasks/t1")
    for d in ("shared", "tasks/t1", f"recordings/{first['vm_id']}", f"recordings/{second['vm_id']}"):
        assert os.path.isdir(os.path.join(h.data_root, "projects/p1", d))


def test_create_workspace_response(h):
    data = h.client.post("/v1/workspaces", json={"project_id": "p1", "task_id": "tA"}).json()["data"]
    assert data["workspace_id"].startswith("ws_") and len(data["workspace_id"]) == 11
    assert data == {
        "workspace_id": data["workspace_id"], "project_id": "p1", "task_id": "tA", "status": "writable",
        "path": os.path.join(h.data_root, "projects/p1/tasks/tA"), "source_workspace_id": None,
        "mounted_by": [], "created_at": "2026-10-11T10:00:00Z", "frozen_at": None,
    }
    assert os.path.isdir(data["path"])


def test_concurrent_creation_of_same_workspace(tmp_path):
    h = Harness(tmp_path)
    barrier = threading.Barrier(12)
    results = []

    def explicit():
        barrier.wait()
        results.append(h.client.post("/v1/workspaces", json={"project_id": "p1", "task_id": "t1"}).status_code)

    def auto():
        barrier.wait()
        results.append(h.ws.ensure("p1", "t1")["workspace_id"])

    threads = [threading.Thread(target=explicit) for _ in range(6)] + [threading.Thread(target=auto) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    rows = h.db.list_workspaces()
    assert len(rows) == 1
    codes = [r for r in results if isinstance(r, int)]
    ids = [r for r in results if isinstance(r, str)]
    assert set(ids) == {rows[0]["workspace_id"]}
    assert codes.count(200) <= 1 and codes.count(409) >= 5


def test_freeze_unfreeze_idempotent(h):
    ws = h.workspace("p1", "tA")
    first = h.client.post(f"/v1/workspaces/{ws}/freeze").json()["data"]
    assert first["status"] == "frozen" and first["frozen_at"] == "2026-10-11T10:00:00Z"
    h.clock.advance(10)
    again = h.client.post(f"/v1/workspaces/{ws}/freeze").json()["data"]
    assert again == first  # 重复冻结：再执行一次 chattr，返回当前状态（冻结时间不变）
    assert [c[0] for c in h.chattr_calls] == ["+i", "+i"]
    path = ws_row(h, ws)["path"]
    assert path in h.frozen
    un = h.client.post(f"/v1/workspaces/{ws}/unfreeze").json()["data"]
    assert un["status"] == "writable" and un["frozen_at"] is None
    assert h.client.post(f"/v1/workspaces/{ws}/unfreeze").json()["data"] == un
    assert [c[0] for c in h.chattr_calls] == ["+i", "+i", "-i", "-i"]
    assert path not in h.frozen


def test_freeze_failure_keeps_pending_op_and_blocks_use(h):
    src = h.workspace("p1", "tA")
    h.chattr_fail = True
    resp = h.client.post(f"/v1/workspaces/{src}/freeze")
    assert resp.status_code == 500 and error_code(resp) == "INTERNAL_ERROR"
    row = ws_row(h, src)
    assert row["pending_op"] == "freeze" and row["status"] == "writable"
    # 冻结做到一半：不能作为复制源，也不能作为上游
    resp = h.client.post(f"/v1/workspaces/{src}/copy", json={"project_id": "p1", "task_id": "tC"})
    assert error_code(resp) == "WORKSPACE_NOT_FROZEN"
    assert error_code(h.create(upstream_task_ids=["tA"])) == "UPSTREAM_NOT_FROZEN"
    # 巡检线程在 60 秒后重试
    h.chattr_fail = False
    h.clock.advance(59)
    h.vms.run_round()
    assert ws_row(h, src)["pending_op"] == "freeze"
    h.clock.advance(1)
    h.vms.run_round()
    row = ws_row(h, src)
    assert row["pending_op"] is None and row["status"] == "frozen" and row["frozen_at"]
    assert h.client.post(f"/v1/workspaces/{src}/copy", json={"project_id": "p1", "task_id": "tC"}).status_code == 200


def test_unfreeze_failure_treated_as_not_frozen(h):
    src = h.workspace("p1", "tA", frozen=True)
    h.chattr_fail = True
    assert error_code(h.client.post(f"/v1/workspaces/{src}/unfreeze")) == "INTERNAL_ERROR"
    row = ws_row(h, src)
    assert row["pending_op"] == "unfreeze" and row["status"] == "frozen"
    resp = h.client.post(f"/v1/workspaces/{src}/copy", json={"project_id": "p1", "task_id": "tC"})
    assert error_code(resp) == "WORKSPACE_NOT_FROZEN"
    # 新请求覆盖 pending_op：改为冻结并成功
    h.chattr_fail = False
    data = h.client.post(f"/v1/workspaces/{src}/freeze").json()["data"]
    assert data["status"] == "frozen" and ws_row(h, src)["pending_op"] is None


def test_copy_success(h):
    src = h.workspace("p1", "tA")
    src_path = ws_row(h, src)["path"]
    os.makedirs(os.path.join(src_path, "sub"))
    with open(os.path.join(src_path, "sub", "a.txt"), "w") as f:
        f.write("hello")
    h.client.post(f"/v1/workspaces/{src}/freeze")
    h.clock.advance(5)
    data = h.client.post(f"/v1/workspaces/{src}/copy", json={"project_id": "p2", "task_id": "tC"}).json()["data"]
    assert data["status"] == "writable" and data["source_workspace_id"] == src
    assert data["path"] == os.path.join(h.data_root, "projects/p2/tasks/tC")
    assert data["mounted_by"] == [] and data["frozen_at"] is None and data["created_at"] == "2026-10-11T10:00:05Z"
    with open(os.path.join(data["path"], "sub", "a.txt")) as f:
        assert f.read() == "hello"
    assert ws_row(h, src)["status"] == "frozen"
    assert not [n for n in os.listdir(os.path.join(h.data_root, "projects/p2/tasks")) if n.startswith(".")]


def test_copy_target_exists(h):
    src = h.workspace("p1", "tA", frozen=True)
    existing = h.workspace("p1", "tB")
    resp = h.client.post(f"/v1/workspaces/{src}/copy", json={"project_id": "p1", "task_id": "tB"})
    assert resp.status_code == 409 and error_code(resp) == "WORKSPACE_EXISTS"
    assert resp.json()["error"]["details"] == {"task_id": "tB", "workspace_id": existing}
    # 只有目录、没有记录
    os.makedirs(os.path.join(h.data_root, "projects/p1/tasks/tD"))
    resp = h.client.post(f"/v1/workspaces/{src}/copy", json={"project_id": "p1", "task_id": "tD"})
    assert error_code(resp) == "WORKSPACE_EXISTS"
    assert resp.json()["error"]["details"] == {"task_id": "tD", "workspace_id": None}


def test_copy_failure_cleans_temp_dir(h):
    src = h.workspace("p1", "tA")
    path = ws_row(h, src)["path"]
    secret = os.path.join(path, "unreadable.txt")
    with open(secret, "w") as f:
        f.write("x")
    os.chmod(secret, 0)
    h.client.post(f"/v1/workspaces/{src}/freeze")
    try:
        if os.access(secret, os.R_OK):  # root 下无法制造读取失败
            return
        resp = h.client.post(f"/v1/workspaces/{src}/copy", json={"project_id": "p1", "task_id": "tC"})
        assert resp.status_code == 500 and error_code(resp) == "INTERNAL_ERROR"
        assert sorted(os.listdir(os.path.join(h.data_root, "projects/p1/tasks"))) == ["tA"]
        assert h.db.get_workspace_by_task("p1", "tC") is None
    finally:
        os.chmod(secret, 0o644)


def test_copy_db_failure_after_rename_removes_target(h, monkeypatch):
    src = h.workspace("p1", "tA", frozen=True)

    def broken_insert(row):
        raise RuntimeError("db is broken")

    monkeypatch.setattr(h.db, "insert_workspace", broken_insert)
    resp = h.client.post(f"/v1/workspaces/{src}/copy", json={"project_id": "p1", "task_id": "tC"})
    assert resp.status_code == 500 and error_code(resp) == "INTERNAL_ERROR"
    assert sorted(os.listdir(os.path.join(h.data_root, "projects/p1/tasks"))) == ["tA"]
    monkeypatch.undo()
    # 之后可以再次复制，不会一直返回 WORKSPACE_EXISTS
    assert h.client.post(f"/v1/workspaces/{src}/copy", json={"project_id": "p1", "task_id": "tC"}).status_code == 200


def test_mounted_by(h):
    up = h.workspace("p1", "tA", frozen=True)
    a = h.create_ok(task_id="tA")  # D:
    b = h.create_ok(task_id="tB", upstream_task_ids=["tA"])  # E:\tA
    c = h.create_ok(task_id="tC", upstream_task_ids=["tA"])
    h.client.post(f"/v1/vms/{c}/stop")  # 已停止的也算
    d = h.create_ok(task_id="tD", upstream_task_ids=["tA"])
    h.client.delete(f"/v1/vms/{d}")  # 已删除的不算
    data = h.client.get(f"/v1/workspaces/{up}").json()["data"]
    assert data["mounted_by"] == [{"vm_id": a, "path": "D:"}, {"vm_id": b, "path": "E:\\tA"},
                                  {"vm_id": c, "path": "E:\\tA"}]
    # 本任务空间已冻结时仍允许创建虚拟机，D 盘只读
    assert h.detail(a)["mounts"]["workspace"]["writable"] is False


def test_list_workspaces(h):
    a = h.workspace("p1", "tA", frozen=True)
    h.clock.advance(1)
    b = h.workspace("p1", "tB")
    h.clock.advance(1)
    c = h.workspace("p2", "tA")
    data = h.client.get("/v1/workspaces").json()["data"]
    assert data["total"] == 3 and [i["workspace_id"] for i in data["items"]] == [c, b, a]
    assert set(data["items"][0]) == {"workspace_id", "project_id", "task_id", "status", "path"}
    data = h.client.get("/v1/workspaces", params={"project_id": "p1", "status": "frozen"}).json()["data"]
    assert data == {"total": 1, "items": [{"workspace_id": a, "project_id": "p1", "task_id": "tA",
                                           "status": "frozen", "path": ws_row(h, a)["path"]}]}
    assert h.client.get("/v1/workspaces", params={"task_id": "tA"}).json()["data"]["total"] == 2


def test_path_must_stay_under_projects(h):
    os.symlink(str(h.tmp), os.path.join(h.data_root, "projects", "evil"))
    resp = h.client.post("/v1/workspaces", json={"project_id": "evil", "task_id": "t1"})
    assert resp.status_code == 500
    assert h.db.list_workspaces() == []
