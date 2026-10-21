"""状态与时间：各状态下启动/停止/删除（11.3）、排队超时、启动超时、到期、健康检查、容器参数、创建失败。"""
import os
from datetime import timedelta

import pytest

from common import from_iso
from conftest import GIB, error_code


def lock(h, vm_id):
    lk = h.locks.get(vm_id)
    assert lk.acquire(blocking=False)
    return lk


# ---------- 启动 ----------

@pytest.mark.parametrize("status", ["queued", "creating", "starting", "running"])
def test_start_returns_current_status(h, status):
    vm_id = h.create_ok()
    h.db.update_vm(vm_id, {"status": status})
    data = h.client.post(f"/v1/vms/{vm_id}/start").json()["data"]
    assert data == {"vm_id": vm_id, "status": status, "queue_position": 1 if status == "queued" else None}


@pytest.mark.parametrize("status", ["stopping", "deleting"])
def test_start_busy(h, status):
    vm_id = h.create_ok()
    h.db.update_vm(vm_id, {"status": status})
    resp = h.client.post(f"/v1/vms/{vm_id}/start")
    assert resp.status_code == 409 and error_code(resp) == "VM_BUSY"


def test_start_stopped_vm(h):
    vm_id = h.run_vm()
    h.client.post(f"/v1/vms/{vm_id}/stop")
    before = h.vm(vm_id)
    h.clock.advance(30)
    data = h.client.post(f"/v1/vms/{vm_id}/start").json()["data"]
    assert data == {"vm_id": vm_id, "status": "queued", "queue_position": 1}
    vm = h.vm(vm_id)
    assert vm["created_at"] == before["created_at"]
    assert vm["started_at"] is None and vm["expires_at"] is None and vm["stopped_at"] is None
    assert vm["stop_reason"] is None and vm["health_failures"] == 0
    assert vm["queue_order"] > before["queue_order"]
    h.rounds(2)
    vm = h.vm(vm_id)
    assert vm["status"] == "running" and vm["container_id"] == before["container_id"]
    assert len(h.backend.containers) == 1  # 沿用原来的容器


def test_start_stopped_checks_docker_and_host(h):
    vm_id = h.run_vm()
    h.client.post(f"/v1/vms/{vm_id}/stop")
    h.backend.docker_up = False
    resp = h.client.post(f"/v1/vms/{vm_id}/start")
    assert resp.status_code == 503 and error_code(resp) == "DOCKER_UNAVAILABLE"
    h.backend.docker_up = True
    h.backend.kvm = False
    resp = h.client.post(f"/v1/vms/{vm_id}/start")
    assert resp.status_code == 503 and error_code(resp) == "HOST_NOT_READY"
    assert resp.json()["error"]["details"] == {"missing": ["kvm"]}
    assert h.status(vm_id) == "stopped"


def test_start_stopped_lock_busy(h):
    vm_id = h.run_vm()
    h.client.post(f"/v1/vms/{vm_id}/stop")
    lk = lock(h, vm_id)
    resp = h.client.post(f"/v1/vms/{vm_id}/start")
    assert resp.status_code == 409 and error_code(resp) == "VM_BUSY"
    lk.release()


def fail_by_health_check(h, vm_id):
    h.backend.unhealthy.add(vm_id)
    h.rounds(h.settings["health_failure_threshold"])
    h.backend.unhealthy.discard(vm_id)
    vm = h.vm(vm_id)
    assert vm["status"] == "failed" and vm["error_code"] == "HEALTH_CHECK_FAILED" and vm["reserved"] == 0
    return vm


def test_start_failed_vm_reuses_its_container(h):
    vm_id = h.run_vm()
    before = fail_by_health_check(h, vm_id)
    h.clock.advance(30)
    data = h.client.post(f"/v1/vms/{vm_id}/start").json()["data"]
    assert data == {"vm_id": vm_id, "status": "queued", "queue_position": 1}
    vm = h.vm(vm_id)
    assert vm["error_code"] is None and vm["error_message"] is None and vm["stop_reason"] is None
    assert vm["started_at"] is None and vm["stopped_at"] is None and vm["health_failures"] == 0
    h.rounds(2)
    vm = h.vm(vm_id)
    assert vm["status"] == "running" and vm["container_id"] == before["container_id"]
    assert len(h.backend.containers) == 1  # 原来的容器，C 盘还在
    assert h.detail(vm_id)["error"] is None


