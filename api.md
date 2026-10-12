# VM Manager API v1

本文档定义专业软件虚拟化子系统的接口规范。

服务地址：`<ip>:<port>`；API 前缀：`/v1`；请求和响应均为 JSON；时间均为 ISO 8601 格式的 UTC 时间。

成功时 HTTP 状态码为 `200`，响应体为 `{"data": ...}`；失败时 HTTP 状态码为 4xx / 5xx，响应体为：

`{"error": {"code": ..., "message": ..., "details": ...}}`

## 接口总览

V1 共 18 个基础接口，分为 4 组。

### 1 查询可用软件环境与宿主机资源

| 序号 | 接口名称 | 方法 | 路径 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 查询可用软件环境 | GET | `/v1/environments` | 查询可用的软件环境，包括操作系统、软件组合与版本、最低资源配置 |
| 2 | 查询宿主机资源 | GET | `/v1/host/resources` | 查询宿主机 CPU、内存、磁盘的总量、已占用量和可分配量 |

### 2 虚拟机管理

| 序号 | 接口名称 | 方法 | 路径 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 查询虚拟机列表 | GET | `/v1/vms` | 查询虚拟机列表，支持按项目 id、任务 id、状态筛选 |
| 2 | 创建虚拟机 | POST | `/v1/vms` | 按选定的软件环境、资源规格和任务创建并自动启动虚拟机，自动挂载该任务的任务空间 |
| 3 | 查询虚拟机详情 | GET | `/v1/vms/{vm_id}` | 查询虚拟机的配置、连接信息、状态与失败原因、时间、关联的任务空间与采集记录 |
| 4 | 启动虚拟机 | POST | `/v1/vms/{vm_id}/start` | 重新启动已停止的虚拟机，不创建新实例 |
| 5 | 停止虚拟机 | POST | `/v1/vms/{vm_id}/stop` | 停止虚拟机，释放 CPU 与内存，保留系统盘和任务空间关联 |
| 6 | 删除虚拟机 | DELETE | `/v1/vms/{vm_id}` | 删除虚拟机，任务空间和采集记录保留 |

### 3 任务空间管理


| 序号 | 接口名称 | 方法 | 路径 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 查询任务空间列表 | GET | `/v1/workspaces` | 查询任务空间列表，支持按项目 id、任务 id、状态筛选 |
| 2 | 查询任务空间详情 | GET | `/v1/workspaces/{workspace_id}` | 查询任务空间的状态（可写/已冻结）、关联信息、挂载情况 |
| 3 | 创建任务空间 | POST | `/v1/workspaces` | 为指定任务创建任务空间，关联项目与任务 |
| 4 | 冻结任务空间 | POST | `/v1/workspaces/{workspace_id}/freeze` | 将任务空间设为只读，对所有挂载它的虚拟机立即生效 |
| 5 | 解冻任务空间 | POST | `/v1/workspaces/{workspace_id}/unfreeze` | 将已冻结的任务空间恢复为可写 |
| 6 | 复制任务空间 | POST | `/v1/workspaces/{workspace_id}/copy` | 以已冻结的任务空间为内容，为另一个任务新建一个可写的任务空间 |

### 4 轨迹采集与记录

注：轨迹采集运行在虚拟机内部，自动采集可观察的操作轨迹（屏幕录像、鼠标键盘事件）。Agent 的调用轨迹无法在虚拟机内采集，应由外部系统负责记录，并保留项目 id、任务 id、虚拟机 id 和时间戳，便于后续关联。

虚拟机进入 `running` 后自动开始采集，停止或删除前自动结束采集。本组的启停接口用于补充控制；重复启动会返回当前的 `recording_id`，不会重复录制。

| 序号 | 接口名称 | 方法 | 路径 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 启动轨迹采集 | POST | `/v1/vms/{vm_id}/recordings` | 启动虚拟机的轨迹采集，关联项目和任务，返回 `recording_id` |
| 2 | 查询采集记录列表 | GET | `/v1/recordings` | 查询采集记录列表，支持按项目 id、任务 id、虚拟机 id 筛选，按采集开始时间倒序 |
| 3 | 查询采集记录详情 | GET | `/v1/recordings/{recording_id}` | 查询采集记录的关联信息、状态（采集中/已停止/失败）、开始和停止时间、结果路径 |
| 4 | 停止轨迹采集 | POST | `/v1/recordings/{recording_id}/stop` | 停止轨迹采集 |

---

## 1 查询可用软件环境与宿主机资源

### 1.1 查询可用软件环境

**接口说明**

查询可用的软件环境，包括操作系统、软件组合与版本、最低资源配置。创建虚拟机时，通过 `environment_id` 选择环境。

**请求地址**

`GET /v1/environments`

**请求参数**

无

**响应参数**

| 参数名称 | 类型 | 描述 |
| --- | --- | --- |
| data.items | Array | 软件环境列表 |
| data.items[].environment_id | String | 环境 ID，创建虚拟机时使用 |
| data.items[].os | String | 操作系统 |
| data.items[].software | Array | 预装的软件列表 |
| data.items[].software[].name | String | 软件名称 |
| data.items[].software[].version | String | 软件版本 |
| data.items[].min_spec.cpu_cores | Integer | 最低 CPU 核数 |
| data.items[].min_spec.ram_gb | Integer | 最低内存（GB） |

**调用示例**

请求示例：

```bash
curl http://<ip>:<port>/v1/environments
```

成功响应示例：

```json
{
  "data": {
    "items": [
      {
        "environment_id": "win11-autocad2026",
        "os": "windows11",
        "software": [
          {"name": "AutoCAD", "version": "2026"}
        ],
        "min_spec": {"cpu_cores": 4, "ram_gb": 8}
      }
    ]
  }
}
```

---

### 1.2 查询宿主机资源

**接口说明**

查询宿主机 CPU、内存、磁盘的总量、已占用量和可分配量，以及虚拟机数量。

- 每台虚拟机实际占用的内存为 `ram_gb + 1` GB，其中 1 GB 是虚拟化开销。
- CPU 和内存按已分配给虚拟机的额度计算，不是实时使用率。
- 返回的是查询时刻的数据，不会预留资源。

**请求地址**

`GET /v1/host/resources`

**请求参数**

无

**响应参数**

