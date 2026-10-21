"""HTTP 接口：18 个路由、请求/响应模型、统一错误转换（以 api.md 为准）。

路由只做参数校验和结果转换，业务逻辑交给各 manager。Docker SDK 是同步调用，路由用普通 def。
"""
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Generic, List, Literal, Optional, TypeVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import (BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, StringConstraints,
                      ValidationInfo, field_validator)

from common import ApiError
from vm_manager import VM_STATUSES

log = logging.getLogger(__name__)

# ==================== 请求模型 ====================

PathId = Annotated[StrictStr, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
PositiveInt = Annotated[StrictInt, Field(gt=0)]
MAX_TIMEOUT_MINUTES = 7 * 24 * 60
TimeoutMinutes = Annotated[StrictInt, Field(gt=0, le=MAX_TIMEOUT_MINUTES)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpecIn(StrictModel):
    cpu_cores: PositiveInt
    ram_gb: PositiveInt


class ExposeIn(StrictModel):
    vnc: StrictBool = True
    rdp: StrictBool = True


class LifecycleIn(StrictModel):
    timeout_minutes: Optional[TimeoutMinutes] = None  # 不传时取 settings 中的默认值
    queue_timeout_minutes: Optional[PositiveInt] = None


class CreateVmIn(StrictModel):
    project_id: PathId
    task_id: PathId
    chat_id: Optional[StrictStr] = None
    environment_id: StrictStr
    spec: SpecIn
    upstream_task_ids: List[PathId] = Field(default_factory=list)
    expose: ExposeIn = Field(default_factory=ExposeIn)
    lifecycle: LifecycleIn = Field(default_factory=LifecycleIn)

    @field_validator("upstream_task_ids")
    @classmethod
    def _check_upstreams(cls, value, info: ValidationInfo):
        if len(set(value)) != len(value):
            raise ValueError("上游任务不能重复")
        if info.data.get("task_id") in value:
            raise ValueError("上游任务不能包含本任务")
        return value


class StartLifecycleIn(StrictModel):
    timeout_minutes: TimeoutMinutes


class StartVmIn(StrictModel):
    lifecycle: Optional[StartLifecycleIn] = None


class TaskRefIn(StrictModel):
    project_id: PathId
    task_id: PathId


class StartRecordingIn(StrictModel):
    chat_id: Optional[StrictStr] = None


# ==================== 响应模型 ====================

T = TypeVar("T")


class Data(BaseModel, Generic[T]):
    data: T


class Items(BaseModel, Generic[T]):
    items: List[T]


class Software(BaseModel):
    name: str
    version: str


class MinSpec(BaseModel):
    cpu_cores: int
    ram_gb: int


class Environment(BaseModel):
    environment_id: str
    os: str
    software: List[Software]
    min_spec: MinSpec


class Quantity(BaseModel):
    total: int
    allocated: int
    available: int


class DiskQuantity(BaseModel):
    total: int
    used: int
    available: int


class VmCounts(BaseModel):
    running: int
    queued: int


class HostResources(BaseModel):
    cpu: Quantity
    memory_gb: Quantity
    disk_gb: DiskQuantity
    vms: VmCounts


class VmSummary(BaseModel):
    vm_id: str
    project_id: str
    task_id: str
    status: str


class VmCreated(BaseModel):
    vm_id: str
    status: str
    queue_position: int
    workspace_id: str


class Spec(BaseModel):
    cpu_cores: int
    ram_gb: int


class Expose(BaseModel):
    vnc: bool
    rdp: bool


class Lifecycle(BaseModel):
    timeout_minutes: int
    queue_timeout_minutes: int


class Access(BaseModel):
    host: str
    vnc_port: Optional[int]
    rdp_port: Optional[int]
    agent_port: Optional[int]


class WorkspaceMount(BaseModel):
    workspace_id: str
    writable: bool


class UpstreamMount(BaseModel):
    task_id: str
    workspace_id: str
    path: str


class Mounts(BaseModel):
    workspace: WorkspaceMount
    upstreams: List[UpstreamMount]
    project_shared_path: str
    recordings_path: str


class Queue(BaseModel):
    position: int
    reason: str
    expires_at: str


class VmError(BaseModel):
    code: str
    message: str


class VmDetail(BaseModel):
    vm_id: str
    project_id: str
    task_id: str
    chat_id: Optional[str]
    status: str
    environment_id: str
    os: str
    software: List[Software]
    spec: Spec
    expose: Expose
    lifecycle: Lifecycle
    container_id: Optional[str]
    access: Access
    mounts: Mounts
    current_recording_id: Optional[str]
    queue: Optional[Queue]
    created_at: str
    started_at: Optional[str]
    expires_at: Optional[str]
    stopped_at: Optional[str]
    stop_reason: Optional[str]
    error: Optional[VmError]


class VmStarted(BaseModel):
    vm_id: str
    status: str
    queue_position: Optional[int]


class VmStopped(BaseModel):
    vm_id: str
    status: str
    stop_reason: Optional[str]
    stopped_at: Optional[str]


class VmDeleted(BaseModel):
    vm_id: str
    status: str


class WorkspaceSummary(BaseModel):
    workspace_id: str
    project_id: str
    task_id: str
    status: str
    path: str


class WorkspaceList(BaseModel):
    total: int
    items: List[WorkspaceSummary]


class MountedBy(BaseModel):
    vm_id: str
    path: str


class WorkspaceDetail(WorkspaceSummary):
    source_workspace_id: Optional[str]
    mounted_by: List[MountedBy]
    created_at: str
    frozen_at: Optional[str]


class Recording(BaseModel):
    recording_id: str
    vm_id: str
    project_id: str
    task_id: str
    chat_id: Optional[str]
    status: str
    started_at: str
    stopped_at: Optional[str]
    result_path: str
    error: Optional[str]


# ==================== 错误转换 ====================


def _error_response(status, code, message, details=None):
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "details": details}})