def test_start_failed_vm_that_never_ran_creates_its_container(h):
    vm_id = h.create_ok(lifecycle={"queue_timeout_minutes": 1})
    h.db.update_vm(vm_id, {"status": "failed", "error_code": "QUEUE_TIMEOUT"})
    data = h.client.post(f"/v1/vms/{vm_id}/start").json()["data"]
    assert data["status"] == "queued"
    h.rounds(2)
    assert h.status(vm_id) == "running" and len(h.backend.containers) == 1


def test_start_failed_vm_whose_container_is_gone(h):
    vm_id = h.run_vm()
    fail_by_health_check(h, vm_id)
    h.backend.containers.clear()
    resp = h.client.post(f"/v1/vms/{vm_id}/start")
    assert resp.status_code == 409 and error_code(resp) == "VM_FAILED"
    assert resp.json()["error"]["details"] == {"vm_id": vm_id}
    assert h.status(vm_id) == "failed" and h.vm(vm_id)["error_code"] == "HEALTH_CHECK_FAILED"


def test_start_failed_vm_still_stopping_is_busy(h):
    vm_id = h.run_vm()
    h.db.update_vm(vm_id, {"status": "failed", "error_code": "HEALTH_CHECK_FAILED"})  # reserved 仍为 1
    resp = h.client.post(f"/v1/vms/{vm_id}/start")
    assert resp.status_code == 409 and error_code(resp) == "VM_BUSY"
    assert resp.json()["error"]["details"] == {"status": "failed"}


def test_start_sets_the_run_limit_for_this_and_later_runs(h):
    vm_id = h.run_vm()
    h.client.post(f"/v1/vms/{vm_id}/stop")
    resp = h.client.post(f"/v1/vms/{vm_id}/start", json={"lifecycle": {"timeout_minutes": 240}})
    assert resp.status_code == 200 and resp.json()["data"]["status"] == "queued"
    h.rounds(2)
    vm = h.vm(vm_id)
    assert vm["timeout_minutes"] == 240
    assert from_iso(vm["expires_at"]) - from_iso(vm["started_at"]) == timedelta(minutes=240)
    assert h.detail(vm_id)["lifecycle"]["timeout_minutes"] == 240
    h.client.post(f"/v1/vms/{vm_id}/stop")
    h.client.post(f"/v1/vms/{vm_id}/start")
    h.rounds(2)
    assert h.vm(vm_id)["timeout_minutes"] == 240


def test_start_with_a_run_limit_on_a_running_vm_moves_its_expiry(h):
    vm_id = h.run_vm()
    started = from_iso(h.vm(vm_id)["started_at"])
    data = h.client.post(f"/v1/vms/{vm_id}/start", json={"lifecycle": {"timeout_minutes": 300}}).json()["data"]
    assert data == {"vm_id": vm_id, "status": "running", "queue_position": None}
    assert from_iso(h.vm(vm_id)["expires_at"]) == started + timedelta(minutes=300)
    h.clock.advance(10 * 60)
    resp = h.client.post(f"/v1/vms/{vm_id}/start", json={"lifecycle": {"timeout_minutes": 5}})
    assert resp.status_code == 422 and error_code(resp) == "TIMEOUT_TOO_SHORT"
    assert h.vm(vm_id)["timeout_minutes"] == 300
    h.clock.advance(300 * 60)
    h.rounds(1)
    assert h.status(vm_id) == "stopped" and h.vm(vm_id)["stop_reason"] == "expired"


def test_start_with_a_run_limit_on_a_queued_vm(h):
    vm_id = h.create_ok()
    h.client.post(f"/v1/vms/{vm_id}/start", json={"lifecycle": {"timeout_minutes": 30}})
    h.rounds(2)
    vm = h.vm(vm_id)
    assert from_iso(vm["expires_at"]) - from_iso(vm["started_at"]) == timedelta(minutes=30)


# ---------- 停止 ----------

@pytest.mark.parametrize("status", ["creating", "deleting"])
def test_stop_busy_status(h, status):
    vm_id = h.create_ok()
    h.db.update_vm(vm_id, {"status": status})
    resp = h.client.post(f"/v1/vms/{vm_id}/stop")
    assert resp.status_code == 409 and error_code(resp) == "VM_BUSY"


def test_stop_busy_lock(h):
    vm_id = h.run_vm()
    lk = lock(h, vm_id)
    resp = h.client.post(f"/v1/vms/{vm_id}/stop")
    assert resp.status_code == 409 and resp.json()["error"]["details"] == {"status": "running"}
    lk.release()