| 参数名称 | 类型 | 描述 |
| --- | --- | --- |
| data.cpu.total | Integer | 宿主机 CPU 总核数 |
| data.cpu.allocated | Integer | 已分配给虚拟机的核数 |
| data.cpu.available | Integer | 可分配给新虚拟机的核数。宿主机自身需要预留一部分，因此小于 `total − allocated` |
| data.memory_gb.total | Integer | 宿主机内存总量（GB） |
| data.memory_gb.allocated | Integer | 已分配给虚拟机的内存（GB），每台按 `ram_gb + 1` 计算 |
| data.memory_gb.available | Integer | 可分配给新虚拟机的内存（GB）。宿主机自身需要预留一部分，因此小于 `total − allocated`；宿主机实际空闲内存更少时，以实际空闲内存为准 |
| data.disk_gb.total | Integer | 项目公共空间、任务空间和采集结果所在磁盘的总容量（GB） |
| data.disk_gb.used | Integer | 该磁盘已使用的容量（GB） |
| data.disk_gb.available | Integer | 该磁盘剩余的容量（GB） |
| data.vms.running | Integer | 正在占用 CPU 和内存的虚拟机数 |
| data.vms.queued | Integer | 排队中的虚拟机数 |

**调用示例**

请求示例：

```bash
curl http://<ip>:<port>/v1/host/resources
```

成功响应示例：

```json
{
  "data": {
    "cpu": {"total": 32, "allocated": 8, "available": 22},
    "memory_gb": {"total": 128, "allocated": 18, "available": 106},
    "disk_gb": {"total": 2000, "used": 500, "available": 1500},
    "vms": {"running": 2, "queued": 0}
  }
}
```

---

## 2 虚拟机管理

### 2.1 查询虚拟机列表

**接口说明**

按创建时间倒序，返回全部未删除的虚拟机。支持按项目id、任务id、状态筛选。

**请求地址**

`GET /v1/vms`

**请求参数**

| 参数名称 | 位置 | 类型 | 必填 | 描述 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| project_id | Query | String | 否 | 按项目 ID 筛选 | proj_001 |
| task_id | Query | String | 否 | 按任务 ID 筛选 | task_B |
| status | Query | String | 否 | 按状态筛选，多个状态用逗号分隔 | running,starting |

**响应参数**

| 参数名称 | 类型 | 描述 |
| --- | --- | --- |
| data.items | Array | 虚拟机列表 |
| data.items[].vm_id | String | 虚拟机 ID |
| data.items[].project_id | String | 项目 ID |
| data.items[].task_id | String | 任务 ID |
| data.items[].status | String | 状态：`queued` 排队中、`creating` 创建中、`starting` 启动中、`running` 运行中、`stopping` 停止中、`stopped` 已停止、`deleting` 删除中、`failed` 失败 |

**调用示例**

请求示例：

```bash
curl "http://<ip>:<port>/v1/vms?task_id=task_B&status=running,starting"
```

成功响应示例：

```json
{
  "data": {
    "items": [
      {
        "vm_id": "vm_a1b2c3d4",
        "project_id": "proj_001",
        "task_id": "task_B",
        "status": "running"
      }
    ]
  }
}
```

---

### 2.2 创建虚拟机

**接口说明**

按选定软件环境、资源规格、任务id创建并自动启动虚拟机，自动挂载该任务的任务空间。
本接口为异步接口：请求通过校验后进入排队并立即返回，此时 `status` 固定为 `queued`。资源足够时，几秒内自动开始创建；资源不足时继续排队，按提交的先后顺序依次创建。提交后轮询「查询虚拟机详情」，`status` 变为 `running` 表示可用。

虚拟机中的盘符：

| 盘符 | 内容 | 权限 |
| --- | --- | --- |
| C 盘 | 系统与预装软件 | 读写；停止后保留，删除后清除 |
| D 盘 | 本任务的任务空间。该任务还没有任务空间时，自动创建 | 读写；任务空间被冻结后只读 |
| E 盘 | 上游任务的任务空间，每个上游一个子文件夹，如 `E:\task_A` | 只读 |
| F 盘 | 项目公共空间，存放项目共享资料、知识库等，同一项目的所有虚拟机共享。该项目还没有公共空间时，自动创建 | 读写 |
| G 盘 | 本虚拟机的采集结果，每次采集一个子文件夹 | 读写 |

宿主机上的目录结构如下，调用方可以直接通过这些路径读写文件。根目录由部署配置决定，以下以 `/data/hdd/vm_manager` 为例：

```
/data/hdd/vm_manager/projects/<project_id>/
├── shared/                 F 盘：项目公共空间
├── tasks/<task_id>/        D 盘：本任务的任务空间；作为上游时为 E:\<task_id>
└── recordings/<vm_id>/     G 盘：本虚拟机的采集结果
```

每台虚拟机只挂载与自己相关的目录：D 盘只能看到本任务，G 盘只能看到本虚拟机的采集结果。

- 同一任务可以同时有多台虚拟机，它们共享同一个任务空间；同一项目的所有虚拟机共享项目公共空间。多台虚拟机同时修改同一个文件会产生冲突，需要由调用方协调。
- 虚拟机进入 `running` 后，自动开始轨迹采集。
- 以下三种情况会自动结束：
  - 排队超过 `queue_timeout_minutes` 仍未开始创建：变为 `failed`（QUEUE_TIMEOUT）；
  - 开始创建后 15 分钟仍未就绪：变为 `failed`（STARTUP_TIMEOUT）；
  - 开始创建后超过 `timeout_minutes`：自动停止，变为 `stopped`。
- 每次调用都会新建一台虚拟机。如果请求网络超时，请先查询列表确认是否已创建，不要直接重试。

**请求地址**

`POST /v1/vms`

**请求参数**

| 参数名称 | 位置 | 类型 | 必填 | 描述 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| project_id | Body | String | 是 | 项目 ID，用作目录名，只能包含字母、数字、`-`、`_`，1–64 个字符 | proj_001 |
| task_id | Body | String | 是 | 任务 ID，用作目录名，只能包含字母、数字、`-`、`_`，1–64 个字符 | task_B |
| chat_id | Body | String | 否 | 对话 ID，用于关联采集记录 | chat_01 |
| environment_id | Body | String | 是 | 软件环境 ID，取值见「查询可用软件环境」 | win11-autocad2026-solidworks2025-ansys2024r1 |
| spec.cpu_cores | Body | Integer | 是 | CPU 核数，不能低于所选环境的最低要求 | 4 |
| spec.ram_gb | Body | Integer | 是 | 内存（GB），不能低于所选环境的最低要求 | 8 |
| upstream_task_ids | Body | Array of String | 否 | 需要只读读取的上游任务 ID 列表，这些任务的任务空间必须已冻结；默认为空 | ["task_A"] |
| expose.vnc | Body | Boolean | 否 | 是否开放 VNC 网页，默认 `true` | true |
| expose.rdp | Body | Boolean | 否 | 是否开放 RDP，默认 `true` | true |
| lifecycle.timeout_minutes | Body | Integer | 否 | 每次运行的最长时长（分钟），从开始创建或启动时算起，到期自动停止，默认 120 | 120 |
| lifecycle.queue_timeout_minutes | Body | Integer | 否 | 每次排队的最长时长（分钟），到期仍未开始创建则失败，默认 30 | 30 |