def _validation_fields(exc):
    fields = []
    for err in exc.errors():
        loc = [str(part) for part in err["loc"]]
        if loc and loc[0] in ("body", "query", "path"):
            loc = loc[1:]
        field = "body" if err["type"] == "json_invalid" or not loc else ".".join(loc)
        fields.append({"field": field, "reason": err["msg"]})
    return fields


def _validation_error(fields):
    return ApiError(422, "VALIDATION_ERROR", "请求参数不合法", {"fields": fields})


# ==================== 路由 ====================


def create_app(vms, workspaces, recordings, on_shutdown=None):
    @asynccontextmanager
    async def lifespan(app):
        yield
        if on_shutdown is not None:
            on_shutdown()  # uvicorn 收到 SIGTERM 时会在关闭后重新触发信号，必须在 lifespan 中收尾

    app = FastAPI(title="VM Manager", version="1", lifespan=lifespan)

    @app.exception_handler(ApiError)
    def handle_api_error(request: Request, exc: ApiError):
        return _error_response(exc.http_status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    def handle_validation_error(request: Request, exc: RequestValidationError):
        return _error_response(422, "VALIDATION_ERROR", "请求参数不合法", {"fields": _validation_fields(exc)})

    @app.exception_handler(Exception)
    def handle_unexpected(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return _error_response(500, "INTERNAL_ERROR", "服务内部错误")

    # ---------- 1 软件环境与宿主机资源 ----------

    @app.get("/v1/environments", response_model=Data[Items[Environment]])
    def list_environments():
        return {"data": {"items": vms.public_environments()}}

    @app.get("/v1/host/resources", response_model=Data[HostResources])
    def host_resources():
        return {"data": vms.host_resources()}

    # ---------- 2 虚拟机 ----------

    @app.get("/v1/vms", response_model=Data[Items[VmSummary]])
    def list_vms(project_id: Optional[str] = None, task_id: Optional[str] = None, status: Optional[str] = None):
        statuses = None
        if status is not None:
            statuses = status.split(",")
            invalid = [s for s in statuses if s not in VM_STATUSES]
            if invalid:
                raise _validation_error([{"field": "status", "reason": f"未知状态：{', '.join(invalid)}"}])
        return {"data": {"items": vms.list(project_id, task_id, statuses)}}

    @app.post("/v1/vms", response_model=Data[VmCreated])
    def create_vm(body: CreateVmIn):
        return {"data": vms.create(body.model_dump())}

    @app.get("/v1/vms/{vm_id}", response_model=Data[VmDetail])
    def get_vm(vm_id: str):
        return {"data": vms.detail(vm_id)}

    @app.post("/v1/vms/{vm_id}/start", response_model=Data[VmStarted])
    def start_vm(vm_id: str, body: Optional[StartVmIn] = None):
        timeout = body.lifecycle.timeout_minutes if body and body.lifecycle else None
        return {"data": vms.start(vm_id, timeout)}

    @app.post("/v1/vms/{vm_id}/stop", response_model=Data[VmStopped])
    def stop_vm(vm_id: str):
        return {"data": vms.stop(vm_id)}

    @app.delete("/v1/vms/{vm_id}", response_model=Data[VmDeleted])
    def delete_vm(vm_id: str):
        return {"data": vms.delete(vm_id)}

    # ---------- 3 任务空间 ----------

    @app.get("/v1/workspaces", response_model=Data[WorkspaceList])
    def list_workspaces(project_id: Optional[str] = None, task_id: Optional[str] = None,
                        status: Optional[Literal["writable", "frozen"]] = None):
        items = workspaces.list(project_id, task_id, status)
        return {"data": {"total": len(items), "items": items}}

    @app.get("/v1/workspaces/{workspace_id}", response_model=Data[WorkspaceDetail])
    def get_workspace(workspace_id: str):
        return {"data": workspaces.detail(workspaces.get(workspace_id))}

    @app.post("/v1/workspaces", response_model=Data[WorkspaceDetail])
    def create_workspace(body: TaskRefIn):
        return {"data": workspaces.create(body.project_id, body.task_id)}

    @app.post("/v1/workspaces/{workspace_id}/freeze", response_model=Data[WorkspaceDetail])
    def freeze_workspace(workspace_id: str):
        return {"data": workspaces.freeze(workspace_id)}

    @app.post("/v1/workspaces/{workspace_id}/unfreeze", response_model=Data[WorkspaceDetail])
    def unfreeze_workspace(workspace_id: str):
        return {"data": workspaces.unfreeze(workspace_id)}

    @app.post("/v1/workspaces/{workspace_id}/copy", response_model=Data[WorkspaceDetail])
    def copy_workspace(workspace_id: str, body: TaskRefIn):
        return {"data": workspaces.copy(workspace_id, body.project_id, body.task_id)}

    # ---------- 4 轨迹采集 ----------

    @app.post("/v1/vms/{vm_id}/recordings", response_model=Data[Recording])
    def start_recording(vm_id: str, body: Optional[StartRecordingIn] = None):
        return {"data": recordings.start(vm_id, body.chat_id if body else None)}

    @app.get("/v1/recordings", response_model=Data[Items[Recording]])
    def list_recordings(project_id: Optional[str] = None, task_id: Optional[str] = None,
                        vm_id: Optional[str] = None):
        return {"data": {"items": recordings.list(project_id, task_id, vm_id)}}

    @app.get("/v1/recordings/{recording_id}", response_model=Data[Recording])
    def get_recording(recording_id: str):
        return {"data": recordings.public(recordings.get(recording_id))}

    @app.post("/v1/recordings/{recording_id}/stop", response_model=Data[Recording])
    def stop_recording(recording_id: str):
        return {"data": recordings.stop(recording_id)}

    return app
