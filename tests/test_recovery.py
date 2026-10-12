"""服务重启恢复（第 13 节）的每一条规则。"""
import json
import os

from conftest import Harness


def set_creating(h, vm_id, ever_started):
    h.db.update_vm(vm_id, {"status": "creating", "reserved": 1, "started_at": "2026-10-11T10:00:00Z",
                           "expires_at": "2026-10-11T12:00:00Z", "ever_started": ever_started})


def test_creating_with_running_container_becomes_starting(h):
    vm_id = h.create_ok()
    set_creating(h, vm_id, 0)
    cid = h.backend.add_container(vm_id, running=True)  # 容器已启动，但 container_id/端口还没写入数据库
    h.clock.advance(30)
    h.restart()
    vm = h.vm(vm_id)
    assert vm["status"] == "starting" and vm["started_at"] == "2026-10-11T10:00:00Z"
    assert vm["container_id"] == cid and json.loads(vm["ports"]) == h.backend.containers[cid]["ports"]
    assert vm["reserved"] == 1 and vm["ever_started"] == 1
    h.rounds(1)
    assert h.status(vm_id) == "running" and len(h.backend.containers) == 1


def test_creating_first_time_interrupted_fails_and_removes_container(h):
    vm_id = h.create_ok()
    set_creating(h, vm_id, 0)
    h.backend.add_container(vm_id, running=False)
    h.restart()
    vm = h.vm(vm_id)
    assert vm["status"] == "failed" and vm["error_code"] == "PROVISION_FAILED" and vm["reserved"] == 0
    assert h.backend.containers == {}


def test_creating_restart_interrupted_requeued_keeping_c_drive(h):
    vm_id = h.run_vm()
    cid = h.vm(vm_id)["container_id"]
    h.client.post(f"/v1/vms/{vm_id}/stop")
    h.client.post(f"/v1/vms/{vm_id}/start")
    order = h.vm(vm_id)["queue_order"]
    set_creating(h, vm_id, 1)
    h.clock.advance(120)
    h.restart()
    vm = h.vm(vm_id)
    assert vm["status"] == "queued" and vm["reserved"] == 0 and vm["queue_order"] == order
    assert vm["queue_deadline"] == "2026-10-11T10:32:00Z" and vm["started_at"] is None
    assert vm["container_id"] == cid and cid in h.backend.containers  # C 盘随容器保留
    h.rounds(2)
    assert h.status(vm_id) == "running" and list(h.backend.containers) == [cid]


def test_running_vm_unchanged_when_container_running(h):
    vm_id = h.run_vm()
    before = h.vm(vm_id)
    h.clock.advance(600)
    h.restart()
    after = h.vm(vm_id)
    assert after == before


def test_running_vm_failed_when_container_exited(h):
    vm_id = h.run_vm()
    h.backend.exit_container(vm_id)
    h.restart()
    vm = h.vm(vm_id)
    assert vm["status"] == "failed" and vm["error_code"] == "CONTAINER_EXITED" and vm["reserved"] == 0
    rec = h.db.list_recordings(vm_id=vm_id)[0]
    assert rec["status"] == "failed" and rec["error"] == "服务重启时虚拟机不在运行"


def test_starting_vm_failed_when_container_missing(h):
    vm_id = h.create_ok()
    h.backend.unhealthy.add(vm_id)
    h.rounds(1)
    h.backend.containers.clear()
    h.restart()
    assert h.vm(vm_id)["error_code"] == "CONTAINER_EXITED"


def test_queued_and_stopped_unchanged(h):
    stopped = h.run_vm()
    h.client.post(f"/v1/vms/{stopped}/stop")
    h.create_ok(spec={"cpu_cores": 14, "ram_gb": 8})
    queued = h.create_ok(lifecycle={"queue_timeout_minutes": 1})
    before = {v: h.vm(v) for v in (stopped, queued)}
    h.clock.advance(3600)
    h.restart()
    assert {v: h.vm(v) for v in (stopped, queued)} == before  # 已超时的由第一轮巡检处理
    h.rounds(1)
    assert h.vm(queued)["error_code"] == "QUEUE_TIMEOUT"


def test_stopping_and_deleting_continued_by_first_round(h):
    a = h.run_vm(task_id="tA")
    b = h.run_vm(task_id="tB")
    h.db.update_vm(a, {"status": "stopping", "stop_reason": "manual"})
    h.db.update_vm(b, {"status": "deleting"})
    h.restart()
    assert h.status(a) == "stopping" and h.status(b) == "deleting"
    h.rounds(1)
    assert h.status(a) == "stopped" and h.vm(a)["reserved"] == 0
    assert h.vm(b) is None