不接受表中以外的字段。Agent 接口端口始终开放，不受 `expose` 控制。

**响应参数**

| 参数名称 | 类型 | 描述 |
| --- | --- | --- |
| data.vm_id | String | 虚拟机 ID |
| data.status | String | 固定为 `queued` |
| data.queue_position | Integer | 提交时的排队序号，从 1 开始 |
| data.workspace_id | String | 本任务的任务空间 ID |

**调用示例**

请求示例：

```bash
curl -X POST http://<ip>:<port>/v1/vms \
  -H "Content-Type: application/json" \
  -d '{"project_id": "proj_001", "task_id": "task_B", "chat_id": "chat_01", "environment_id": "win11-autocad2026-solidworks2025-ansys2024r1", "spec": {"cpu_cores": 4, "ram_gb": 8}, "upstream_task_ids": ["task_A"]}'
```

成功响应示例：

```json
{"data": {"vm_id": "vm_a1b2c3d4", "status": "queued", "queue_position": 1, "workspace_id": "ws_5e6f7a8b"}}
```

---

### 2.3 查询虚拟机详情

**接口说明**

查询虚拟机的配置、连接信息、状态与失败原因、时间、关联的任务空间与采集记录。提交创建或启动后，轮询此接口（建议每 5 秒一次）。

- `status` 为 `running` 表示虚拟机可用：容器正在运行，探活地址返回成功，已开放的 VNC 和 RDP 可以连接。
- 运行期间系统会持续检查，连续 3 次检查失败则变为 `failed`（HEALTH_CHECK_FAILED），并自动停止。
- 每次启动后，端口都可能变化，请以最新返回的端口为准。
- 停止或失败后，仍会返回原来分配的地址供排查使用，但不保证这些地址仍可访问。

**请求地址**

`GET /v1/vms/{vm_id}`

**请求参数**

| 参数名称 | 位置 | 类型 | 必填 | 描述 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| vm_id | Path | String | 是 | 虚拟机 ID | vm_a1b2c3d4 |

**响应参数**

| 参数名称 | 类型 | 描述 |
| --- | --- | --- |
| data.vm_id | String | 虚拟机 ID |
| data.project_id | String | 项目 ID |
| data.task_id | String | 任务 ID |
| data.chat_id | String | 对话 ID；未指定时为 null |
| data.status | String | 状态：`queued` 排队中、`creating` 创建中、`starting` 启动中、`running` 运行中、`stopping` 停止中、`stopped` 已停止、`deleting` 删除中、`failed` 失败 |
| data.environment_id | String | 软件环境 ID |
| data.os | String | 操作系统 |
| data.software | Array | 预装的软件列表 |
| data.software[].name | String | 软件名称 |
| data.software[].version | String | 软件版本 |
| data.spec.cpu_cores | Integer | CPU 核数 |
| data.spec.ram_gb | Integer | 内存（GB） |
| data.expose.vnc | Boolean | 是否开放 VNC 网页 |
| data.expose.rdp | Boolean | 是否开放 RDP |
| data.lifecycle.timeout_minutes | Integer | 每次运行的最长时长（分钟） |
| data.lifecycle.queue_timeout_minutes | Integer | 每次排队的最长时长（分钟） |
| data.container_id | String | Docker 容器 ID；容器创建前为 null |
| data.access.host | String | 宿主机地址，以下端口都在这个地址上 |
| data.access.vnc_port | Integer | VNC 网页端口，访问 `http://{host}:{vnc_port}`；未开放或容器启动前为 null |
| data.access.rdp_port | Integer | RDP 端口，连接 `{host}:{rdp_port}`；未开放或容器启动前为 null |
| data.access.agent_port | Integer | Agent 接口端口，访问 `http://{host}:{agent_port}`，探活地址为 `http://{host}:{agent_port}/probe`；容器启动前为 null |
| data.mounts.workspace.workspace_id | String | 本任务的任务空间 ID，在虚拟机中为 D 盘 |
| data.mounts.workspace.writable | Boolean | D 盘当前是否可写；任务空间被冻结后为 false |
| data.mounts.upstreams | Array | 只读挂载的上游任务空间，在虚拟机中为 E 盘，每个上游一个子文件夹；没有时为空数组 |
| data.mounts.upstreams[].task_id | String | 上游任务 ID |
| data.mounts.upstreams[].workspace_id | String | 上游任务空间 ID |
| data.mounts.upstreams[].path | String | 在虚拟机中的路径，如 `E:\task_A` |
| data.mounts.project_shared_path | String | 项目公共空间在宿主机上的目录，在虚拟机中为 F 盘 |
| data.mounts.recordings_path | String | 本虚拟机的采集结果在宿主机上的目录，在虚拟机中为 G 盘 |
| data.current_recording_id | String | 正在进行的采集记录 ID；没有时为 null。全部采集记录可通过「查询采集记录列表」按 vm_id 查询 |
| data.queue | Object | 排队信息；仅 `queued` 时有值，其他状态为 null |
| data.queue.position | Integer | 排队序号，从 1 开始 |
| data.queue.reason | String | 等待原因：`waiting_turn` 前面还有请求、`insufficient_resources` 宿主机资源不足、`docker_unavailable` Docker 暂时不可用 |
| data.queue.expires_at | String | 排队截止时间，到期仍未开始创建则变为 `failed` |
| data.created_at | String | 创建时间 |
| data.started_at | String | 本次离开队列、开始运行的时间；排队中为 null |
| data.expires_at | String | 本次运行到期自动停止的时间，等于 `started_at` 加上 `timeout_minutes`；排队中为 null |
| data.stopped_at | String | 停止时间；未停止，或重新启动后，为 null |
| data.stop_reason | String | 停止原因：`manual` 手动停止、`cancelled` 排队中被取消、`expired` 到期自动停止、`failure` 失败后自动停止；未停止，或重新启动后，为 null |
| data.error | Object | 失败信息；没有失败时为 null |
| data.error.code | String | `QUEUE_TIMEOUT` 排队超时、`HOST_NOT_READY` 宿主机环境未就绪、`PROVISION_FAILED` 创建失败、`STARTUP_TIMEOUT` 启动超时、`CONTAINER_EXITED` 容器异常退出、`HEALTH_CHECK_FAILED` 运行中连续 3 次检查失败 |
| data.error.message | String | 失败原因说明 |

