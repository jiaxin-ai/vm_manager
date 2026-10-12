"""校验与错误码：每个错误码至少一个用例；非法 ID；校验失败不留下记录和目录。"""
import pytest

from conftest import error_code


def assert_nothing_written(h):
    assert h.db.list_vms() == []
    assert h.db.list_workspaces() == []
    assert h.project_tree() == []


# ---------- VALIDATION_ERROR ----------

@pytest.mark.parametrize("mutate, field", [
    (lambda b: b.pop("project_id"), "project_id"),
    (lambda b: b.update(project_id="../etc"), "project_id"),
    (lambda b: b.update(project_id="a/b"), "project_id"),
    (lambda b: b.update(task_id="x" * 65), "task_id"),
    (lambda b: b.update(task_id=""), "task_id"),
    (lambda b: b.update(task_id=123), "task_id"),
    (lambda b: b.update(spec={"cpu_cores": "4", "ram_gb": 8}), "spec.cpu_cores"),
    (lambda b: b.update(spec={"cpu_cores": 4.0, "ram_gb": 8}), "spec.cpu_cores"),
    (lambda b: b.update(spec={"cpu_cores": 0, "ram_gb": 8}), "spec.cpu_cores"),
    (lambda b: b.update(spec={"cpu_cores": 4, "ram_gb": 8, "gpu": 1}), "spec.gpu"),
    (lambda b: b.update(expose={"vnc": "true"}), "expose.vnc"),
    (lambda b: b.update(expose={"vnc": 1}), "expose.vnc"),
    (lambda b: b.update(lifecycle={"timeout_minutes": -1}), "lifecycle.timeout_minutes"),
    (lambda b: b.update(unknown_field=1), "unknown_field"),
    (lambda b: b.update(upstream_task_ids=["a", "a"]), "upstream_task_ids"),
    (lambda b: b.update(upstream_task_ids=["t1"]), "upstream_task_ids"),
    (lambda b: b.update(upstream_task_ids=["../x"]), "upstream_task_ids.0"),
    (lambda b: b.update(chat_id=5), "chat_id"),
])
def test_create_vm_validation_error(h, mutate, field):
    body = h.body()
    mutate(body)
    resp = h.client.post("/v1/vms", json=body)
    assert resp.status_code == 422
    assert error_code(resp) == "VALIDATION_ERROR"
    assert field in [f["field"] for f in resp.json()["error"]["details"]["fields"]]
    assert_nothing_written(h)


def test_invalid_json_body(h):
    resp = h.client.post("/v1/vms", content=b"{not json", headers={"Content-Type": "application/json"})
    assert resp.status_code == 422
    assert error_code(resp) == "VALIDATION_ERROR"
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "body"
    assert_nothing_written(h)


@pytest.mark.parametrize("path, body", [
    ("/v1/workspaces", {"project_id": "../p", "task_id": "t"}),
    ("/v1/workspaces", {"project_id": "p", "task_id": "t/../../x"}),
    ("/v1/workspaces", {"project_id": "p", "task_id": "t", "extra": 1}),
    ("/v1/workspaces/ws_00000000/copy", {"project_id": "p", "task_id": ".."}),
])
def test_workspace_validation_error(h, path, body):
    resp = h.client.post(path, json=body)
    assert resp.status_code == 422
    assert error_code(resp) == "VALIDATION_ERROR"
    assert_nothing_written(h)


def test_list_filters_validation(h):
    resp = h.client.get("/v1/vms", params={"status": "running,bogus"})
    assert resp.status_code == 422 and error_code(resp) == "VALIDATION_ERROR"
    resp = h.client.get("/v1/workspaces", params={"status": "bogus"})
    assert resp.status_code == 422 and error_code(resp) == "VALIDATION_ERROR"


