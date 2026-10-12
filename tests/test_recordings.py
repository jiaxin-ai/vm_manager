"""采集：自动开始只一次、重复开始返回同一条、失败重试与累计 3 次、与停止/删除并发、失败不阻塞停止、容器退出。"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from conftest import Harness, error_code

START, STOP = "/recording/start", "/recording/stop"


def test_auto_start_only_once(h):
    vm_id = h.run_vm(chat_id="chat_01")
    h.rounds(5)
    recs = h.db.list_recordings(vm_id=vm_id)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["status"] == "recording" and rec["confirmed"] == 1 and rec["chat_id"] == "chat_01"
    assert len(h.backend.calls("start")) == 1
    body = h.backend.calls("start")[0]
    assert body == {"recording_id": rec["recording_id"], "output_dir": f"G:\\{rec['recording_id']}",
                    "project_id": "p1", "task_id": "t1", "vm_id": vm_id, "chat_id": "chat_01"}
    assert rec["result_path"] == os.path.join(h.data_root, f"projects/p1/recordings/{vm_id}/{rec['recording_id']}")
    assert os.path.isdir(rec["result_path"])


def test_repeated_start_returns_same_record(h):
    vm_id = h.run_vm()
    auto = h.db.active_recording(vm_id)
    resp = h.client.post(f"/v1/vms/{vm_id}/recordings", json={"chat_id": "other"})
    data = resp.json()["data"]
    assert data["recording_id"] == auto["recording_id"] and data["chat_id"] is None  # 不修改 chat_id
    assert set(data) == {"recording_id", "vm_id", "project_id", "task_id", "chat_id", "status",
                         "started_at", "stopped_at", "result_path", "error"}
    assert len(h.backend.calls("start")) == 1


def test_manual_stop_then_manual_start_creates_new(h):
    vm_id = h.run_vm(chat_id="c1")
    first = h.db.active_recording(vm_id)["recording_id"]
    stopped = h.client.post(f"/v1/recordings/{first}/stop").json()["data"]
    assert stopped["status"] == "stopped" and stopped["stopped_at"]
    assert h.client.post(f"/v1/recordings/{first}/stop").json()["data"] == stopped  # 重复停止原样返回
    h.rounds(3)
    assert h.db.active_recording(vm_id) is None  # 本次运行不再自动开始
    new = h.client.post(f"/v1/vms/{vm_id}/recordings", json={"chat_id": "c2"}).json()["data"]
    assert new["recording_id"] != first and new["chat_id"] == "c2" and new["status"] == "recording"
    assert h.detail(vm_id)["current_recording_id"] == new["recording_id"]


def test_start_failure_retried_with_same_id_then_failed(h):
    h.backend.recorder_fail.add(START)
    vm_id = h.run_vm()
    # 第 2 轮：自动开始失败 1 次；同一轮的第 6 步又用同一个 ID 重试 1 次
    rec = h.db.list_recordings(vm_id=vm_id)[0]
    assert rec["status"] == "recording" and rec["confirmed"] == 0 and rec["start_attempts"] == 2
    assert h.status(vm_id) == "running"
    # 未确认的记录对外仍是 recording
    assert h.client.get(f"/v1/recordings/{rec['recording_id']}").json()["data"]["status"] == "recording"
    h.rounds(1)
    final = h.db.get_recording(rec["recording_id"])
    assert final["status"] == "failed" and final["start_attempts"] == 3 and final["error"]
    ids = {b["recording_id"] for b in h.backend.calls("start")}
    assert ids == {rec["recording_id"]} and len(h.backend.calls("start")) == 3
    assert h.backend.calls("stop") == [{"recording_id": rec["recording_id"]}]  # 改为 failed 前先停止一次
    assert len(h.db.list_recordings(vm_id=vm_id)) == 1
    h.rounds(3)
    assert h.status(vm_id) == "running" and len(h.db.list_recordings(vm_id=vm_id)) == 1


def test_manual_start_failure(h):
    vm_id = h.run_vm()
    first = h.db.active_recording(vm_id)["recording_id"]
    h.client.post(f"/v1/recordings/{first}/stop")
    h.backend.recorder_fail.add(START)
    resp = h.client.post(f"/v1/vms/{vm_id}/recordings")
    assert resp.status_code == 500 and error_code(resp) == "RECORDING_START_FAILED"
    assert resp.json()["error"]["details"] == {"vm_id": vm_id}
    pending = h.db.active_recording(vm_id)
    assert pending["confirmed"] == 0
    # 再次手动开始：用同一个 recording_id 重试，不生成新的记录
    h.backend.recorder_fail.clear()
    data = h.client.post(f"/v1/vms/{vm_id}/recordings").json()["data"]
    assert data["recording_id"] == pending["recording_id"]
    assert h.db.get_recording(pending["recording_id"])["confirmed"] == 1


def test_manual_stop_failure_keeps_recording(h):
    vm_id = h.run_vm()
    rec_id = h.db.active_recording(vm_id)["recording_id"]
    h.backend.recorder_fail.add(STOP)
    resp = h.client.post(f"/v1/recordings/{rec_id}/stop")
    assert resp.status_code == 500 and error_code(resp) == "RECORDING_STOP_FAILED"
    assert resp.json()["error"]["details"] == {"recording_id": rec_id}
    assert h.db.get_recording(rec_id)["status"] == "recording"
    # 记录仍是 recording：再次开始返回这条，不会开出第二份
    assert h.client.post(f"/v1/vms/{vm_id}/recordings").json()["data"]["recording_id"] == rec_id
    h.backend.recorder_fail.clear()
    assert h.client.post(f"/v1/recordings/{rec_id}/stop").json()["data"]["status"] == "stopped"


def test_recording_failure_does_not_block_stop(h):
    vm_id = h.run_vm()
    rec_id = h.db.active_recording(vm_id)["recording_id"]
    h.backend.recorder_fail.add(STOP)
    assert h.client.post(f"/v1/vms/{vm_id}/stop").json()["data"]["status"] == "stopped"
    rec = h.db.get_recording(rec_id)
    assert rec["status"] == "failed" and rec["error"]


def test_recording_failure_does_not_block_delete(h):
    vm_id = h.run_vm()
    rec_id = h.db.active_recording(vm_id)["recording_id"]
    h.backend.recorder_fail.add(STOP)
    assert h.client.delete(f"/v1/vms/{vm_id}").json()["data"]["status"] == "deleted"
    assert h.db.get_recording(rec_id)["status"] == "failed"


def test_container_exit_fails_recording(h):
    vm_id = h.run_vm()
    rec_id = h.db.active_recording(vm_id)["recording_id"]
    h.backend.exit_container(vm_id)
    h.rounds(1)
    rec = h.db.get_recording(rec_id)
    assert rec["status"] == "failed" and rec["error"] == "虚拟机已退出，采集未正常结束"
    assert h.backend.calls("stop") == []  # 不调用停止接口


def test_manual_stop_when_vm_gone(h):
    vm_id = h.run_vm()
    rec_id = h.db.active_recording(vm_id)["recording_id"]
    h.db.update_vm(vm_id, {"status": "failed", "error_code": "CONTAINER_EXITED"})
    data = h.client.post(f"/v1/recordings/{rec_id}/stop").json()["data"]
    assert data["status"] == "failed" and data["error"] == "虚拟机已退出，采集未正常结束"


def test_start_recording_while_vm_busy_is_not_running(h):
    vm_id = h.run_vm()
    lk = h.locks.get(vm_id)
    lk.acquire()
    resp = h.client.post(f"/v1/vms/{vm_id}/recordings")
    assert resp.status_code == 409 and error_code(resp) == "VM_NOT_RUNNING"
    lk.release()


def test_start_recording_racing_with_stop(tmp_path):
    """开始采集持有实例锁时，停止返回 VM_BUSY；停止完成后不会再开始采集。"""
    h = Harness(tmp_path)
    vm_id = h.run_vm()
    first = h.db.active_recording(vm_id)["recording_id"]
    h.client.post(f"/v1/recordings/{first}/stop")
    h.backend.recorder_gate = threading.Event()
    h.backend.recorder_entered.clear()
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("rec", h.client.post(f"/v1/vms/{vm_id}/recordings")))
    t.start()
    assert h.backend.recorder_entered.wait(5)
    resp = h.client.post(f"/v1/vms/{vm_id}/stop")
    assert resp.status_code == 409 and error_code(resp) == "VM_BUSY"
    h.backend.recorder_gate.set()
    t.join()
    rec_id = out["rec"].json()["data"]["recording_id"]
    h.backend.recorder_gate = None
    assert h.client.post(f"/v1/vms/{vm_id}/stop").json()["data"]["status"] == "stopped"
    assert h.db.get_recording(rec_id)["status"] == "stopped"  # 停止流程结束了这条采集
    resp = h.client.post(f"/v1/vms/{vm_id}/recordings")
    assert resp.status_code == 409 and error_code(resp) == "VM_NOT_RUNNING"
    assert h.db.active_recording(vm_id) is None


def test_auto_start_after_vm_left_running_does_nothing(h):
    vm_id = h.run_vm()
    first = h.db.active_recording(vm_id)["recording_id"]
    h.client.post(f"/v1/recordings/{first}/stop")
    h.client.post(f"/v1/vms/{vm_id}/stop")
    calls = len(h.backend.calls("start"))
    assert h.rec.start_locked(vm_id, None, manual=False) is None
    assert len(h.backend.calls("start")) == calls and h.db.active_recording(vm_id) is None


def test_auto_start_runs_in_background_after_running(tmp_path):
    """使用真实线程池：starting → running 后在后台开始采集，并与紧随其后的删除串行。"""
    pool = ThreadPoolExecutor(max_workers=4)
    h = Harness(tmp_path, executor=pool)
    vm_id = h.create_ok()
    h.rounds(2)
    assert h.status(vm_id) == "running"
    resp = h.client.delete(f"/v1/vms/{vm_id}")
    while resp.status_code == 409:  # 后台采集正持有实例锁
        resp = h.client.delete(f"/v1/vms/{vm_id}")
    assert resp.json()["data"]["status"] == "deleted"
    pool.shutdown(wait=True)
    recs = h.db.list_recordings(vm_id=vm_id)
    assert len(recs) == 1 and recs[0]["status"] == "stopped"


def test_list_recordings(h):
    a = h.run_vm(task_id="tA")
    h.clock.advance(1)
    b = h.run_vm(task_id="tB")
    items = h.client.get("/v1/recordings").json()["data"]["items"]
    assert [i["vm_id"] for i in items] == [b, a]
    assert [i["vm_id"] for i in h.client.get("/v1/recordings", params={"task_id": "tA"}).json()["data"]["items"]] == [a]
    assert h.client.get("/v1/recordings", params={"vm_id": b, "project_id": "p2"}).json()["data"]["items"] == []