**调用示例**

请求示例：

```bash
curl http://<ip>:<port>/v1/vms/vm_a1b2c3d4
```

成功响应示例：

```json
{
  "data": {
    "vm_id": "vm_a1b2c3d4",
    "project_id": "proj_001",
    "task_id": "task_B",
    "chat_id": "chat_01",
    "status": "running",
    "environment_id": "win11-autocad2026-solidworks2025-ansys2024r1",
    "os": "windows11",
    "software": [
      {"name": "AutoCAD", "version": "2026"},
      {"name": "SolidWorks", "version": "2025"},
      {"name": "ANSYS", "version": "2024 R1"}
    ],
    "spec": {"cpu_cores": 4, "ram_gb": 8},
    "expose": {"vnc": true, "rdp": true},
    "lifecycle": {"timeout_minutes": 120, "queue_timeout_minutes": 30},
    "container_id": "3f9c2e7a1b5d...",
    "access": {"host": "10.0.0.10", "vnc_port": 32781, "rdp_port": 32782, "agent_port": 32783},
    "mounts": {
      "workspace": {"workspace_id": "ws_5e6f7a8b", "writable": true},
      "upstreams": [
        {"task_id": "task_A", "workspace_id": "ws_1a2b3c4d", "path": "E:\\task_A"}
      ],
      "project_shared_path": "/data/hdd/vm_manager/projects/proj_001/shared",
      "recordings_path": "/data/hdd/vm_manager/projects/proj_001/recordings/vm_a1b2c3d4"
    },
    "current_recording_id": "rec_9c0d1e2f",
    "queue": null,
    "created_at": "2026-10-11T10:00:00Z",
    "started_at": "2026-10-11T10:00:05Z",
    "expires_at": "2026-10-11T12:00:05Z",
    "stopped_at": null,
    "stop_reason": null,
    "error": null
  }
}
```

排队中的响应示例（只列出与上面不同的字段）：

```json
{
  "data": {
    "status": "queued",
    "container_id": null,
    "access": {"host": "10.0.0.10", "vnc_port": null, "rdp_port": null, "agent_port": null},
    "current_recording_id": null,
    "queue": {"position": 2, "reason": "waiting_turn", "expires_at": "2026-10-11T10:30:00Z"},
    "started_at": null,
    "expires_at": null
  }
}
```

---

### 2.4 启动虚拟机

**接口说明**

重新启动已停止的虚拟机，不创建新实例。本接口为异步接口，调用后虚拟机重新进入排队，提交后轮询「查询虚拟机详情」。

- 排队、排队超时、启动超时、运行到期的规则，与创建时相同；`started_at` 和 `expires_at` 按本次启动重新计算。
- C 盘保留上次停止时的内容；D、E、F、G 盘的挂载与创建时相同。
- **启动后端口可能变化，请以最新返回的端口为准。**

| 当前状态 | 结果 |
| --- | --- |
| stopped | 重新进入排队，`status` 变为 `queued` |
| queued / creating / starting / running | 已在启动或运行中，直接返回当前状态 |
| stopping / deleting | 返回 409 VM_BUSY，请稍后重试 |
| failed | 返回 409 VM_FAILED。失败的虚拟机不能启动，请删除后重新创建 |

**请求地址**

`POST /v1/vms/{vm_id}/start`

**请求参数**

| 参数名称 | 位置 | 类型 | 必填 | 描述 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| vm_id | Path | String | 是 | 虚拟机 ID | vm_a1b2c3d4 |

**响应参数**

| 参数名称 | 类型 | 描述 |
| --- | --- | --- |
| data.vm_id | String | 虚拟机 ID |
| data.status | String | 当前状态 |
| data.queue_position | Integer | 排队序号，从 1 开始；不在排队中时为 null |

**调用示例**

请求示例：

```bash
curl -X POST http://<ip>:<port>/v1/vms/vm_a1b2c3d4/start
```

成功响应示例：

```json
{"data": {"vm_id": "vm_a1b2c3d4", "status": "queued", "queue_position": 1}}
```

---

### 2.5 停止虚拟机

**接口说明**

停止虚拟机，释放 CPU 和内存。C 盘内容、任务空间关联和采集结果都保留，之后可以调用「启动虚拟机」重新启动。正在进行的轨迹采集会先自动结束。

本接口为同步接口，需要等待 Windows 关机，最长约 2 分钟，客户端超时请设为 180 秒以上。

| 当前状态 | 结果 |
| --- | --- |
| queued | 取消排队；`status` 变为 `stopped`，`stop_reason` 为 `cancelled` |
| starting / running | 结束采集、关机后停止；`status` 变为 `stopped`，`stop_reason` 为 `manual` |
| stopped / failed | 已经处于停止状态，直接返回当前状态 |
| stopping，且上次停止失败 | 重新执行停止 |
| creating / deleting，或正在停止中 | 返回 409 VM_BUSY，请稍后重试 |

**请求地址**

`POST /v1/vms/{vm_id}/stop`

**请求参数**

| 参数名称 | 位置 | 类型 | 必填 | 描述 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| vm_id | Path | String | 是 | 虚拟机 ID | vm_a1b2c3d4 |

**响应参数**

| 参数名称 | 类型 | 描述 |
| --- | --- | --- |
| data.vm_id | String | 虚拟机 ID |
| data.status | String | 当前状态：`stopped` 已停止，或 `failed` 失败（失败的虚拟机已被自动停止） |
| data.stop_reason | String | 停止原因：`manual` 手动停止、`cancelled` 排队中被取消、`expired` 到期自动停止、`failure` 失败后自动停止；从未运行过的失败虚拟机为 null |
| data.stopped_at | String | 停止时间；从未运行过的失败虚拟机为 null |

