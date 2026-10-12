"""排队与资源：FIFO、并发不超额预留、取消与领取互斥、重启排到队尾、确认停止后才释放资源、准入检查。"""
import threading

from conftest import GIB, Harness, error_code


def reserved_cpu(h):
    return sum(vm["cpu_cores"] for vm in h.db.reserved_vms())


def test_host_resources(h):
    h.backend.disk_total, h.backend.disk_free["default"] = 2000 * GIB + 5, 1500 * GIB + 7
    vm_id = h.run_vm(spec={"cpu_cores": 4, "ram_gb": 8})
    h.backend.mem_available = int(20.5 * GIB)
    h.create_ok(spec={"cpu_cores": 14, "ram_gb": 8})  # 放不下，排队
    h.rounds()
    data = h.client.get("/v1/host/resources").json()["data"]
    assert data == {
        "cpu": {"total": 16, "allocated": 4, "available": 10},
        # min(64-4-9, 20.5-4) = 16.5 → 16
        "memory_gb": {"total": 64, "allocated": 9, "available": 16},
        "disk_gb": {"total": 2000, "used": 500, "available": 1500},
        "vms": {"running": 1, "queued": 1},
    }
    assert vm_id


def test_strict_fifo_head_blocks_followers(h):
    a = h.run_vm(spec={"cpu_cores": 10, "ram_gb": 8})
    b = h.create_ok(spec={"cpu_cores": 8, "ram_gb": 8})  # 放不下
    c = h.create_ok(spec={"cpu_cores": 2, "ram_gb": 2})  # 放得下，但不能插队
    h.rounds(3)
    assert h.status(b) == "queued" and h.status(c) == "queued"
    assert h.detail(b)["queue"]["reason"] == "insufficient_resources"
    assert h.detail(b)["queue"]["position"] == 1
    assert h.detail(c)["queue"] == {"position": 2, "reason": "waiting_turn",
                                    "expires_at": h.vm(c)["queue_deadline"]}
    h.client.post(f"/v1/vms/{a}/stop")
    h.rounds(1)
    assert h.status(b) in ("starting", "running") and h.status(c) == "queued"  # 每轮最多启动一台
    h.rounds(1)
    assert h.status(c) in ("starting", "running")


def test_queue_position_returned_on_create(h):
    h.backend.docker_up = True
    first = h.create().json()["data"]
    second = h.create().json()["data"]
    assert (first["queue_position"], second["queue_position"]) == (1, 2)
    assert first["status"] == "queued" and first["workspace_id"] == second["workspace_id"]


def test_restarted_vm_goes_to_queue_tail(h):
    a = h.run_vm(spec={"cpu_cores": 10, "ram_gb": 8})
    h.client.post(f"/v1/vms/{a}/stop")
    blocker = h.run_vm(spec={"cpu_cores": 10, "ram_gb": 8})
    b = h.create_ok(spec={"cpu_cores": 10, "ram_gb": 8})
    resp = h.client.post(f"/v1/vms/{a}/start").json()["data"]
    assert resp == {"vm_id": a, "status": "queued", "queue_position": 2}
    assert h.vm(a)["queue_order"] > h.vm(b)["queue_order"]
    h.client.post(f"/v1/vms/{blocker}/stop")
    h.rounds(2)
    assert h.status(b) == "running" and h.status(a) == "queued"


def test_resources_released_only_after_confirmed_stop(h):
    vm_id = h.run_vm()
    h.backend.fail_stop = True
    resp = h.client.post(f"/v1/vms/{vm_id}/stop")
    assert resp.status_code == 500 and error_code(resp) == "STOP_FAILED"
    assert resp.json()["error"]["details"] == {"vm_id": vm_id}
    vm = h.vm(vm_id)
    assert vm["status"] == "stopping" and vm["reserved"] == 1
    assert h.client.get("/v1/host/resources").json()["data"]["cpu"]["allocated"] == 4
    h.backend.fail_stop = False
    resp = h.client.post(f"/v1/vms/{vm_id}/stop").json()["data"]
    assert resp["status"] == "stopped" and resp["stop_reason"] == "manual"
    assert h.vm(vm_id)["reserved"] == 0