def test_recording_start_body_validation(h):
    vm_id = h.run_vm()
    resp = h.client.post(f"/v1/vms/{vm_id}/recordings", json={"chat_id": "c", "x": 1})
    assert resp.status_code == 422 and error_code(resp) == "VALIDATION_ERROR"


# ---------- 创建虚拟机的第 2~8 项 ----------

def test_environment_not_found(h):
    resp = h.create(environment_id="nope")
    assert resp.status_code == 422 and error_code(resp) == "ENVIRONMENT_NOT_FOUND"
    assert resp.json()["error"]["details"] == {"requested": "nope", "supported": ["env-a", "env-b"]}
    assert_nothing_written(h)


def test_below_environment_minimum(h):
    resp = h.create(environment_id="env-b", spec={"cpu_cores": 4, "ram_gb": 4})
    assert resp.status_code == 422 and error_code(resp) == "BELOW_ENVIRONMENT_MINIMUM"
    assert resp.json()["error"]["details"]["minimum"] == {"cpu_cores": 4, "ram_gb": 8}
    assert_nothing_written(h)


def test_request_exceeds_capacity(h):
    resp = h.create(spec={"cpu_cores": 15, "ram_gb": 8})  # CPU 预算 16-2=14
    assert resp.status_code == 422 and error_code(resp) == "REQUEST_EXCEEDS_CAPACITY"
    details = resp.json()["error"]["details"]
    assert details["required"] == {"cpu_cores": 15, "ram_gb": 9}
    assert details["capacity"] == {"cpu_cores": 14, "ram_gb": 60}
    resp = h.create(spec={"cpu_cores": 4, "ram_gb": 60})  # 内存 60+1 > 64-4
    assert error_code(resp) == "REQUEST_EXCEEDS_CAPACITY"
    assert_nothing_written(h)


def test_upstream_not_found(h):
    resp = h.create(upstream_task_ids=["tA"])
    assert resp.status_code == 422 and error_code(resp) == "UPSTREAM_NOT_FOUND"
    assert resp.json()["error"]["details"] == {"task_ids": ["tA"]}
    assert_nothing_written(h)


def test_upstream_in_other_project_is_not_found(h):
    h.workspace("p2", "tA", frozen=True)
    resp = h.create(upstream_task_ids=["tA"])
    assert error_code(resp) == "UPSTREAM_NOT_FOUND"


def test_upstream_not_frozen(h):
    h.workspace("p1", "tA")
    tree = h.project_tree()
    resp = h.create(upstream_task_ids=["tA"])
    assert resp.status_code == 409 and error_code(resp) == "UPSTREAM_NOT_FROZEN"
    assert resp.json()["error"]["details"] == {"task_ids": ["tA"]}
    assert h.db.list_vms() == [] and h.project_tree() == tree


def test_docker_unavailable_on_create(h):
    h.backend.docker_up = False
    resp = h.create()
    assert resp.status_code == 503 and error_code(resp) == "DOCKER_UNAVAILABLE"
    assert resp.json()["error"]["details"] is None
    assert_nothing_written(h)


@pytest.mark.parametrize("breaker, missing", [
    (lambda h: setattr(h.backend, "kvm", False), ["kvm"]),
    (lambda h: h.backend.images.clear(), ["image"]),
    (lambda h: __import__("os").remove(h.base_storage + "/base_a.img"), ["base_disk"]),
])
def test_host_not_ready_on_create(h, breaker, missing):
    breaker(h)
    resp = h.create()
    assert resp.status_code == 503 and error_code(resp) == "HOST_NOT_READY"
    assert resp.json()["error"]["details"] == {"missing": missing}
    assert_nothing_written(h)


def test_validation_order_docker_before_host(h):
    """第 7 项（Docker）先于第 8 项（KVM 等）。"""
    h.backend.docker_up = False
    h.backend.kvm = False
    assert error_code(h.create()) == "DOCKER_UNAVAILABLE"


# ---------- 404 ----------