**调用示例**

请求示例：

```bash
curl -X POST http://<ip>:<port>/v1/vms/vm_a1b2c3d4/stop
```

成功响应示例：

```json
{"data": {"vm_id": "vm_a1b2c3d4", "status": "stopped", "stop_reason": "manual", "stopped_at": "2026-10-11T11:00:30Z"}}
```

---

### 2.6 删除虚拟机

**接口说明**

删除虚拟机的容器、C 盘内容和记录，释放全部资源。任务空间和采集记录保留。删除后，再查询该虚拟机会返回 404。正在进行的轨迹采集会先自动结束。

本接口为同步接口：运行中的虚拟机需要先关机，最长约 2 分钟，客户端超时请设为 180 秒以上。

| 当前状态 | 结果 |
| --- | --- |
| queued（从未启动过） | 取消排队并删除记录 |
| starting / running | 结束采集、关机后删除 |
| queued（重新启动排队中）/ stopped / failed | 直接删除 |
| stopping / deleting，且上次操作失败 | 重新执行删除 |
| creating，或正在停止、删除中 | 返回 409 VM_BUSY，请稍后重试 |
| 不存在（包括已删除） | 同样返回成功 |

**请求地址**

`DELETE /v1/vms/{vm_id}`

**请求参数**

| 参数名称 | 位置 | 类型 | 必填 | 描述 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| vm_id | Path | String | 是 | 虚拟机 ID | vm_a1b2c3d4 |

**响应参数**

| 参数名称 | 类型 | 描述 |
| --- | --- | --- |
| data.vm_id | String | 虚拟机 ID |
| data.status | String | 固定为 `deleted` |

**调用示例**

请求示例：

```bash
curl -X DELETE http://<ip>:<port>/v1/vms/vm_a1b2c3d4
```

成功响应示例：

```json
{"data": {"vm_id": "vm_a1b2c3d4", "status": "deleted"}}
```

---

## 3 任务空间管理

注：每个任务最多有一个任务空间，它是宿主机上的目录 `projects/<project_id>/tasks/<task_id>/`，在虚拟机中为 D 盘（本任务）或 E 盘下的子文件夹（作为上游）。创建虚拟机时，如果该任务还没有任务空间，会自动创建；本组接口仅用于补充控制。v1 不提供删除接口。

### 3.1 查询任务空间列表

**接口说明**

按创建时间倒序返回任务空间列表。支持按项目id、任务id、状态筛选。

**请求地址**

`GET /v1/workspaces`

**请求参数**

| 参数名称 | 位置 | 类型 | 必填 | 描述 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| project_id | Query | String | 否 | 按项目 ID 筛选 | proj_001 |
| task_id | Query | String | 否 | 按任务 ID 筛选 | task_A |
| status | Query | String | 否 | 按状态筛选：`writable` 或 `frozen` | frozen |

**响应参数**

| 参数名称 | 类型 | 描述 |
| --- | --- | --- |
| data.total | Integer | 任务空间数量 |
| data.items | Array | 任务空间列表 |
| data.items[].workspace_id | String | 任务空间 ID |
| data.items[].project_id | String | 项目 ID |
| data.items[].task_id | String | 任务 ID |
| data.items[].status | String | 状态：`writable` 可写、`frozen` 已冻结（只读） |
| data.items[].path | String | 任务空间在宿主机上的目录 |


**调用示例**

请求示例：

```bash
curl "http://<ip>:<port>/v1/workspaces?project_id=proj_001&status=frozen"
```

成功响应示例：

```json
{
  "data": {
    "total": 1,
    "items": [
      {
        "workspace_id": "ws_1a2b3c4d",
        "project_id": "proj_001",
        "task_id": "task_A",
        "status": "frozen",
        "path": "/data/hdd/vm_manager/projects/proj_001/tasks/task_A"
      }
    ]
  }
}
```

---

### 3.2 查询任务空间详情

**接口说明**

查询任务空间的状态（可写/已冻结）、关联信息和挂载情况。

**请求地址**

`GET /v1/workspaces/{workspace_id}`

**请求参数**

| 参数名称 | 位置 | 类型 | 必填 | 描述 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| workspace_id | Path | String | 是 | 任务空间 ID | ws_1a2b3c4d |

**响应参数**

| 参数名称 | 类型 | 描述 |
| --- | --- | --- |
| data.workspace_id | String | 任务空间 ID |
| data.project_id | String | 项目 ID |
| data.task_id | String | 任务 ID |
| data.status | String | 状态：`writable` 可写、`frozen` 已冻结（只读） |
| data.path | String | 任务空间在宿主机上的目录 |
| data.source_workspace_id | String | 复制来源的任务空间 ID；不是复制得到的为 null |
| data.mounted_by | Array | 当前挂载该空间的虚拟机（不含已删除的）；没有时为空数组 |
| data.mounted_by[].vm_id | String | 虚拟机 ID |
| data.mounted_by[].path | String | 在虚拟机中的路径：作为本任务空间时为 `D:`，作为上游时为 `E:\{task_id}` |
| data.created_at | String | 创建时间 |
| data.frozen_at | String | 冻结时间；可写时为 null |

**调用示例**

请求示例：

```bash
curl http://<ip>:<port>/v1/workspaces/ws_1a2b3c4d
```

成功响应示例：

```json
{
  "data": {
    "workspace_id": "ws_1a2b3c4d",
    "project_id": "proj_001",
    "task_id": "task_A",
    "status": "frozen",
    "path": "/data/hdd/vm_manager/projects/proj_001/tasks/task_A",
    "source_workspace_id": null,
    "mounted_by": [{"vm_id": "vm_a1b2c3d4", "path": "E:\\task_A"}],
    "created_at": "2026-10-10T08:00:00Z",
    "frozen_at": "2026-10-10T16:00:00Z"
  }
}
```

---

### 3.3 创建任务空间

**接口说明**

为指定任务创建一个空的、可写的任务空间。通常不需要调用：创建虚拟机时会自动创建。每个任务最多只能有一个任务空间。

**请求地址**

`POST /v1/workspaces`

**请求参数**

| 参数名称 | 位置 | 类型 | 必填 | 描述 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| project_id | Body | String | 是 | 项目 ID，用作目录名，只能包含字母、数字、`-`、`_`，1–64 个字符 | proj_001 |
| task_id | Body | String | 是 | 任务 ID，用作目录名，只能包含字母、数字、`-`、`_`，1–64 个字符 | task_A |