def test_concurrent_creates_get_distinct_queue_positions(tmp_path):
    h = Harness(tmp_path)
    results, errors = [], []

    def create():
        try:
            resp = h.create(spec={"cpu_cores": 4, "ram_gb": 4})
            results.append(resp.json()["data"]["queue_position"])
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=create) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert sorted(results) == list(range(1, 11))
    orders = [vm["queue_order"] for vm in h.db.list_vms()]
    assert len(set(orders)) == 10


def test_concurrent_scheduling_does_not_over_reserve(tmp_path):
    h = Harness(tmp_path)
    for _ in range(8):
        h.create_ok(spec={"cpu_cores": 4, "ram_gb": 4})  # 预算 14 核，最多 3 台
    barrier = threading.Barrier(8)

    def schedule():
        barrier.wait()
        for _ in range(5):
            h.vms._schedule()

    threads = [threading.Thread(target=schedule) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert reserved_cpu(h) == 12
    assert len(h.db.reserved_vms()) == 3


def test_cancel_and_claim_only_one_wins(tmp_path):
    for i in range(20):
        h = Harness(tmp_path / str(i))
        vm_id = h.create_ok()
        barrier = threading.Barrier(2)
        out = {}

        def cancel():
            barrier.wait()
            out["stop"] = h.client.post(f"/v1/vms/{vm_id}/stop")

        def claim():
            barrier.wait()
            h.vms._schedule()

        threads = [threading.Thread(target=cancel), threading.Thread(target=claim)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        vm = h.vm(vm_id)
        if vm["stop_reason"] == "cancelled":  # 取消成功：没有领取
            assert vm["status"] == "stopped" and vm["reserved"] == 0
            assert vm["container_id"] is None and h.backend.containers == {}
        elif vm["status"] == "starting":  # 领取成功：取消没有生效
            assert vm["reserved"] == 1
            assert out["stop"].status_code == 409 and error_code(out["stop"]) == "VM_BUSY"
        else:  # 领取完成后，停止请求才拿到锁：按 starting 正常停止
            assert vm["status"] == "stopped" and vm["stop_reason"] == "manual" and vm["reserved"] == 0


def test_cas_on_queued_only_once(h):
    vm_id = h.create_ok()
    assert h.db.update_vm(vm_id, {"status": "stopped"}, statuses=["queued"])
    assert not h.db.update_vm(vm_id, {"status": "creating"}, statuses=["queued"])


def test_docker_unavailable_keeps_queue(h):
    """修正后的顺序：先判断 Docker，再查 KVM/镜像/基础磁盘；Docker 断开不会误判为 HOST_NOT_READY。"""
    vm_id = h.create_ok()
    h.backend.docker_up = False
    h.rounds(2)
    assert h.status(vm_id) == "queued"
    assert h.detail(vm_id)["queue"]["reason"] == "docker_unavailable"
    h.backend.docker_up = True
    h.rounds(2)
    assert h.status(vm_id) == "running"


def test_host_not_ready_fails_head_without_blocking_queue(h):
    a = h.create_ok(environment_id="env-b", spec={"cpu_cores": 4, "ram_gb": 8})
    b = h.create_ok()
    import os
    os.remove(os.path.join(h.base_storage, "base_b.img"))
    h.rounds(1)
    vm = h.vm(a)
    assert vm["status"] == "failed" and vm["error_code"] == "HOST_NOT_READY"
    assert vm["stop_reason"] is None and vm["stopped_at"] is None
    assert h.status(b) == "starting"


def test_disk_space_admission(h):
    vm_id = h.create_ok()
    h.backend.disk_free["/docker-root"] = 49 * GIB
    h.rounds(1)
    assert h.detail(vm_id)["queue"]["reason"] == "insufficient_resources"
    h.backend.disk_free["/docker-root"] = 50 * GIB
    h.backend.disk_free[h.data_root] = 10 * GIB
    h.rounds(1)
    assert h.status(vm_id) == "queued"
    h.backend.disk_free[h.data_root] = 100 * GIB
    h.rounds(1)
    assert h.status(vm_id) == "starting"


def test_memavailable_limits_admission(h):
    vm_id = h.create_ok(spec={"cpu_cores": 4, "ram_gb": 8})
    h.backend.mem_available = 12 * GIB  # 12-4=8 < 9
    h.rounds(1)
    assert h.detail(vm_id)["queue"]["reason"] == "insufficient_resources"
    h.backend.mem_available = 13 * GIB
    h.rounds(1)
    assert h.status(vm_id) == "starting"