def test_stop_queued_cancels_without_docker(h):
    vm_id = h.create_ok()
    h.backend.docker_up = False
    data = h.client.post(f"/v1/vms/{vm_id}/stop").json()["data"]
    assert data == {"vm_id": vm_id, "status": "stopped", "stop_reason": "cancelled",
                    "stopped_at": "2026-10-11T10:00:00Z"}
    # 排队中被取消、从未启动过的虚拟机，再次启动时新建容器
    h.backend.docker_up = True
    h.client.post(f"/v1/vms/{vm_id}/start")
    h.rounds(2)
    assert h.status(vm_id) == "running" and h.vm(vm_id)["container_id"]


@pytest.mark.parametrize("status", ["starting", "running"])
def test_stop_running_or_starting(h, status):
    vm_id = h.run_vm() if status == "running" else h.create_ok()
    if status == "starting":
        h.backend.unhealthy.add(vm_id)
        h.rounds(1)
        assert h.status(vm_id) == "starting"
    h.clock.advance(10)
    data = h.client.post(f"/v1/vms/{vm_id}/stop").json()["data"]
    assert data == {"vm_id": vm_id, "status": "stopped", "stop_reason": "manual",
                    "stopped_at": "2026-10-11T10:00:10Z"}
    vm = h.vm(vm_id)
    assert vm["reserved"] == 0
    assert h.backend.containers[vm["container_id"]]["running"] is False  # 停止不删除


def test_stop_running_docker_unavailable(h):
    vm_id = h.run_vm()
    h.backend.docker_up = False
    resp = h.client.post(f"/v1/vms/{vm_id}/stop")
    assert resp.status_code == 503 and error_code(resp) == "DOCKER_UNAVAILABLE"
    assert h.status(vm_id) == "running"


def test_stop_retries_stopping(h):
    vm_id = h.run_vm()
    h.backend.fail_stop = True
    assert error_code(h.client.post(f"/v1/vms/{vm_id}/stop")) == "STOP_FAILED"
    assert h.status(vm_id) == "stopping"
    h.backend.fail_stop = False
    assert h.client.post(f"/v1/vms/{vm_id}/stop").json()["data"]["status"] == "stopped"


def test_stop_stopped_returns_as_is(h):
    vm_id = h.run_vm()
    first = h.client.post(f"/v1/vms/{vm_id}/stop").json()["data"]
    calls = len(h.backend.stop_calls)
    h.clock.advance(100)
    assert h.client.post(f"/v1/vms/{vm_id}/stop").json()["data"] == first
    assert len(h.backend.stop_calls) == calls


def test_stop_failed_with_reserved_stops_container(h):
    vm_id = h.run_vm()
    h.db.update_vm(vm_id, {"status": "failed", "error_code": "HEALTH_CHECK_FAILED", "stop_reason": "failure"})
    data = h.client.post(f"/v1/vms/{vm_id}/stop").json()["data"]
    assert data["status"] == "failed" and data["stop_reason"] == "failure" and data["stopped_at"]
    assert h.vm(vm_id)["reserved"] == 0


def test_stop_failed_unreserved_returns_as_is(h):
    vm_id = h.create_ok()
    h.db.update_vm(vm_id, {"status": "failed", "error_code": "QUEUE_TIMEOUT"})
    data = h.client.post(f"/v1/vms/{vm_id}/stop").json()["data"]
    assert data == {"vm_id": vm_id, "status": "failed", "stop_reason": None, "stopped_at": None}


# ---------- 删除 ----------

def test_delete_busy(h):
    vm_id = h.create_ok()
    h.db.update_vm(vm_id, {"status": "creating"})
    assert error_code(h.client.delete(f"/v1/vms/{vm_id}")) == "VM_BUSY"
    h.db.update_vm(vm_id, {"status": "running"})
    lk = lock(h, vm_id)
    assert error_code(h.client.delete(f"/v1/vms/{vm_id}")) == "VM_BUSY"
    lk.release()


def test_delete_queued_never_created_without_docker(h):
    vm_id = h.create_ok()
    h.backend.docker_up = False
    assert h.client.delete(f"/v1/vms/{vm_id}").json()["data"] == {"vm_id": vm_id, "status": "deleted"}
    assert h.vm(vm_id) is None
    assert h.client.get(f"/v1/vms/{vm_id}").status_code == 404