**响应参数**

| 参数名称 | 类型 | 描述 |
| --- | --- | --- |
| data.workspace_id | String | 任务空间 ID |
| data.project_id | String | 项目 ID |
| data.task_id | String | 任务 ID |
| data.status | String | 固定为 `writable` |
| data.path | String | 任务空间在宿主机上的目录 |
| data.source_workspace_id | String | 固定为 null |
| data.mounted_by | Array | 固定为空数组 |
| data.created_at | String | 创建时间 |
| data.frozen_at | String | 固定为 null |

**调用示例**

请求示例：

```bash
curl -X POST http://<ip>:<port>/v1/workspaces \
  -H "Content-Type: application/json" \
  -d '{"project_id": "proj_001", "task_id": "task_A"}'
```

成功响应示例：

```json
{
  "data": {
    "workspace_id": "ws_1a2b3c4d",
    "project_id": "proj_001",
    "task_id": "task_A",
    "status": "writable",
    "path": "/data/hdd/vm_manager/projects/proj_001/tasks/task_A",
    "source_workspace_id": null,
    "mounted_by": [],
    "created_at": "2026-10-10T08:00:00Z",
    "frozen_at": null
  }
}
```

---

### 3.4 冻结任务空间

**接口说明**

将任务空间设为只读，对所有挂载它的虚拟机立即生效，此后其中的文件无法再被修改。通常在任务完成时调用。

- 下游任务可以在创建虚拟机时，通过 `upstream_task_ids` 只读读取已冻结的任务空间。
- 需要在已冻结的内容上继续修改时，请使用「复制任务空间」新建一个可写的空间。
- 已冻结时重复调用，直接返回当前状态。

**请求地址**

`POST /v1/workspaces/{workspace_id}/freeze`

**请求参数**

| 参数名称 | 位置 | 类型 | 必填 | 描述 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| workspace_id | Path | String | 是 | 任务空间 ID | ws_1a2b3c4d |

**响应参数**

| 参数名称 | 类型 | 描述 |
| --- | --- | --- |
| data.workspace_id | String | 任务空间 ID |
| data.project_id | String | 项目 ID |
| data.task_id | String | 任务 ID |
| data.status | String | 固定为 `frozen` |
| data.path | String | 任务空间在宿主机上的目录 |
| data.source_workspace_id | String | 复制来源的任务空间 ID；不是复制得到的为 null |
| data.mounted_by | Array | 当前挂载该空间的虚拟机（不含已删除的）；没有时为空数组 |
| data.mounted_by[].vm_id | String | 虚拟机 ID |
| data.mounted_by[].path | String | 在虚拟机中的路径：作为本任务空间时为 `D:`，作为上游时为 `E:\{task_id}` |
| data.created_at | String | 创建时间 |
| data.frozen_at | String | 冻结时间 |

**调用示例**

请求示例：

```bash
curl -X POST http://<ip>:<port>/v1/workspaces/ws_1a2b3c4d/freeze
```

成功响应示例：

```json
{
  "data": {
    "workspace_id": "ws_1a2b3c4d",
    "project_id": "proj_001",
    "task_id": "task_A",
    "status": "frozen",
    "path": "/data/hdd/vm_manager/projects/proj_001/tasks/task_A",
    "source_workspace_id": null,
    "mounted_by": [{"vm_id": "vm_e5f6a7b8", "path": "D:"}],
    "created_at": "2026-10-10T08:00:00Z",
    "frozen_at": "2026-10-10T16:00:00Z"
  }
}
```

---

### 3.5 解冻任务空间

**接口说明**

将已冻结的任务空间恢复为可写：

- 将它作为本任务空间挂载（D 盘）的虚拟机，恢复写入权限；
- 将它作为上游挂载（E 盘）的虚拟机，仍然只读。

已经是可写状态时重复调用，直接返回当前状态。

**请求地址**

`POST /v1/workspaces/{workspace_id}/unfreeze`

**请求参数**

| 参数名称 | 位置 | 类型 | 必填 | 描述 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| workspace_id | Path | String | 是 | 任务空间 ID | ws_1a2b3c4d |

**响应参数**

| 参数名称 | 类型 | 描述 |
| --- | --- | --- |
| data.workspace_id | String | 任务空间 ID |
| data.project_id | String | 项目 ID |
| data.task_id | String | 任务 ID |
| data.status | String | 固定为 `writable` |
| data.path | String | 任务空间在宿主机上的目录 |
| data.source_workspace_id | String | 复制来源的任务空间 ID；不是复制得到的为 null |
| data.mounted_by | Array | 当前挂载该空间的虚拟机（不含已删除的）；没有时为空数组 |
| data.mounted_by[].vm_id | String | 虚拟机 ID |
| data.mounted_by[].path | String | 在虚拟机中的路径：作为本任务空间时为 `D:`，作为上游时为 `E:\{task_id}` |
| data.created_at | String | 创建时间 |
| data.frozen_at | String | 固定为 null |

**调用示例**

请求示例：

```bash
curl -X POST http://<ip>:<port>/v1/workspaces/ws_1a2b3c4d/unfreeze
```

成功响应示例：

```json
{
  "data": {
    "workspace_id": "ws_1a2b3c4d",
    "project_id": "proj_001",
    "task_id": "task_A",
    "status": "writable",
    "path": "/data/hdd/vm_manager/projects/proj_001/tasks/task_A",
    "source_workspace_id": null,
    "mounted_by": [],
    "created_at": "2026-10-10T08:00:00Z",
    "frozen_at": null
  }
}
```

---

### 3.6 复制任务空间

**接口说明**

以一个已冻结的任务空间为内容，为另一个任务新建一个可写的任务空间。用于在已提交的成果上继续加工或纠错：原空间保持不变，在新任务的空间中修改。

- 源任务空间必须已冻结。
- 目标任务不能已有任务空间。
- 本接口为同步接口，耗时取决于数据量，客户端需适当延长超时时间。

**请求地址**

`POST /v1/workspaces/{workspace_id}/copy`

**请求参数**

| 参数名称 | 位置 | 类型 | 必填 | 描述 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| workspace_id | Path | String | 是 | 源任务空间 ID，必须已冻结 | ws_1a2b3c4d |
| project_id | Body | String | 是 | 新任务空间所属的项目 ID，只能包含字母、数字、`-`、`_`，1–64 个字符 | proj_001 |
| task_id | Body | String | 是 | 新任务空间所属的任务 ID，该任务不能已有任务空间；只能包含字母、数字、`-`、`_`，1–64 个字符 | task_C |

