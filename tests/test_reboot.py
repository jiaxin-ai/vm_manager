"""Windows restarting inside a running VM: the agent service is unreachable for a few minutes while the
container keeps running. The VM stays running, and its recording continues in a new record afterwards."""
from conftest import Harness


def harness(tmp_path):
    h = Harness(tmp_path)
    del h.settings["health_failure_seconds"]  # the service default, 300 s
    return h


def outage(h, vm_id, seconds, step=2):
    h.backend.unhealthy.add(vm_id)
    for _ in range(seconds // step):
        h.rounds(1)
        h.clock.advance(step)
    h.backend.unhealthy.discard(vm_id)


def test_a_restart_inside_the_vm_does_not_fail_it(tmp_path):
    h = harness(tmp_path)
    vm_id = h.run_vm()
    h.backend.unhealthy.add(vm_id)
    h.rounds(1)
    started = h.detail(vm_id)["unreachable_since"]
    for _ in range(60):  # 2 minutes of failed checks, far more than health_failure_threshold
        h.clock.advance(2)
        h.rounds(1)
    assert h.status(vm_id) == "running" and h.vm(vm_id)["health_failures"] == 61
    assert h.detail(vm_id)["unreachable_since"] == started
    h.backend.unhealthy.discard(vm_id)
    h.rounds(1)
    assert h.status(vm_id) == "running" and h.vm(vm_id)["health_failures"] == 0
    assert h.detail(vm_id)["unreachable_since"] is None and h.detail(vm_id)["error"] is None


def test_a_vm_unreachable_for_five_minutes_fails(tmp_path):
    h = harness(tmp_path)
    vm_id = h.run_vm()
    outage(h, vm_id, 298)
    h.backend.unhealthy.add(vm_id)
    h.rounds(1)
    assert h.status(vm_id) == "running"
    h.clock.advance(2)
    h.rounds(1)
    vm = h.vm(vm_id)
    assert vm["status"] == "failed" and vm["error_code"] == "HEALTH_CHECK_FAILED"
    assert vm["error_message"].startswith("运行中连续 300 秒无响应")
    assert vm["container_id"] in h.backend.containers  # kept: the VM can be started again


def test_the_recording_continues_in_a_new_record_after_a_restart(tmp_path):
    h = harness(tmp_path)
    vm_id = h.run_vm()
    first = h.db.active_recording(vm_id)["recording_id"]
    outage(h, vm_id, 90)
    h.rounds(1)
    records = {r["recording_id"]: r for r in h.db.list_recordings(vm_id=vm_id)}
    assert records[first]["status"] == "stopped"
    second = h.db.active_recording(vm_id)
    assert second["recording_id"] != first and second["status"] == "recording"
    assert [body["recording_id"] for body in h.backend.calls("/recording/stop")] == [first]
    assert [body["recording_id"] for body in h.backend.calls("/recording/start")][-1] == second["recording_id"]


def test_a_short_outage_keeps_the_recording(tmp_path):
    h = harness(tmp_path)
    vm_id = h.run_vm()
    first = h.db.active_recording(vm_id)["recording_id"]
    outage(h, vm_id, 10)
    h.rounds(1)
    assert h.db.active_recording(vm_id)["recording_id"] == first
    assert h.backend.calls("/recording/stop") == []


def test_an_old_outage_does_not_split_the_next_run(tmp_path):
    h = harness(tmp_path)
    vm_id = h.run_vm()
    h.backend.unhealthy.add(vm_id)
    h.rounds(1)
    h.client.post(f"/v1/vms/{vm_id}/stop")  # stopped during an outage
    h.backend.unhealthy.discard(vm_id)
    stops = len(h.backend.calls("/recording/stop"))
    h.clock.advance(600)
    h.client.post(f"/v1/vms/{vm_id}/start")
    h.rounds(3)
    assert h.status(vm_id) == "running" and h.detail(vm_id)["unreachable_since"] is None
    assert len(h.backend.calls("/recording/stop")) == stops
    assert len([r for r in h.db.list_recordings(vm_id=vm_id) if r["status"] == "recording"]) == 1