@pytest.mark.parametrize("final", ["running", "stopped", "failed", "requeued"])
def test_delete_with_container(h, final):
    vm_id = h.run_vm()
    rec = h.db.active_recording(vm_id)
    if final == "stopped":
        h.client.post(f"/v1/vms/{vm_id}/stop")
    elif final == "failed":
        h.backend.exit_container(vm_id)
        h.rounds(1)
        assert h.status(vm_id) == "failed"
    elif final == "requeued":
        h.client.post(f"/v1/vms/{vm_id}/stop")
        h.create_ok(spec={"cpu_cores": 14, "ram_gb": 8})  # 占住队首
        h.client.post(f"/v1/vms/{vm_id}/start")
    cid = h.vm(vm_id)["container_id"]
    assert h.client.delete(f"/v1/vms/{vm_id}").json()["data"]["status"] == "deleted"
    assert h.vm(vm_id) is None and cid not in h.backend.containers
    # 任务空间、采集目录、采集记录都保留
    assert os.path.isdir(os.path.join(h.data_root, "projects/p1/tasks/t1"))
    assert os.path.isdir(os.path.join(h.data_root, f"projects/p1/recordings/{vm_id}"))
    assert h.db.get_recording(rec["recording_id"]) is not None


def test_delete_with_container_docker_unavailable(h):
    vm_id = h.run_vm()
    h.backend.docker_up = False
    resp = h.client.delete(f"/v1/vms/{vm_id}")
    assert resp.status_code == 503 and error_code(resp) == "DOCKER_UNAVAILABLE"
    assert h.status(vm_id) == "running"


def test_delete_failure_and_retry(h):
    vm_id = h.run_vm()
    cid = h.vm(vm_id)["container_id"]
    h.backend.fail_remove = True
    resp = h.client.delete(f"/v1/vms/{vm_id}")
    assert resp.status_code == 500 and error_code(resp) == "DELETE_FAILED"
    assert resp.json()["error"]["details"] == {"vm_id": vm_id}
    assert h.status(vm_id) == "deleting" and h.vm(vm_id)["reserved"] == 0
    # 容器已经不存在时，重试视为已删除
    h.backend.fail_remove = False
    h.backend.containers.pop(cid)
    assert h.client.delete(f"/v1/vms/{vm_id}").json()["data"]["status"] == "deleted"
    assert h.vm(vm_id) is None


# ---------- 时间 ----------

def test_queue_timeout(h):
    h.run_vm(spec={"cpu_cores": 12, "ram_gb": 8})
    vm_id = h.create_ok(lifecycle={"queue_timeout_minutes": 1})
    h.rounds(1)
    h.clock.advance(60)
    h.rounds(1)
    assert h.status(vm_id) == "queued"
    h.clock.advance(1)
    h.rounds(1)
    vm = h.vm(vm_id)
    assert vm["status"] == "failed" and vm["error_code"] == "QUEUE_TIMEOUT"
    detail = h.detail(vm_id)
    assert detail["error"]["code"] == "QUEUE_TIMEOUT" and detail["queue"] is None
    assert detail["stop_reason"] is None and detail["stopped_at"] is None


def test_startup_timeout_keeps_container(h):
    vm_id = h.create_ok()
    h.backend.unhealthy.add(vm_id)
    h.rounds(1)
    assert h.status(vm_id) == "starting"
    h.clock.advance(900)
    h.rounds(1)
    assert h.status(vm_id) == "starting"
    h.clock.advance(1)
    h.rounds(1)
    vm = h.vm(vm_id)
    assert vm["status"] == "failed" and vm["error_code"] == "STARTUP_TIMEOUT"
    assert vm["stop_reason"] == "failure" and vm["stopped_at"] and vm["reserved"] == 0
    container = h.backend.containers[vm["container_id"]]
    assert container["running"] is False  # 停止但不删除


def test_startup_timeout_with_bad_probe_path(h):
    """probe_path 指向不存在的路径：始终检查失败，到启动截止时间变为 STARTUP_TIMEOUT。"""
    h.vms.environments["env-a"] = dict(h.vms.environments["env-a"], probe_path="/no-such-path")
    vm_id = h.create_ok()
    h.rounds(2)
    h.clock.advance(901)
    h.rounds(1)
    assert h.vm(vm_id)["error_code"] == "STARTUP_TIMEOUT"