**响应参数**

| 参数名称 | 类型 | 描述 |
| --- | --- | --- |
| data.workspace_id | String | 新任务空间 ID |
| data.project_id | String | 项目 ID |
| data.task_id | String | 任务 ID |
| data.status | String | 固定为 `writable` |
| data.path | String | 新任务空间在宿主机上的目录 |
| data.source_workspace_id | String | 源任务空间 ID |
| data.mounted_by | Array | 固定为空数组 |
| data.created_at | String | 创建时间 |
| data.frozen_at | String | 固定为 null |

**调用示例**

请求示例：

```bash
curl -X POST http://<ip>:<port>/v1/workspaces/ws_1a2b3c4d/copy \
  -H "Content-Type: application/json" \
  -d '{"project_id": "proj_001", "task_id": "task_C"}'
```

成功响应示例：

```json
{
  "data": {
    "workspace_id": "ws_7c8d9e0f",
    "project_id": "proj_001",
    "task_id": "task_C",
    "status": "writable",
    "path": "/data/hdd/vm_manager/projects/proj_001/tasks/task_C",
    "source_workspace_id": "ws_1a2b3c4d",
    "mounted_by": [],
    "created_at": "2026-10-11T09:00:00Z",
    "frozen_at": null
  }
}
```

---

## 4 轨迹采集与记录

注：每台虚拟机进入 `running` 后自动开始采集，停止或删除前自动结束采集。采集结果保存在宿主机的目录 `projects/<project_id>/recordings/<vm_id>/` 中，在虚拟机中为 G 盘，每次采集一个子文件夹。

### 4.1 启动轨迹采集

**接口说明**

为运行中的虚拟机启动轨迹采集，关联项目、任务和对话，返回 `recording_id`。

- 只能对 `running` 状态的虚拟机调用。
- 该虚拟机已有采集正在进行时，直接返回这条采集记录，不会重复录制。

**请求地址**

`POST /v1/vms/{vm_id}/recordings`

**请求参数**

| 参数名称 | 位置 | 类型 | 必填 | 描述 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| vm_id | Path | String | 是 | 虚拟机 ID | vm_a1b2c3d4 |
| chat_id | Body | String | 否 | 对话 ID；不传时使用虚拟机的 chat_id | chat_02 |

**响应参数**

| 参数名称 | 类型 | 描述 |
| --- | --- | --- |
| data.recording_id | String | 采集记录 ID |
| data.vm_id | String | 虚拟机 ID |
| data.project_id | String | 项目 ID |
| data.task_id | String | 任务 ID |
| data.chat_id | String | 对话 ID；未指定时为 null |
| data.status | String | 状态：`recording` 采集中 |
| data.started_at | String | 开始采集的时间 |
| data.stopped_at | String | 固定为 null |
| data.result_path | String | 采集结果在宿主机上的目录，在虚拟机中为 `G:\{recording_id}` |
| data.error | String | 固定为 null |

**调用示例**

请求示例：

```bash
curl -X POST http://<ip>:<port>/v1/vms/vm_a1b2c3d4/recordings \
  -H "Content-Type: application/json" \
  -d '{"chat_id": "chat_02"}'
```

成功响应示例：

```json
{
  "data": {
    "recording_id": "rec_9c0d1e2f",
    "vm_id": "vm_a1b2c3d4",
    "project_id": "proj_001",
    "task_id": "task_B",
    "chat_id": "chat_02",
    "status": "recording",
    "started_at": "2026-10-11T10:05:00Z",
    "stopped_at": null,
    "result_path": "/data/hdd/vm_manager/projects/proj_001/recordings/vm_a1b2c3d4/rec_9c0d1e2f",
    "error": null
  }
}
```

---

### 4.2 查询采集记录列表

**接口说明**

按开始采集的时间倒序，返回采集记录列表。支持按项目id、任务id、虚拟机id筛选。虚拟机删除后，它的采集记录仍然保留。

**请求地址**

`GET /v1/recordings`

**请求参数**

| 参数名称 | 位置 | 类型 | 必填 | 描述 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| project_id | Query | String | 否 | 按项目 ID 筛选 | proj_001 |
| task_id | Query | String | 否 | 按任务 ID 筛选 | task_B |
| vm_id | Query | String | 否 | 按虚拟机 ID 筛选 | vm_a1b2c3d4 |

**响应参数**

| 参数名称 | 类型 | 描述 |
| --- | --- | --- |
| data.items | Array | 采集记录列表 |
| data.items[].recording_id | String | 采集记录 ID |
| data.items[].vm_id | String | 虚拟机 ID |
| data.items[].project_id | String | 项目 ID |
| data.items[].task_id | String | 任务 ID |
| data.items[].chat_id | String | 对话 ID；未指定时为 null |
| data.items[].status | String | 状态：`recording` 采集中、`stopped` 已停止、`failed` 失败 |
| data.items[].started_at | String | 开始采集的时间 |
| data.items[].stopped_at | String | 停止采集的时间；采集中为 null |
| data.items[].result_path | String | 采集结果在宿主机上的目录，包含屏幕录像和鼠标键盘事件，具体文件由采集程序决定 |
| data.items[].error | String | 失败原因；没有失败时为 null |

**调用示例**

请求示例：

```bash
curl "http://<ip>:<port>/v1/recordings?task_id=task_B"
```

成功响应示例：

```json
{
  "data": {
    "items": [
      {
        "recording_id": "rec_9c0d1e2f",
        "vm_id": "vm_a1b2c3d4",
        "project_id": "proj_001",
        "task_id": "task_B",
        "chat_id": "chat_01",
        "status": "stopped",
        "started_at": "2026-10-11T10:00:20Z",
        "stopped_at": "2026-10-11T11:00:10Z",
        "result_path": "/data/hdd/vm_manager/projects/proj_001/recordings/vm_a1b2c3d4/rec_9c0d1e2f",
        "error": null
      }
    ]
  }
}
```

---

### 4.3 查询采集记录详情

**接口说明**

查询采集记录的关联信息、状态、开始和停止时间，以及结果路径。

**请求地址**

`GET /v1/recordings/{recording_id}`

**请求参数**

| 参数名称 | 位置 | 类型 | 必填 | 描述 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| recording_id | Path | String | 是 | 采集记录 ID | rec_9c0d1e2f |

