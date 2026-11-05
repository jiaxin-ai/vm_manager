"""按项目统计与可选服务令牌：Studio 的配额对账和资源总览依赖这两个接口。"""
from fastapi.testclient import TestClient

from api import create_app


def test_usage_groups_vms_by_project_and_status(h):
    h.create_ok(project_id="p1", task_id="t1")
    h.create_ok(project_id="p1", task_id="t2")
    h.create_ok(project_id="p2", task_id="t1")
    response = h.client.get("/v1/usage")
    assert response.status_code == 200
    projects = {row["project_id"]: row for row in response.json()["data"]["projects"]}
    assert projects["p1"]["queued"] == 2 and projects["p1"]["total"] == 2 and projects["p1"]["active"] == 2
    assert projects["p2"]["total"] == 1


def test_usage_counts_running_and_stopped(h):
    running = h.run_vm(project_id="p1", task_id="t1")
    stopped = h.run_vm(project_id="p1", task_id="t2")
    h.client.post(f"/v1/vms/{stopped}/stop")
    h.rounds(3)
    projects = {row["project_id"]: row for row in h.client.get("/v1/usage").json()["data"]["projects"]}
    assert projects["p1"]["running"] == 1
    assert projects["p1"]["stopped"] == 1
    assert projects["p1"]["active"] == 1
    assert h.status(running) == "running"


def test_container_carries_project_and_task_labels(h):
    vm_id = h.create_ok(project_id="p1", task_id="t1")
    h.rounds(2)
    container = h.backend.containers[h.vm(vm_id)["container_id"]]
    assert container["labels"] == {"harnessstudio.project": "p1", "harnessstudio.task": "t1"}


def test_bearer_token_protects_every_route(h):
    client = TestClient(create_app(h.vms, h.ws, h.rec, auth_token="secret"), raise_server_exceptions=False)
    assert client.get("/v1/usage").status_code == 401
    assert client.get("/v1/usage", headers={"authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/v1/usage", headers={"authorization": "Bearer secret"}).status_code == 200
    assert client.get("/docs", headers={"authorization": "Bearer secret"}).status_code == 200


def test_without_token_file_the_service_stays_unchanged(h):
    assert h.client.get("/v1/usage").status_code == 200