def test_expiry_stops_vm(h):
    vm_id = h.run_vm(lifecycle={"timeout_minutes": 1})
    vm = h.vm(vm_id)
    assert vm["expires_at"] == "2026-10-11T10:01:00Z"
    h.clock.advance(59)
    h.rounds(1)
    assert h.status(vm_id) == "running"
    h.clock.advance(1)
    h.rounds(1)
    vm = h.vm(vm_id)
    assert vm["status"] == "stopped" and vm["stop_reason"] == "expired" and vm["reserved"] == 0
    assert h.db.list_recordings(vm_id=vm_id)[0]["status"] == "stopped"


def test_health_check_failures_counted_and_reset(h):
    vm_id = h.run_vm()
    h.backend.unhealthy.add(vm_id)
    h.rounds(2)
    assert h.vm(vm_id)["health_failures"] == 2 and h.status(vm_id) == "running"
    h.backend.unhealthy.discard(vm_id)
    h.rounds(1)
    assert h.vm(vm_id)["health_failures"] == 0
    h.backend.unhealthy.add(vm_id)
    h.rounds(3)
    vm = h.vm(vm_id)
    assert vm["status"] == "failed" and vm["error_code"] == "HEALTH_CHECK_FAILED"
    assert vm["stop_reason"] == "failure" and vm["reserved"] == 0
    assert h.backend.containers[vm["container_id"]]["running"] is False


def test_container_exited(h):
    vm_id = h.run_vm()
    h.backend.exit_container(vm_id)
    h.rounds(1)
    vm = h.vm(vm_id)
    assert vm["status"] == "failed" and vm["error_code"] == "CONTAINER_EXITED" and vm["reserved"] == 0
    assert vm["container_id"] in h.backend.containers


def test_docker_down_does_not_count_failures_or_release(h):
    vm_id = h.run_vm()
    h.backend.docker_up = False
    h.backend.unhealthy.add(vm_id)
    h.rounds(5)
    vm = h.vm(vm_id)
    assert vm["status"] == "running" and vm["health_failures"] == 0 and vm["reserved"] == 1


def test_resume_interrupted_stop_after_retry_interval(h):
    vm_id = h.run_vm()
    h.backend.fail_stop = True
    h.client.post(f"/v1/vms/{vm_id}/stop")
    h.backend.fail_stop = False
    h.clock.advance(59)
    h.rounds(1)
    assert h.status(vm_id) == "stopping"
    h.clock.advance(1)
    h.rounds(1)
    assert h.status(vm_id) == "stopped" and h.vm(vm_id)["reserved"] == 0


def test_resume_interrupted_delete(h):
    vm_id = h.run_vm()
    h.backend.fail_remove = True
    h.client.delete(f"/v1/vms/{vm_id}")
    h.backend.fail_remove = False
    h.clock.advance(60)
    h.rounds(1)
    assert h.vm(vm_id) is None and h.backend.containers == {}


def test_failed_stop_retried_by_inspector(h):
    vm_id = h.run_vm()
    h.backend.unhealthy.add(vm_id)
    h.backend.fail_stop = True
    h.rounds(3)
    vm = h.vm(vm_id)
    assert vm["status"] == "failed" and vm["reserved"] == 1
    h.backend.fail_stop = False
    h.clock.advance(60)
    h.rounds(1)
    assert h.vm(vm_id)["reserved"] == 0


# ---------- 创建容器 ----------

def test_container_arguments(h):
    up = h.workspace("p1", "tA", frozen=True)
    vm_id = h.create_ok(spec={"cpu_cores": 6, "ram_gb": 10}, upstream_task_ids=["tA"],
                        expose={"vnc": False, "rdp": True})
    h.rounds(1)
    c = h.backend.containers[h.vm(vm_id)["container_id"]]
    projects = os.path.join(h.data_root, "projects")
    import vm_manager
    assert c["volumes"] == {
        h.base_storage: {"bind": "/base_storage", "mode": "ro"},
        f"{projects}/p1/tasks/t1": {"bind": "/shared", "mode": "rw"},
        f"{projects}/p1/tasks/tA": {"bind": "/upstream/tA", "mode": "ro"},
        f"{projects}/p1/shared": {"bind": "/project", "mode": "rw"},
        f"{projects}/p1/recordings/{vm_id}": {"bind": "/recordings", "mode": "rw"},
        vm_manager.SCRIPT_PATH: {"bind": "/opt/vm-manager/start_windows.sh", "mode": "ro"},
    }
    assert c["publish_ports"] == [5000, 3389]
    assert c["environment"] == {"RAM_SIZE": "10G", "CPU_CORES": "6", "BASE_IMG": "base_a.img", "BASE_FORMAT": "raw"}
    assert c["cpu_cores"] == 6 and c["mem_bytes"] == 11 * GIB
    detail = h.detail(vm_id)
    assert detail["access"]["vnc_port"] is None and detail["access"]["rdp_port"] is not None
    assert detail["mounts"]["upstreams"] == [{"task_id": "tA", "workspace_id": up, "path": "E:\\tA"}]