**响应参数**

| 参数名称 | 类型 | 描述 |
| --- | --- | --- |
| data.recording_id | String | 采集记录 ID |
| data.vm_id | String | 虚拟机 ID |
| data.project_id | String | 项目 ID |
| data.task_id | String | 任务 ID |
| data.chat_id | String | 对话 ID；未指定时为 null |
| data.status | String | 状态：`recording` 采集中、`stopped` 已停止、`failed` 失败 |
| data.started_at | String | 开始采集的时间 |
| data.stopped_at | String | 停止采集的时间；采集中为 null |
| data.result_path | String | 采集结果在宿主机上的目录，包含屏幕录像和鼠标键盘事件，具体文件由采集程序决定 |
| data.error | String | 失败原因；没有失败时为 null |

**调用示例**

请求示例：

```bash
curl http://<ip>:<port>/v1/recordings/rec_9c0d1e2f
```

成功响应示例：

```json
{
  "data": {
    "recording_id": "rec_9c0d1e2f",
    "vm_id": "vm_a1b2c3d4",
    "project_id": "proj_001",
    "task_id": "task_B",
    "chat_id": "chat_01",
    "status": "recording",
    "started_at": "2026-10-11T10:00:20Z",
    "stopped_at": null,
    "result_path": "/data/hdd/vm_manager/projects/proj_001/recordings/vm_a1b2c3d4/rec_9c0d1e2f",
    "error": null
  }
}
```

---

### 4.4 停止轨迹采集

**接口说明**

停止轨迹采集。本接口为同步接口，会等待采集程序把结果文件写完后再返回。已停止或已失败的采集记录，重复调用直接返回当前状态。

**请求地址**

`POST /v1/recordings/{recording_id}/stop`

**请求参数**

| 参数名称 | 位置 | 类型 | 必填 | 描述 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| recording_id | Path | String | 是 | 采集记录 ID | rec_9c0d1e2f |

**响应参数**

| 参数名称 | 类型 | 描述 |
| --- | --- | --- |
| data.recording_id | String | 采集记录 ID |
| data.vm_id | String | 虚拟机 ID |
| data.project_id | String | 项目 ID |
| data.task_id | String | 任务 ID |
| data.chat_id | String | 对话 ID；未指定时为 null |
| data.status | String | 状态：`stopped` 已停止，或 `failed` 失败 |
| data.started_at | String | 开始采集的时间 |
| data.stopped_at | String | 停止采集的时间 |
| data.result_path | String | 采集结果在宿主机上的目录，包含屏幕录像和鼠标键盘事件，具体文件由采集程序决定 |
| data.error | String | 失败原因；没有失败时为 null |

**调用示例**

请求示例：

```bash
curl -X POST http://<ip>:<port>/v1/recordings/rec_9c0d1e2f/stop
```

成功响应示例：

```json
{
  "data": {
    "recording_id": "rec_9c0d1e2f",
    "vm_id": "vm_a1b2c3d4",
    "project_id": "proj_001",
    "task_id": "task_B",
    "chat_id": "chat_01",
    "status": "stopped",
    "started_at": "2026-10-11T10:00:20Z",
    "stopped_at": "2026-10-11T11:00:10Z",
    "result_path": "/data/hdd/vm_manager/projects/proj_001/recordings/vm_a1b2c3d4/rec_9c0d1e2f",
    "error": null
  }
}
```

## 5 返回码

| HTTP 状态码 | code | 说明 | details |
| --- | --- | --- | --- |
| 200 | — | 成功 | — |
| 404 | VM_NOT_FOUND | 虚拟机不存在 | `vm_id` |
| 404 | WORKSPACE_NOT_FOUND | 任务空间不存在 | `workspace_id` |
| 404 | RECORDING_NOT_FOUND | 采集记录不存在 | `recording_id` |
| 409 | VM_BUSY | 虚拟机正在创建、停止或删除中，请稍后重试 | `status`：当前状态 |
| 409 | VM_FAILED | 虚拟机处于失败状态，不能启动，请删除后重新创建 | `vm_id` |
| 409 | VM_NOT_RUNNING | 虚拟机不在 `running` 状态 | `status`：当前状态 |
| 409 | UPSTREAM_NOT_FROZEN | 上游任务的任务空间尚未冻结 | `task_ids`：尚未冻结的上游任务 |
| 409 | WORKSPACE_EXISTS | 该任务已有任务空间 | `task_id`；`workspace_id`：已有的任务空间 ID |
| 409 | WORKSPACE_NOT_FROZEN | 任务空间尚未冻结 | `workspace_id` |
| 422 | VALIDATION_ERROR | 缺少必填字段、类型错误、取值非法（包括 `project_id`、`task_id` 含有不允许的字符）、存在重复项，或出现未知字段 | `fields`：出错字段列表，如 `[{"field": "spec.cpu_cores", "reason": "必须为正整数"}]` |
| 422 | ENVIRONMENT_NOT_FOUND | 软件环境不存在 | `requested`：请求的 environment_id；`supported`：全部可用的 environment_id |
| 422 | BELOW_ENVIRONMENT_MINIMUM | 资源规格低于环境最低配置 | `requested`：请求的规格；`minimum`：最低配置 |
| 422 | REQUEST_EXCEEDS_CAPACITY | 资源规格超出宿主机总容量，即使宿主机空闲也无法创建 | `requested`：请求的规格；`required`：含开销的实际占用；`capacity`：宿主机可分配总量 |
| 422 | UPSTREAM_NOT_FOUND | 上游任务没有任务空间 | `task_ids`：没有任务空间的上游任务 |
| 500 | STOP_FAILED | 停止虚拟机未完成，可再次调用重试 | `vm_id` |
| 500 | DELETE_FAILED | 删除虚拟机未完成，记录仍保留，可再次调用重试 | `vm_id` |
| 500 | RECORDING_START_FAILED | 虚拟机内的采集程序启动失败 | `vm_id` |
| 500 | RECORDING_STOP_FAILED | 虚拟机内的采集程序停止失败，可再次调用重试 | `recording_id` |
| 500 | INTERNAL_ERROR | 服务内部错误 | null |
| 503 | DOCKER_UNAVAILABLE | Docker 服务不可用，请稍后重试 | null |
| 503 | HOST_NOT_READY | 宿主机环境未就绪 | `missing`：缺少的项，可能为 `kvm`、`image`、`base_disk` |