def test_docker_unavailable_at_restart_defers_recovery(h):
    vm_id = h.run_vm()
    h.backend.exit_container(vm_id)
    h.backend.docker_up = False
    h.restart()
    h.rounds(2)
    assert h.status(vm_id) == "running"  # 状态未知，不当作已停止
    h.backend.docker_up = True
    h.rounds(1)
    assert h.vm(vm_id)["error_code"] == "CONTAINER_EXITED"


def test_recordings_on_restart(h):
    confirmed_vm = h.run_vm(task_id="tA")
    confirmed = h.db.active_recording(confirmed_vm)["recording_id"]
    h.backend.recorder_fail.add("/recording/start")
    pending_vm = h.create_ok(task_id="tB")
    h.rounds(1)
    h.backend.recorder_fail.clear()
    # 只让 starting → running 发生，不经过第 6 步的重试：直接改库模拟“开始接口超时后服务退出”
    h.db.update_vm(pending_vm, {"status": "running"})
    h.db.insert_recording({"recording_id": "rec_pending1", "vm_id": pending_vm, "project_id": "p1", "task_id": "tB",
                           "chat_id": None, "status": "recording", "confirmed": 0, "start_attempts": 1,
                           "started_at": "2026-10-11T10:00:00Z", "stopped_at": None,
                           "result_path": "/x", "error": None})
    starts_before = len(h.backend.calls("start"))
    h.restart()
    assert h.db.get_recording(confirmed)["status"] == "recording"  # 已确认的保持不变
    assert len(h.backend.calls("start")) == starts_before  # 恢复时不重新开始采集
    h.rounds(1)
    new_starts = h.backend.calls("start")[starts_before:]
    assert [b["recording_id"] for b in new_starts] == ["rec_pending1"]  # 未确认的用同一个 ID 重试
    assert h.db.get_recording("rec_pending1")["confirmed"] == 1
    assert len(h.db.list_recordings(vm_id=confirmed_vm)) == 1


def test_workspace_pending_op_completed_on_restart(h):
    ws = h.workspace("p1", "tA")
    h.chattr_fail = True
    h.client.post(f"/v1/workspaces/{ws}/freeze")
    h.chattr_fail = False
    h.restart()
    row = h.db.get_workspace(ws)
    assert row["status"] == "frozen" and row["pending_op"] is None


def test_copy_residue_cleanup(h):
    h.workspace("p1", "tA")
    tasks = os.path.join(h.data_root, "projects/p1/tasks")
    os.makedirs(os.path.join(tasks, ".tC.copying-abcd1234", "sub"))
    os.makedirs(os.path.join(tasks, "tOrphan"))
    with open(os.path.join(tasks, "tOrphan", "f.txt"), "w") as f:
        f.write("keep me")
    h.restart()
    names = sorted(os.listdir(tasks))
    assert names == [".tOrphan.orphan-20261011T100000Z", "tA"]
    with open(os.path.join(tasks, names[0], "f.txt")) as f:
        assert f.read() == "keep me"
    # 改名后可以重新复制/创建 tOrphan
    assert h.client.post("/v1/workspaces", json={"project_id": "p1", "task_id": "tOrphan"}).status_code == 200


def test_unknown_managed_container_left_untouched(h):
    cid = h.backend.add_container("vm_unknown1", running=True)
    h.restart()
    h.rounds(2)
    assert h.backend.containers[cid]["running"] is True


def test_full_restart_with_running_and_queued(tmp_path):
    h = Harness(tmp_path)
    running = h.run_vm(spec={"cpu_cores": 10, "ram_gb": 8})
    queued = h.create_ok(spec={"cpu_cores": 10, "ram_gb": 8})
    h.rounds(1)
    before = {v: h.vm(v) for v in (running, queued)}
    containers = dict(h.backend.containers)
    starts = len(h.backend.calls("start"))
    h.restart()
    h.rounds(3)
    after = {v: h.vm(v) for v in (running, queued)}
    for key in ("vm_id", "container_id", "ports", "expires_at", "started_at", "queue_order", "status"):
        assert after[running][key] == before[running][key]
        assert after[queued][key] == before[queued][key]
    assert h.backend.containers.keys() == containers.keys()
    assert len(h.backend.calls("start")) == starts
    assert len(h.db.list_recordings(vm_id=running)) == 1