def test_vm_detail_fields(h):
    vm_id = h.create_ok(chat_id="chat_01")
    queued = h.detail(vm_id)
    assert queued["queue"] == {"position": 1, "reason": "waiting_turn", "expires_at": "2026-10-11T10:30:00Z"}
    assert queued["access"] == {"host": "10.0.0.10", "vnc_port": None, "rdp_port": None, "agent_port": None}
    assert queued["container_id"] is None and queued["started_at"] is None
    h.rounds(2)
    d = h.detail(vm_id)
    assert d["status"] == "running" and d["queue"] is None and d["error"] is None
    assert d["os"] == "windows11" and d["software"] == [{"name": "AutoCAD", "version": "2026"}]
    assert d["spec"] == {"cpu_cores": 4, "ram_gb": 8}
    assert d["expose"] == {"vnc": True, "rdp": True}
    assert d["lifecycle"] == {"timeout_minutes": 120, "queue_timeout_minutes": 30}
    assert d["started_at"] == "2026-10-11T10:00:00Z" and d["expires_at"] == "2026-10-11T12:00:00Z"
    assert d["mounts"]["workspace"]["writable"] is True
    assert d["mounts"]["project_shared_path"] == os.path.join(h.data_root, "projects/p1/shared")
    assert d["mounts"]["recordings_path"] == os.path.join(h.data_root, f"projects/p1/recordings/{vm_id}")
    assert d["current_recording_id"] == h.db.active_recording(vm_id)["recording_id"]
    assert all(d["access"][k] for k in ("vnc_port", "rdp_port", "agent_port"))


def test_list_vms_filters_and_order(h):
    a = h.create_ok(task_id="tA")
    b = h.create_ok(task_id="tB")
    c = h.create_ok(project_id="p2", task_id="tA")
    h.client.post(f"/v1/vms/{b}/stop")
    items = h.client.get("/v1/vms").json()["data"]["items"]
    assert [i["vm_id"] for i in items] == [c, b, a]
    assert set(items[0]) == {"vm_id", "project_id", "task_id", "status"}
    q = lambda **p: [i["vm_id"] for i in h.client.get("/v1/vms", params=p).json()["data"]["items"]]  # noqa: E731
    assert q(task_id="tA") == [c, a]
    assert q(project_id="p1", task_id="tA") == [a]
    assert q(status="stopped") == [b]
    assert q(status="queued,stopped", project_id="p1") == [b, a]


def test_provision_failure_first_time_removes_container(h):
    h.backend.fail_start = True
    vm_id = h.create_ok()
    h.rounds(1)
    vm = h.vm(vm_id)
    assert vm["status"] == "failed" and vm["error_code"] == "PROVISION_FAILED"
    assert vm["reserved"] == 0 and vm["container_id"] is None and h.backend.containers == {}
    assert vm["stop_reason"] is None and vm["stopped_at"] is None


def test_provision_create_failure(h):
    h.backend.fail_create = True
    vm_id = h.create_ok()
    h.rounds(1)
    vm = h.vm(vm_id)
    assert vm["status"] == "failed" and vm["error_code"] == "PROVISION_FAILED" and vm["reserved"] == 0


def test_provision_failure_on_restart_keeps_container(h):
    vm_id = h.run_vm()
    cid = h.vm(vm_id)["container_id"]
    h.client.post(f"/v1/vms/{vm_id}/stop")
    h.client.post(f"/v1/vms/{vm_id}/start")
    h.backend.fail_start = True
    h.rounds(1)
    vm = h.vm(vm_id)
    assert vm["status"] == "failed" and vm["error_code"] == "PROVISION_FAILED"
    assert vm["container_id"] == cid and cid in h.backend.containers
    assert vm["reserved"] == 0 and vm["stop_reason"] == "failure"


def test_each_round_starts_at_most_one(h):
    ids = [h.create_ok(spec={"cpu_cores": 2, "ram_gb": 2}) for _ in range(3)]
    h.rounds(1)
    assert [h.status(i) for i in ids] == ["starting", "queued", "queued"]