def test_not_found_codes(h):
    for method, path, code in [
        ("get", "/v1/vms/vm_00000000", "VM_NOT_FOUND"),
        ("post", "/v1/vms/vm_00000000/start", "VM_NOT_FOUND"),
        ("post", "/v1/vms/vm_00000000/stop", "VM_NOT_FOUND"),
        ("post", "/v1/vms/vm_00000000/recordings", "VM_NOT_FOUND"),
        ("get", "/v1/workspaces/ws_00000000", "WORKSPACE_NOT_FOUND"),
        ("post", "/v1/workspaces/ws_00000000/freeze", "WORKSPACE_NOT_FOUND"),
        ("post", "/v1/workspaces/ws_00000000/unfreeze", "WORKSPACE_NOT_FOUND"),
        ("get", "/v1/recordings/rec_00000000", "RECORDING_NOT_FOUND"),
        ("post", "/v1/recordings/rec_00000000/stop", "RECORDING_NOT_FOUND"),
    ]:
        resp = getattr(h.client, method)(path)
        assert resp.status_code == 404, path
        assert error_code(resp) == code, path
    resp = h.client.post("/v1/workspaces/ws_00000000/copy", json={"project_id": "p1", "task_id": "t9"})
    assert resp.status_code == 404 and error_code(resp) == "WORKSPACE_NOT_FOUND"
    assert resp.json()["error"]["details"] == {"workspace_id": "ws_00000000"}


def test_delete_missing_vm_is_success(h):
    resp = h.client.delete("/v1/vms/vm_00000000")
    assert resp.status_code == 200
    assert resp.json() == {"data": {"vm_id": "vm_00000000", "status": "deleted"}}


# ---------- 409 / 500 ----------

def test_vm_failed_on_start(h):
    vm_id = h.create_ok(lifecycle={"queue_timeout_minutes": 1})
    h.vms.db.update_vm(vm_id, {"status": "failed", "error_code": "QUEUE_TIMEOUT"})
    resp = h.client.post(f"/v1/vms/{vm_id}/start")
    assert resp.status_code == 409 and error_code(resp) == "VM_FAILED"
    assert resp.json()["error"]["details"] == {"vm_id": vm_id}


def test_vm_busy_details_contain_status(h):
    vm_id = h.create_ok()
    h.db.update_vm(vm_id, {"status": "creating"})
    resp = h.client.post(f"/v1/vms/{vm_id}/stop")
    assert resp.status_code == 409 and error_code(resp) == "VM_BUSY"
    assert resp.json()["error"]["details"] == {"status": "creating"}


def test_vm_not_running_for_recording(h):
    vm_id = h.create_ok()
    resp = h.client.post(f"/v1/vms/{vm_id}/recordings")
    assert resp.status_code == 409 and error_code(resp) == "VM_NOT_RUNNING"
    assert resp.json()["error"]["details"] == {"status": "queued"}


def test_internal_error_hides_stack(h, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret stack")
    monkeypatch.setattr(h.vms, "host_resources", boom)
    resp = h.client.get("/v1/host/resources")
    assert resp.status_code == 500
    assert resp.json() == {"error": {"code": "INTERNAL_ERROR", "message": "服务内部错误", "details": None}}


def test_workspace_exists_and_not_frozen_codes(h):
    ws = h.workspace("p1", "tA")
    resp = h.client.post("/v1/workspaces", json={"project_id": "p1", "task_id": "tA"})
    assert resp.status_code == 409 and error_code(resp) == "WORKSPACE_EXISTS"
    assert resp.json()["error"]["details"] == {"task_id": "tA", "workspace_id": ws}
    resp = h.client.post(f"/v1/workspaces/{ws}/copy", json={"project_id": "p1", "task_id": "tB"})
    assert resp.status_code == 409 and error_code(resp) == "WORKSPACE_NOT_FROZEN"
    assert resp.json()["error"]["details"] == {"workspace_id": ws}
