# VM Manager v1
## 0. 项目背景

HarnessStudio 是面向工业软件（CAD、CAE 等）的人与 Agent 协作平台，按「项目 → 任务 → 对话」组织工作：一个项目包含多个任务，一个任务包含多个对话。同一任务的对话共享工程文件，并可按需申请多个软件席位（Seat）。

本项目实现其中的「专业软件虚拟化子系统」，即一个独立的 VM Manager REST 服务，供 HarnessStudio 后端调用。一个 Seat 对应本服务的一台虚拟机（VM），一台 VM 是运行在一个 Docker 容器中的 Windows 实例，预装 AutoCAD、SolidWorks、ANSYS 等专业软件。

调用方的典型使用顺序是：查询可用的软件环境 → 按任务创建 VM → 获取远程连接地址（VNC、RDP、Agent 接口）→ 用户或 Agent 在 VM 中工作 → 停止或删除 VM。

### 职责划分

| 角色 | 负责的内容 |
| --- | --- |
| HarnessStudio 平台 | 用户鉴权、项目与任务 DAG、任务状态、Seat 控制权 |
| 本项目 | VM 的创建、启动、停止、删除和排队调度；宿主机资源管理；任务空间和项目公共空间的目录管理（挂载、冻结、复制）；轨迹采集的启停和记录 |
| 外部组件（本项目不开发，只接入） | Docker 镜像（含共享文件夹服务）、Windows 基础磁盘（已预装软件、已配置盘符映射）、虚拟机内的 Agent 服务（提供探活和采集启停接口）、轨迹采集程序 |

本服务只把项目、任务、对话的 ID 作为关联字段保存，不校验它们是否存在，也不管理它们的状态。

### 核心概念

| 概念 | 含义 |
| --- | --- |
| 虚拟机（VM） | 运行在 Docker 容器中的 Windows 实例，对应平台的一个 Seat，用完即可停止或删除 |
| 软件环境 | 预装了特定软件组合的 Windows 基础磁盘文件。所有虚拟机都以它为模板，各自的改动叠加在上面，就是虚拟机的 C 盘：停止后保留，删除后清除 |
| 任务空间 | 每个任务一个目录，存放该任务的工程文件。该任务的所有虚拟机共享这个目录，在虚拟机中挂载为 D 盘 |
| 冻结 | 任务完成后，平台调用冻结接口，任务空间变为只读，作为成果存档 |
| 上游 | 下游任务的虚拟机可以只读挂载已冻结的上游任务空间，挂在 E 盘下，每个上游一个子文件夹。需要修改上游成果时，复制出一个新的任务空间 |
| 项目公共空间 | 同一项目的所有任务、所有虚拟机共享的资料目录，用于存放知识库等，挂载为 F 盘 |
| 轨迹采集 | 虚拟机进入运行状态后，自动录制屏幕和键盘鼠标操作，用于积累操作数据。每台虚拟机只能看到自己的采集结果，在 G 盘 |

### 典型流程

1. 平台为任务 A 创建虚拟机。虚拟机挂载 A 的任务空间为 D 盘、项目公共空间为 F 盘，进入运行状态后自动开始采集。
2. 用户或 Agent 在虚拟机中工作，工程文件保存在 D 盘。
3. 任务 A 完成，平台调用冻结接口，A 的任务空间变为只读。
4. 平台为任务 B 创建虚拟机，并指定 A 为上游。虚拟机挂载 B 的任务空间为 D 盘，A 的任务空间以只读方式挂载在 `E:\task_A`。
5. 任务 B 读取 A 的成果，继续工作。
6. 使用完毕后，平台停止或删除虚拟机，任务空间和采集结果都会保留。


## 1. 目标与边界

- 实现 `api.md` 中的 18 个接口。路径、字段、返回码都以 `api.md` 为准，不得增删或改名。实现中如果发现接口规范有问题，记录到 TEST_REPORT.md 并报告，不要自行修改 `api.md`。
- 运行环境：单台 Linux 宿主机，服务以 root 身份运行在宿主机上：单进程，单个 uvicorn worker，不使用 `--reload`。
- **v1 不做**：本文档未定义的功能，如 HarnessStudio 平台 / 外部组件实现的内容、快照、GPU、多宿主机等。
- **代码要简单通用、人类易读、模块职责清楚、方便后续迭代。不引入插件框架或通用平台。**

### 1.1 已验证的启动命令
 
下面是已验证可用的单机启动命令。复用它，只做第 9 节规定的修改，不研究镜像内部的启动机制：
 
```bash
docker run -it --rm \
    -p 8006:8006 \
    -p 3390:3389 \
    -p 5000:5000 \
    --name winarena \
    --platform linux/amd64 \
    --device=/dev/kvm \
    -e RAM_SIZE=8G -e CPU_CORES=4 \
    -v /data/hdd/ProSoftAgentArena/src/win-arena-container/vm/storage/.:/base_storage:ro \
    --privileged \
    --cap-add NET_ADMIN \
    --stop-timeout 120 \
    --entrypoint /bin/bash julyai/mywinarena:v1 \
    -c 'echo '"'"'1. Construct basic env...'"'"'; \
    rm -rf /storage; \
    mkdir -p /storage; \
    cp -n /base_storage/windows* /storage/; \
    rm -f /storage/windows*.vars; \
    qemu-img create -f qcow2 -F raw -b /base_storage/data.img /storage/data.qcow2; \
    echo '"'"'2. Starting VM...'"'"'; \
    nohup /usr/bin/tini -s /run/entry.sh > /dev/null 2>&1 & sleep 1; \
    echo '"'"'3. Applying iptables bypass rules...'"'"'; \
    iptables -t nat -I PREROUTING 1 -p tcp --dport 8000 -j RETURN; \
    iptables -t nat -I PREROUTING 1 -p tcp --dport 8080 -j RETURN; \
    iptables -t nat -I PREROUTING 1 -p tcp --dport 22 -j RETURN; \
    tail -f /dev/null'
```
 
### 1.2 外部组件与接入约定

| 组件 | 约定 |
| --- | --- |
| Docker 镜像 | `julyai/mywinarena:v1`，配置在 `environments.json` 中。镜像内的共享文件夹服务会把容器内的四个目录发布给 Windows：`/shared` → D 盘，`/upstream` → E 盘，`/project` → F 盘，`/recordings` → G 盘 |
| Windows 基础磁盘 | 两个基础磁盘都已预装软件，开机后会自动映射 D、E、F、G 四个盘符 |
| Agent 服务 | 虚拟机内的 Flask 服务，容器端口为 5000。提供 `GET /probe`，以及开始采集、停止采集两个接口（约定见 10.1） |
| 采集程序 | 由 Agent 服务启停，录制屏幕和键盘鼠标操作，结果写入指定的目录 |
 
- 本项目按上表的约定对接这些组件，假定它们可用，不测试组件本身。
- 不要自行开发或修改这些组件。
- 测试只验证到容器这一层：宿主机目录、容器挂载、读写权限、对 Agent 服务的调用；不验证 Windows 内部的盘符、软件和录像内容。
- 组件交付后，另行安排联调。


## 2. 技术栈

| 用途 | 选型 |
| --- | --- |
| 语言 | Python 3.10+ |
| Web 框架 | FastAPI + Pydantic v2 + uvicorn。Docker SDK 是同步调用，路由用普通 `def`，不阻塞事件循环|
| Docker 操作 | `docker`（Docker SDK for Python） |
| 数据库 | 标准库 `sqlite3`，开启 WAL |
| HTTP 探测 | `requests`，设置 `trust_env=False`，不继承环境代理 |
| 测试 | `pytest` |


## 3. 模块划分
 
```
vm-manager/
├── api.md                    # 接口规范（已提供，不要修改）
├── task.md                   # 本文档
├── main.py                   # 读取配置、启动前检查、恢复状态、启动巡检线程、启动服务
├── api.py                    # 18 个接口的路由、请求/响应模型、统一错误转换
├── vm_manager.py             # 虚拟机：校验、资源计算、FIFO 排队、巡检、启动/停止/删除、实例锁
├── workspace_manager.py      # 目录与任务空间：路径规则、创建、冻结/解冻、复制、挂载关联
├── recording_manager.py      # 轨迹采集：采集记录、启停、状态
├── backend.py                # 封装 Docker 和 Agent 服务的调用；测试时可替换为假实现
├── db.py                     # 建表、查询、条件更新
├── environments.json         # 软件环境（见 4.1）
├── settings.json             # 服务参数（见 4.2）
├── scripts/start_windows.sh  # 容器启动脚本（见 9.4）
├── tests/
├── requirements.txt
├── README.md                 # 运行命令、依赖组件的入口、目录权限、HarnessStudio 调用顺序、常见问题
└── TEST_REPORT.md            # 真实测试的条件、请求、结果和未完成项
```

路由层只做参数校验和结果转换，不直接执行 SQL，也不调用 Docker、操作文件；这些都交给对应的 manager 模块。不要把所有管理逻辑写进一个文件。

## 4. 配置

配置在服务启动时读取，修改后需重启才生效。每台虚拟机创建时保存一份配置副本，之后修改配置不影响已有的虚拟机。相对路径一律按项目根目录解析。

### 4.1 environments.json
 
```json
[
  {
    "environment_id": "win11-autocad2026",
    "os": "windows11",
    "software": [{"name": "AutoCAD", "version": "2026"}],
    "min_spec": {"cpu_cores": 4, "ram_gb": 8},
    "docker_image": "julyai/mywinarena:v1",
    "base_storage": "/data/hdd/ProSoftAgentArena/src/win-arena-container/vm/storage",
    "base_img": "data_v1_autocad_30G.img",
    "base_format": "raw",
    "probe_path": "/probe"
  },
  {
    "environment_id": "win11-autocad2026-solidworks2025-ansys2024r1",
    "os": "windows11",
    "software": [
      {"name": "AutoCAD", "version": "2026"},
      {"name": "SolidWorks", "version": "2025"},
      {"name": "ANSYS", "version": "2024 R1"}
    ],
    "min_spec": {"cpu_cores": 4, "ram_gb": 8},
    "docker_image": "julyai/mywinarena:v1",
    "base_storage": "/data/hdd/ProSoftAgentArena/src/win-arena-container/vm/storage",
    "base_img": "data.img",
    "base_format": "raw",
    "probe_path": "/probe"
  }
]
```

- `environment_id`、`os`、`software`、`min_spec` 四个字段原样用于「查询可用软件环境」接口；其余是内部字段，不对外返回。
- 文件名中的 `30G` 不作为任何判断依据。
- 容器内部端口固定为 VNC 8006、RDP 3389、Agent 5000，在代码中写成常量。
- **基础磁盘文件不得修改或替换。** 所有虚拟机的 C 盘都是基于它的增量，一旦修改，已有虚拟机的 C 盘就会损坏。更新软件环境时，要新建磁盘文件，并新增一个环境条目。

### 4.2 settings.json
 
```json
{
  "listen_host": "0.0.0.0",
  "listen_port": 8000,
  "access_host": "REPLACE_WITH_HOST_LAN_IP",
  "data_root": "/data/hdd/vm_manager",
  "host_reserved_cpu": 2,
  "host_reserved_ram_gb": 4,
  "vm_overhead_ram_gb": 1,
  "min_free_disk_gb": 50,
  "default_timeout_minutes": 120,
  "default_queue_timeout_minutes": 30,
  "boot_timeout_seconds": 900,
  "poll_interval_seconds": 2,
  "probe_timeout_seconds": 3,
  "health_failure_threshold": 3,
  "stop_timeout_seconds": 120,
  "recorder_start_path": "/recording/start",
  "recorder_stop_path": "/recording/stop",
  "recorder_timeout_seconds": 60
}
```
 
| 字段 | 说明 |
| --- | --- |
| `access_host` | 接口返回的 `access.host`，必须是调用方能访问到的宿主机 IP 或域名 |
| `data_root` | 数据根目录，存放数据库 `vmm.db` 和 `projects/` 目录（见第 5 节） |
| `min_free_disk_gb` | `data_root` 所在的磁盘，以及 Docker 数据目录所在的磁盘，各自至少要保留的空间 |
| `recorder_*` | 虚拟机内采集接口的路径和超时时间（见 10.1） |
 
- `vm_overhead_ram_gb`、`default_timeout_minutes`、`default_queue_timeout_minutes`、`boot_timeout_seconds`、`health_failure_threshold` 这几项的值在 `api.md` 中也有写明。正式部署时必须与 `api.md` 保持一致，只有测试时可以临时修改。
- 所有 `_gb` 字段都按 GiB（1024³ 字节）计算。

### 4.3 启动前检查
 
以下任一项不满足，服务都会启动失败，并给出明确的错误信息：
 
1. `environment_id` 不重复；`base_img` 只能是文件名，不能包含 `/` 或 `..`。
2. `access_host` 不是占位值。
3. `/dev/kvm` 存在，Docker 可以连接，所有环境用到的镜像都已在本地。服务运行期间不拉取镜像。
4. 每个环境的 `base_img` 文件都存在。
5. `data_root` 可写，且所在的文件系统支持 `chattr +i`。检查方法：创建一个临时文件，加上 `+i` 后尝试写入，应当失败；之后执行 `-i`，再删除该文件。
6. 宿主机上有 `qemu-img` 时，用 `qemu-img info --output=json` 只读检查 `base_img` 的实际格式，必须与 `base_format` 一致；没有 `qemu-img` 时跳过此项，并记录一条 warning。


## 5. 宿主机目录与挂载
 
### 5.1 目录结构
 
```
<data_root>/
├── vmm.db                                  本服务的数据库，不挂载给任何虚拟机
└── projects/<project_id>/
    ├── shared/                             项目公共空间
    ├── tasks/<task_id>/                    任务空间
    └── recordings/<vm_id>/<recording_id>/  采集结果
```
 
- `project_id`、`task_id` 直接用作目录名，必须匹配 `^[A-Za-z0-9_-]{1,64}$`。这项校验在请求模型中完成，不匹配时返回 422 VALIDATION_ERROR。拼出完整路径后，还要用 `os.path.realpath` 确认它仍然位于 `data_root/projects/` 之下。
- 目录按需自动创建（`os.makedirs(..., exist_ok=True)`），时机如下：
  - 创建虚拟机时：项目的 `shared/`、本任务的 `tasks/<task_id>/`（该任务还没有任务空间时）、`recordings/<vm_id>/`；
  - 创建任务空间时：`tasks/<task_id>/`；
  - 开始采集时：`recordings/<vm_id>/<recording_id>/`。
- 服务从不删除 `projects/` 下的任何内容。删除虚拟机时，任务空间、项目公共空间、采集结果都保留。

### 5.2 容器挂载
 
| 宿主机目录 | 容器内路径 | 模式 | Windows 中 |
| --- | --- | --- | --- |
| 环境的 `base_storage` | `/base_storage` | 只读 | — |
| `projects/<pid>/tasks/<tid>/`（本任务） | `/shared` | 读写 | D 盘 |
| `projects/<pid>/tasks/<上游 tid>/`（每个上游一项） | `/upstream/<上游 tid>` | 只读 | `E:\<上游 tid>` |
| `projects/<pid>/shared/` | `/project` | 读写 | F 盘 |
| `projects/<pid>/recordings/<vm_id>/` | `/recordings` | 读写 | G 盘 |
| `scripts/start_windows.sh` | `/opt/vm-manager/start_windows.sh` | 只读 | — |
 
- 每台虚拟机只挂载与自己相关的目录：本任务、声明的上游、所属项目的公共空间、自己的采集目录。**不得挂载 `data_root` 或 `projects/` 这样的上级目录。**
- 上游任务必须与本任务属于同一个项目。
- C 盘的增量盘 `/storage` 不挂载宿主机目录，它保存在容器内部：停止时随容器保留，删除容器时一起删除。
- 挂载在创建容器时就确定了，之后不再变化。重新启动时，沿用原来的容器和挂载。

## 6. 任务空间
 
### 6.1 创建
 
- 每个任务最多一个任务空间，以 `(project_id, task_id)` 唯一标识，数据库中加唯一约束。
- 创建任务空间、创建虚拟机时自动创建、复制到目标任务，这三种操作都要先拿到该 `(project_id, task_id)` 的创建锁（8.2），防止重复创建。
- 显式调用创建接口时，如果已有记录，返回 409 WORKSPACE_EXISTS。创建虚拟机时，有记录就直接复用，没有就自动创建。

### 6.2 冻结与解冻
 
- **冻结**：先在数据库中写入 `pending_op=freeze`，再执行 `chattr -R +i <任务空间目录>`。成功后，把状态改为 `frozen`，写入 `frozen_at`，并清空 `pending_op`。所有挂载了这个目录的容器，会立即变为只读。
- **解冻**：先在数据库中写入 `pending_op=unfreeze`，再执行 `chattr -R -i <任务空间目录>`。成功后，把状态改为 `writable`，`frozen_at` 置为 null，并清空 `pending_op`。作为上游的挂载本来就是只读的，不受解冻影响。
- 冻结、解冻前，都要先拿到该任务空间的锁（8.2）。
- `chattr` 执行失败时，保留 `pending_op` 和原来的状态，返回 500 INTERNAL_ERROR。之后，巡检线程和服务重启恢复会按 `pending_op` 继续执行，直到成功（12.1、13）。冻结和解冻都可以重复执行：即使目标状态与当前状态相同，也要再执行一次 `chattr`。新的冻结或解冻请求会覆盖 `pending_op`，以新请求为准。
- **`pending_op` 不为空时，这个空间一律视为未冻结**：作为复制的源空间时，返回 409 WORKSPACE_NOT_FROZEN；作为上游时，返回 409 UPSTREAM_NOT_FROZEN。这样，做到一半的冻结或解冻，不会被当作可靠的只读成果使用。
- 冻结有以下边界，需要在 README.md 中说明，不需要代码处理：
  - 冻结只作用于已经写到磁盘上的文件，不会替软件保存未保存的内容。平台应先让用户或 Agent 保存文件，再调用冻结。
  - 文件已经被打开时，冻结后也必须不能再写入。容器层（内核）的行为在 14.2 中验证，这是测试的通过条件；经过 Samba 时的行为，联调时验证。
  - E 盘挂载的是上游目录本身，不是一份快照：上游被解冻并修改后，下游能看到新的内容。是否允许解冻一个已被下游引用的空间，由平台决定，本服务不做判断。

### 6.3 复制
 
按以下步骤同步执行：
 
1. 先拿目标 `(project_id, task_id)` 的创建锁，再拿源任务空间的锁。这个顺序与 8.2 一致。
2. 检查：源空间存在，否则返回 404 WORKSPACE_NOT_FOUND；源空间已冻结、且 `pending_op` 为空，否则返回 409 WORKSPACE_NOT_FROZEN；目标任务没有任务空间记录，且目标目录不存在，否则返回 409 WORKSPACE_EXISTS。
3. 执行 `cp -a --reflink=auto <源目录>/. <临时目录>/`。临时目录放在目标项目的 `tasks/` 下，命名为 `.<目标 task_id>.copying-<随机串>`。`cp` 不会复制 `chattr` 属性，所以新目录是可写的。不能用硬链接代替复制。
4. 复制完成后，用 `rename` 把临时目录改名为 `tasks/<目标 task_id>/`，然后写入数据库记录，状态为 `writable`，并记录 `source_workspace_id`。**如果改名成功、但写数据库失败，要删除刚改名的目标目录**。否则这个目录会一直留着，之后对这个任务的复制或创建都会返回 WORKSPACE_EXISTS。
5. 任何一步失败，都删除临时目录，返回 500 INTERNAL_ERROR，不留下半成品。复制过程自己产生的临时目录和目标目录，不受 5.1「不删除 `projects/` 下任何内容」的限制。

只复制任务空间目录的内容，不复制项目公共空间、采集结果或任何数据库记录。源空间保持不变，仍处于冻结状态。

如果服务在改名之后、写数据库之前崩溃，留下的目录由服务重启恢复处理（13）。
 
### 6.4 挂载关联
 
`mounted_by` 不单独存储，查询时根据 vms 表计算，包含两类虚拟机：
 
- 以它作为本任务空间的虚拟机，路径为 `D:`；
- 在 `upstreams` 中引用了它的虚拟机，路径为 `E:\<task_id>`。
两类都包括所有未删除的虚拟机，排队中、已停止的也算在内。它表示的是挂载配置，不代表虚拟机正在运行。

## 7. 数据库
 
使用三张表。数据库是唯一的状态来源；内存中只保存锁和工作线程。
 
**vms**（删除虚拟机时，整行删除）
 
| 字段 | 说明 |
| --- | --- |
| seq INTEGER PK AUTOINCREMENT | 内部序号，列表按它倒序排列 |
| vm_id TEXT UNIQUE | `vm_` 加 8 位小写十六进制 |
| project_id、task_id、chat_id | 关联信息；chat_id 可以为 null |
| environment_id、environment_json | 软件环境 ID，以及该环境配置的完整副本 |
| cpu_cores、ram_gb、expose、timeout_minutes、queue_timeout_minutes | 请求内容；`expose` 存为 JSON |
| workspace_id | 本任务的任务空间 |
| upstreams | 上游列表，JSON 格式：`[{"task_id":..,"workspace_id":..}]` |
| status | 8 种状态之一 |
| container_id、ports | 容器 ID；宿主机端口，JSON 格式：`{"vnc":..,"rdp":..,"agent":..}` |
| reserved INTEGER | 1 表示正在占用 CPU 和内存。从 queued 变为 creating 时置 1，**确认容器已停止后**才置 0 |
| health_failures INTEGER | running 状态下连续检查失败的次数 |
| ever_started INTEGER | 虚拟机第一次进入 starting 时置 1，之后不再改变。用于区分首次创建和重新启动（8.1、13） |
| queue_order INTEGER | 入队顺序。每次进入排队（创建或启动）时，取当前最大值加 1。FIFO 以它为准 |
| queue_deadline、queue_reason | 本次排队的截止时间（入队时间 + `queue_timeout_minutes`）；队首最近一次的等待原因 |
| created_at、started_at、expires_at、stopped_at | `created_at` 在重新启动时不变；其余三个按每次运行重新计算 |
| stop_reason | manual / cancelled / expired / failure |
| error_code、error_message | 异步失败的原因；完整的异常信息同时写入日志 |
| last_retry_at | 补做中断操作的上次尝试时间，用于控制重试间隔（12.1 第 6 步） |
 
**workspaces**
 
| 字段 | 说明 |
| --- | --- |
| workspace_id TEXT PK | `ws_` 加 8 位小写十六进制 |
| project_id、task_id | 关联信息，`UNIQUE(project_id, task_id)` |
| status | `writable` 或 `frozen` |
| pending_op | 未完成的权限操作：`freeze`、`unfreeze` 或 null（6.2） |
| path | 宿主机目录的绝对路径 |
| source_workspace_id | 复制来源；不是复制得到的，则为 null |
| created_at、frozen_at | 创建时间、冻结时间 |
| last_retry_at | 补做 `pending_op` 的上次尝试时间，用于控制重试间隔（12.1 第 6 步） |
 
**recordings**（删除虚拟机时不删除）
 
| 字段 | 说明 |
| --- | --- |
| recording_id TEXT PK | `rec_` 加 8 位小写十六进制 |
| vm_id、project_id、task_id、chat_id | 关联信息 |
| status | `recording` / `stopped` / `failed` |
| confirmed INTEGER | 开始接口是否已返回 200：0 为未确认，1 为已确认（10.2）。未确认的记录，对外状态仍为 `recording` |
| start_attempts INTEGER | 调用开始接口失败的累计次数 |
| started_at、stopped_at | 开始和停止的时间 |
| result_path | 宿主机目录的绝对路径 |
| error | 失败原因 |
 
- **所有状态变更都用条件更新（CAS）**：`UPDATE ... WHERE id=? AND status IN (...)`，并检查受影响的行数。
- 每个线程使用独立的数据库连接，事务要短。**不要在持有事务时等待 Docker、网络或文件复制。**
- 生成 ID 时，要检查它在表中是否已存在。时间统一使用 UTC ISO 8601，精确到秒，例如 `2026-10-11T10:00:00Z`。


## 8. 虚拟机状态与调度
 
### 8.1 状态流转
 
| 从 | 到 | 触发条件 | 执行方 |
| --- | --- | --- | --- |
| （新建） | queued | 创建请求通过全部校验 | 创建接口 |
| stopped | queued | 启动请求。清空 `started_at`、`expires_at`、`stopped_at`、`stop_reason`、`health_failures`，重新计算 `queue_order` 和 `queue_deadline`，排到队尾 | 启动接口 |
| queued | creating | 轮到队首，且通过准入检查（8.3）；预留资源，写入 `started_at`、`expires_at` | 巡检线程 |
| queued | failed | 超过 `queue_deadline`（QUEUE_TIMEOUT），或前提条件已失效（HOST_NOT_READY） | 巡检线程 |
| queued | stopped | 停止接口取消排队（cancelled） | 停止接口 |
| queued | （删除记录） | 删除请求 | 删除接口 |
| creating | starting | 容器启动成功，端口已读出；第一次进入 starting 时，置 `ever_started=1` | 巡检线程 |
| creating | failed | 创建或启动容器失败（PROVISION_FAILED） | 巡检线程 |
| starting | running | 健康检查通过（12.2），同时自动开始采集 | 巡检线程 |
| starting | failed | 超过 `started_at + boot_timeout_seconds`（STARTUP_TIMEOUT），或容器已退出（CONTAINER_EXITED） | 巡检线程 |
| running | failed | 容器已退出（CONTAINER_EXITED），或连续检查失败达到阈值（HEALTH_CHECK_FAILED） | 巡检线程 |
| starting / running | stopping → stopped | 手动停止（manual），或到达 `expires_at`（expired） | 停止接口 / 巡检线程 |
| 非 creating 状态 | deleting → （删除记录） | 删除请求 | 删除接口 |
 
- **failed 状态下的容器处理**：
  - STARTUP_TIMEOUT、HEALTH_CHECK_FAILED：停止容器但不删除，方便用 `docker logs` 排查，设置 `stop_reason=failure`。停止确认之前，`reserved` 保持为 1。
  - PROVISION_FAILED：
    - `ever_started=0`（首次创建）：删除已创建的容器（如果有），释放资源；
    - `ever_started=1`（重新启动时启动容器失败）：保留容器和 C 盘，确认容器已停止后释放资源。
  - 从未启动过的失败虚拟机，`stop_reason` 和 `stopped_at` 都为 null。
- 离开 running 时（停止、删除、到期、失败），都要先按第 10 节结束正在进行的采集。
- 只操作带本服务 label 的容器，**绝不触碰**已有的 `winarena` 容器或其他容器。

### 8.2 锁
 
所有锁都是进程内的锁（`threading.Lock` 保护的集合或字典），不持久化。
 
| 锁 | 粒度 | 保护的操作 | 拿不到时 |
| --- | --- | --- | --- |
| 实例锁 | 每台虚拟机 | 创建、启动容器、停止、删除、自动停止、开始采集、停止采集 | 生命周期接口返回 409 VM_BUSY；手动开始采集返回 409 VM_NOT_RUNNING；自动开始采集、手动停止采集等待获取；巡检线程跳过，下一轮再试 |
| 任务空间锁 | 每个任务空间 | 冻结、解冻、作为复制的源空间 | 等待 |
| 创建锁 | 每个 `(project_id, task_id)` | 创建任务空间、自动创建、作为复制的目标 | 等待 |
| 调度锁 | 全局 | 领取队首和预留资源 | 等待 |
 
- 实例锁实现为每台虚拟机一个 `threading.Lock`，同时支持尝试获取（`acquire(blocking=False)`）和等待获取两种用法。
- 停止、删除接口在以下情况返回 VM_BUSY：状态为 creating，或者拿不到实例锁。启动接口按 11.3 处理：状态为 queued、creating、starting、running 时直接返回当前状态，只有 stopped 需要启动时才获取实例锁。采集接口的处理见 10.2、10.3。
- 状态为 stopping 或 deleting、但实例锁空闲，说明上次操作失败或被中断，此时允许重新执行。
- 停止、删除、到期停止、故障停止这几个流程本身已持有实例锁，在流程内直接结束采集，不要重复加锁。
- 需要同时拿多把锁时，按「创建锁 → 任务空间锁 → 实例锁」的顺序获取。
- 调度锁只在领取队首、预留资源的那一小段时间内持有。持有任何锁时，都不能同时持有数据库事务，去等待 Docker、网络或文件复制。

### 8.3 资源计算与准入检查
 
| 项目 | 计算方式 |
| --- | --- |
| CPU 预算 | 逻辑 CPU 数 − `host_reserved_cpu` |
| 内存预算 | 物理内存 − `host_reserved_ram_gb` |
| 单台占用 | CPU = `cpu_cores`；内存 = `ram_gb + vm_overhead_ram_gb` |
| `cpu.total`、`memory_gb.total` | 宿主机物理总量 |
| `cpu.allocated`、`memory_gb.allocated` | 所有 `reserved=1` 记录的占用之和 |
| `cpu.available` | `max(0, CPU 预算 − cpu.allocated)` |
| `memory_gb.available` | `max(0, min(内存预算 − memory_gb.allocated, MemAvailable − host_reserved_ram_gb))`，向下取整。`MemAvailable` 取自 `/proc/meminfo` |
| `disk_gb.*` | `data_root` 所在的文件系统，用 `shutil.disk_usage` 读取，向下取整；`used = total − free` |
| `vms.running` | `reserved=1` 的记录数 |
| `vms.queued` | queued 状态的记录数 |
 
准入检查只针对队首：
 
| 情况 | 处理 |
| --- | --- |
| KVM、镜像或基础磁盘已不可用 | 队首直接变为 failed（HOST_NOT_READY），不阻塞后面的队列 |
| Docker 无法连接 | 继续排队，`queue_reason=docker_unavailable` |
| 出现以下任一情况：`cpu_cores` > `cpu.available`；`ram_gb + overhead` > `memory_gb.available`；`data_root` 或 Docker 数据目录所在磁盘的剩余空间 < `min_free_disk_gb` | 继续排队，`queue_reason=insufficient_resources` |
| 以上都没有问题 | 领取，开始创建或启动 |
 
- 严格 FIFO：队首放不下时，后面的请求即使放得下，也不能先启动。
- 接口返回的 `queue.reason`：队首返回 `queue_reason` 的值，其他排队项一律返回 `waiting_turn`。
- Docker 数据目录通过 `client.info()["DockerRootDir"]` 获取。

## 9. 容器与启动脚本
 
### 9.1 创建与启动容器
 
巡检线程领取队首后，分两种情况处理：
 
- **还没有容器**（新虚拟机，或排队中被取消、从未启动过的虚拟机）：
  1. 调用 `containers.create(...)`，**立即**把 `container_id` 写入数据库；
  2. 调用 `start()`；
  3. 调用 `reload()`，从 `NetworkSettings.Ports` 读出宿主机端口，转成整数后写入数据库；
  4. 状态变为 starting。
- **已有容器**（重新启动）：调用 `start()` 和 `reload()`，重新读取端口并写入数据库，状态变为 starting。端口可能与上次不同。
不要用 shell 拼接 `docker run` 命令。
 
### 9.2 参数对照
 
1.1 命令中的每一项，按下表对应到 SDK 参数：
 
| 原命令 | SDK 参数 | 说明 |
| --- | --- | --- |
| `julyai/mywinarena:v1` | 环境的 `docker_image` | 使用派生镜像 |
| `-it --rm` | 不使用 | 后台运行；容器由删除接口负责删除 |
| `--name winarena` | `name=f"vm-{vm_id}"` | |
| `-p 8006:8006 -p 3390:3389 -p 5000:5000` | `ports={"5000/tcp": None, "8006/tcp": None, "3389/tcp": None}` | 宿主机端口由 Docker 分配。5000 始终发布；8006 和 3389 根据 `expose` 决定是否发布 |
| `--platform linux/amd64` | `platform="linux/amd64"` | |
| `--device=/dev/kvm` | `devices=["/dev/kvm:/dev/kvm:rwm"]` | |
| `-e RAM_SIZE=8G -e CPU_CORES=4` | `environment={"RAM_SIZE": f"{ram_gb}G", "CPU_CORES": str(cpu_cores), "BASE_IMG": ..., "BASE_FORMAT": ...}` | 规格取自请求，`BASE_IMG`、`BASE_FORMAT` 取自环境配置 |
| `-v .../vm/storage/.:/base_storage:ro` | 按 5.2 的挂载表生成 `volumes` | |
| `--privileged --cap-add NET_ADMIN` | `privileged=True`，`cap_add=["NET_ADMIN"]` | |
| `--stop-timeout 120` | 停止时调用 `stop(timeout=stop_timeout_seconds)` | |
| `--entrypoint /bin/bash ... -c '...'` | `entrypoint="/bin/bash"`，`command=["/opt/vm-manager/start_windows.sh"]` | 脚本内容见 9.4 |
| （新增） | `labels={"vmm.managed": "true", "vmm.vm_id": vm_id}` | 用于识别本服务创建的容器 |
| （新增） | `nano_cpus=cpu_cores * 10**9`；`mem_limit` 和 `memswap_limit` 都设为 `(ram_gb + overhead)` GiB | 限制单台虚拟机的资源，且不允许用 swap 扩容 |
 
- 不设置 auto_remove，也不设置自动重启。
- Docker 客户端：`docker.from_env(timeout=180)`，超时时间要大于停止等待时间。

### 9.3 停止与删除容器
 
- **停止**：调用 `stop(timeout=stop_timeout_seconds)`，再用 `reload()` 确认容器已经退出，才算停止成功。
- **删除**：一律调用 `remove(v=True)`，同时删除容器的匿名卷。镜像可能把 `/storage` 声明为卷，不带 `v=True` 会遗留 C 盘的磁盘。

### 9.4 启动脚本 scripts/start_windows.sh
 
```bash
echo '1. Construct basic env...'
if [ -f /storage/data.qcow2 ]; then
  echo 'Existing disk found, skip initialization.'
else
  rm -rf /storage
  mkdir -p /storage
  cp -n /base_storage/windows* /storage/ || { echo 'ERROR: failed to copy windows* files'; exit 1; }
  rm -f /storage/windows*.vars
  qemu-img create -f qcow2 -F "$BASE_FORMAT" -b "/base_storage/$BASE_IMG" /storage/data.qcow2 \
    || { echo 'ERROR: failed to create data.qcow2'; exit 1; }
fi
echo '2. Starting VM...'
/usr/bin/tini -s /run/entry.sh &
pid=$!
trap 'kill -TERM "$pid" 2>/dev/null' TERM INT
sleep 1
echo '3. Applying iptables bypass rules...'
iptables -t nat -I PREROUTING 1 -p tcp --dport 8000 -j RETURN
iptables -t nat -I PREROUTING 1 -p tcp --dport 8080 -j RETURN
iptables -t nat -I PREROUTING 1 -p tcp --dport 22 -j RETURN
while kill -0 "$pid" 2>/dev/null; do wait "$pid"; done
```
 
与 1.1 原命令相比，有以下不同：
 
- **增量盘已存在时，跳过初始化。** 这样重新启动后，C 盘会保留上次的内容。
- **基础磁盘由环境变量指定。** 使用 `BASE_IMG`、`BASE_FORMAT`，不再写死为 `data.img`。
- **初始化失败时立即退出。** 复制启动文件或创建增量盘失败时，不再继续启动虚拟机。`rm -rf /storage` 的退出码不检查，因为 `/storage` 可能是挂载点，目录本身删不掉。
- **保留输出。** 输出不再重定向到 `/dev/null`，会进入 `docker logs`。
- **等待 tini 进程并转发停止信号。** 虚拟机进程退出后，容器随之退出；停止容器时，Windows 可以正常关机。
如果实测发现容器启动后很快就自行退出，说明 `entry.sh` 不会常驻前台。这时把最后一行改为 `tail -f /dev/null`，并在 TEST_REPORT.md 中记录。

## 10. 轨迹采集
 
### 10.1 Agent 服务的采集接口约定
 
请求地址为 `http://127.0.0.1:<agent 端口>`，超时为 `recorder_timeout_seconds`。
 
| 操作 | 请求 | 成功的判定 |
| --- | --- | --- |
| 开始 | `POST {recorder_start_path}`，请求体 `{"recording_id": "rec_xxx", "output_dir": "G:\\rec_xxx", "project_id": "...", "task_id": "...", "vm_id": "...", "chat_id": "..."}` | HTTP 200。对同一个 `recording_id` 重复调用，不会重复启动 |
| 停止 | `POST {recorder_stop_path}`，请求体 `{"recording_id": "rec_xxx"}` | HTTP 200，表示采集进程已经结束，结果文件已经写完。重复调用不出错 |
 
### 10.2 开始采集
 
自动开始和手动开始，共用同一个函数：
 
1. 拿到实例锁：自动开始时等待获取；手动开始时，拿不到锁就返回 409 VM_NOT_RUNNING。拿到锁后，重新读取虚拟机的状态，不是 running 就不开始，手动开始时返回 409 VM_NOT_RUNNING。
2. 这台虚拟机已有 `recording` 状态的记录时，直接返回该记录，不修改它的 `chat_id`。如果这条记录还没确认（`confirmed=0`），先用它的 `recording_id` 再调用一次开始接口，结果按第 4、5 步处理。
3. 否则生成 `recording_id`，创建 `recordings/<vm_id>/<recording_id>/` 目录，写入一条记录（状态为 `recording`，`confirmed=0`），然后调用开始接口。
4. 调用成功：置 `confirmed=1`。
5. 调用失败或超时：`start_attempts` 加 1，记录保持 `recording` 状态，**不改为 failed**。超时不代表采集程序没有启动，所以之后只能用同一个 `recording_id` 重试（10.1 约定了重复调用不会重复启动），由巡检线程负责（12.1）。
   - 手动开始：返回 500 RECORDING_START_FAILED；
   - 自动开始：只记录日志，虚拟机保持 running；
   - 累计失败 3 次后：先用同一个 `recording_id` 调用一次停止接口，确保没有遗留的采集进程，再把记录改为 failed，并写入 `error`。

新记录的 `chat_id`：请求中传了就用请求中的值，否则使用虚拟机的 `chat_id`。
 
### 10.3 结束采集
 
手动停止，以及离开 running 时的自动结束，共用同一个函数：
 
1. 拿到实例锁：手动停止时，等待获取。自动结束发生在已经持有实例锁的停止、删除、到期、故障流程中，直接执行，不再加锁。然后重新读取记录，已经是 stopped 或 failed 的，原样返回。
2. 找到 `recording` 状态的记录（无论是否已确认），用它的 `recording_id` 调用停止接口。
3. 成功：改为 stopped，写入 `stopped_at`。
4. 失败：
   - **手动停止**：返回 500 RECORDING_STOP_FAILED，记录保持 `recording` 状态，可以重试。因为记录仍是 `recording`，再次开始采集时会返回这条记录，不会开出第二份。
   - **自动结束**：把记录改为 failed，写入 `error`，然后继续停止虚拟机。不能因为采集失败而阻塞虚拟机的停止或删除。
5. 容器已经退出（CONTAINER_EXITED）时，不调用停止接口，直接改为 failed，`error` 写「虚拟机已退出，采集未正常结束」。已经产生的部分文件保留。

### 10.4 自动采集的规则
 
- 自动开始只在 starting → running 时触发一次，在后台线程中执行。
- 手动停止采集后，本次运行不会再自动开始；手动开始，或者虚拟机下一次启动，才会产生新的记录。
- 每台虚拟机同一时间最多只有一条 `recording` 状态的记录。

## 11. 接口实现要点
 
### 11.1 通用
 
- 成功时返回 HTTP 200，响应体为 `{"data": ...}`。
- 失败时抛出 `ApiError(http_status, code, message, details)`，由全局异常处理器转换为 `{"error": {"code", "message", "details"}}`。
- Pydantic 校验错误（包括请求体不是合法 JSON），一律返回 422 VALIDATION_ERROR，在 `details.fields` 中列出每个出错字段的路径和原因。
- 请求模型的每一层都设置 `extra="forbid"`。整数用 `StrictInt`，布尔值用 `StrictBool`，字符串用 `StrictStr`。
- 未捕获的异常返回 500 INTERNAL_ERROR，完整堆栈只写入日志。
- 列表接口的多个筛选条件之间是「且」的关系。虚拟机列表的 `status` 参数，每一项都必须是 8 种状态之一。
### 11.2 软件环境与宿主机资源
 
- **查询可用软件环境**：返回 `environments.json` 中的四个对外字段（见 4.1）。
- **查询宿主机资源**：按 8.3 计算。

### 11.3 虚拟机
 
**创建**：按以下顺序校验，遇到第一个错误即返回。**校验失败时，不写数据库，也不创建任何目录。**
 
| 顺序 | 检查项 | 错误 |
| --- | --- | --- |
| 1 | 字段、类型、格式；`upstream_task_ids` 不能重复，也不能包含本任务 | 422 VALIDATION_ERROR |
| 2 | `environment_id` 是否存在 | 422 ENVIRONMENT_NOT_FOUND |
| 3 | 是否满足该环境的最低配置 | 422 BELOW_ENVIRONMENT_MINIMUM |
| 4 | 单台占用是否超过预算 | 422 REQUEST_EXCEEDS_CAPACITY |
| 5 | 每个上游任务在同一项目下都有任务空间 | 422 UPSTREAM_NOT_FOUND |
| 6 | 每个上游任务空间都已冻结，且没有未完成的权限操作（`pending_op` 为空，见 6.2） | 409 UPSTREAM_NOT_FROZEN |
| 7 | Docker 能否连接（`client.ping()`） | 503 DOCKER_UNAVAILABLE |
| 8 | KVM、镜像、基础磁盘是否可用 | 503 HOST_NOT_READY |
 
全部通过后，依次执行：
 
1. 按 5.1 创建目录。本任务还没有任务空间时，按 6.1 创建。
2. 写入一条 queued 记录，在同一个事务中计算 `queue_position`。
3. 唤醒巡检线程，返回结果。
本任务的任务空间已冻结时，仍然允许创建虚拟机，此时 D 盘是只读的。
 
**列表**：只返回 `api.md` 中列出的摘要字段，按 `seq` 倒序排列。
 
**详情**：
 
- `os`、`software` 取自 `environment_json`；
- `mounts` 根据 `workspace_id`、`upstreams` 和目录规则生成，`writable` 取本任务空间当前的状态；
- `current_recording_id` 取这台虚拟机 `recording` 状态的记录。
**启动**：
 
| 状态 | 处理 |
| --- | --- |
| stopped | 检查 Docker 能否连接（不能则返回 503 DOCKER_UNAVAILABLE），以及 KVM、镜像、基础磁盘是否可用（不可用则返回 503 HOST_NOT_READY）。然后 CAS 改为 queued（8.1），并唤醒巡检线程 |
| queued / creating / starting / running | 原样返回当前状态 |
| stopping / deleting，或拿不到实例锁 | 409 VM_BUSY |
| failed | 409 VM_FAILED |
 
**停止**：
 
| 状态 | 处理 |
| --- | --- |
| 不存在 | 404 VM_NOT_FOUND |
| 正忙（8.2），或为 deleting | 409 VM_BUSY |
| queued | CAS 改为 stopped，`stop_reason=cancelled`，写入 `stopped_at`。不依赖 Docker |
| starting / running / stopping（锁空闲） | Docker 不可用时，返回 503。否则依次执行：<br>1. CAS 改为 stopping，写入 `stop_reason=manual`；<br>2. 结束采集；<br>3. 停止容器。<br>成功：改为 stopped，`reserved=0`，写入 `stopped_at`。<br>失败：返回 500 STOP_FAILED，状态保持 stopping |
| stopped | 原样返回 |
| failed | 若 `reserved=1`（容器还没确认停止），先补做一次停止，再返回当前状态 |
 
**删除**：
 
| 状态 | 处理 |
| --- | --- |
| 不存在 | 200，返回 `deleted` |
| 正忙（8.2） | 409 VM_BUSY |
| queued，且从未创建过容器 | 用带 `status='queued'` 条件的 DELETE 删除记录。不依赖 Docker |
| 其他状态（锁空闲） | 有容器且 Docker 不可用时，返回 503。否则依次执行：<br>1. CAS 改为 deleting；<br>2. 结束采集；<br>3. 停止容器；<br>4. `remove(v=True)`；<br>5. 删除记录。<br>全部成功：返回 `deleted`。<br>任一步失败：返回 500 DELETE_FAILED，状态保持 deleting |
 
重试时，如果容器已经不存在，就视为已删除。删除虚拟机时，不删除任何目录，也不删除采集记录。
 
### 11.4 任务空间
 
| 接口 | 实现 |
| --- | --- |
| 列表 | 只返回 `api.md` 中列出的摘要字段，按 `created_at` 倒序排列 |
| 详情 | `mounted_by` 按 6.4 计算 |
| 创建 | 按 6.1 实现 |
| 冻结、解冻 | 按 6.2 实现 |
| 复制 | 按 6.3 实现 |
 
### 11.5 轨迹采集
 
| 接口 | 实现 |
| --- | --- |
| 启动 | 虚拟机不存在：404 VM_NOT_FOUND。否则按 10.2 执行；虚拟机不是 running，或实例锁正被占用时，返回 409 VM_NOT_RUNNING |
| 列表、详情 | 按 `started_at` 倒序排列；采集记录不存在时，返回 404 RECORDING_NOT_FOUND |
| 停止 | 采集记录不存在：404 RECORDING_NOT_FOUND。否则按 10.3 执行：等待获取实例锁后，重新读取记录。已经是 stopped 或 failed 的，原样返回；容器已经退出的，改为 failed；其他情况，调用停止接口 |

## 12. 巡检线程
 
### 12.1 每一轮的处理顺序
 
一个后台线程，每隔 `poll_interval_seconds` 执行一轮；收到创建、启动、停止、删除请求时会被唤醒，立即执行一轮。每一轮按以下顺序处理：
 
1. **列出容器**：按 label 列出本服务的全部容器和运行状态。Docker 无法连接时，跳过下面所有依赖 Docker 的步骤，既不判定为检查失败，也不释放资源。
2. **排队超时**：已超过 `queue_deadline` 的 queued 记录，改为 failed（QUEUE_TIMEOUT）。
3. **到期停止**：已超过 `expires_at` 的 starting 或 running 记录，拿到实例锁后，在后台线程中先结束采集、再停止容器，`stop_reason=expired`。
4. **健康检查**：逐台检查 starting 和 running 状态的虚拟机（12.2）。
5. **更新状态**：
   - starting：检查通过 → running，并在后台线程中自动开始采集；超过启动截止时间 → failed（STARTUP_TIMEOUT）。
   - running：检查失败时，`health_failures` 加 1，达到阈值 → failed（HEALTH_CHECK_FAILED）；成功一次就清零。
   - 容器已退出 → failed（CONTAINER_EXITED）。
   - 新变为 failed、且容器仍在运行的，在后台线程中先结束采集，再停止容器（不删除）。
6. **补做中断的操作**：
   - 实例锁空闲的 stopping、deleting 记录，以及 `reserved=1` 的 failed 记录，在后台线程中按原来的意图继续执行；
   - `pending_op` 不为空的任务空间，按 6.2 继续执行；
   - 所属虚拟机为 running、状态为 `recording` 且 `confirmed=0` 的采集记录，用同一个 `recording_id` 重试开始接口（10.2），每轮最多重试一次。

   虚拟机和任务空间的同一条记录失败后，距 `last_retry_at` 至少 60 秒才重试。
7. **调度队首**：取 `queue_order` 最小的 queued 记录，做准入检查（8.3），并把结果写入 `queue_reason`。通过后，在调度锁内拿到实例锁，CAS 改为 creating，置 `reserved=1`，写入 `started_at` 和 `expires_at`；然后释放调度锁，按 9.1 执行。**每轮最多启动一台。**
后台的停止、删除、采集，使用 `ThreadPoolExecutor` 执行，每台虚拟机同一时间最多有一个进行中的操作。巡检线程中的任何异常都只记录日志，不能让循环退出。
 
### 12.2 健康检查
 
以下各项全部通过才算成功：
 
- `GET http://127.0.0.1:<agent 端口><probe_path>` 返回 HTTP 200，且 JSON 中的 `status` 等于 `"Probe successful"`；
- 开放了 VNC 时，请求 VNC 端口，返回 HTTP 2xx 或 3xx；
- 开放了 RDP 时，RDP 端口能建立 TCP 连接。
每个请求的超时为 `probe_timeout_seconds`。超时、连接失败、非 200、返回内容不是 JSON、`status` 不符，都算失败。
 
## 13. 服务重启恢复
 
在开始接收请求之前执行。如果此时 Docker 无法连接，就不修改任何虚拟机的状态，等 Docker 恢复后的第一轮巡检再按下面的规则处理。不能把状态未知的虚拟机当作已经停止。
 
1. **补齐容器信息**：对每条既不是 queued、也不是 stopped 的记录，按 label `vmm.vm_id` 找到它的容器，用 inspect 的结果补齐 `container_id` 和实际端口，写入数据库。这是为了处理「容器已经启动、端口还没写入数据库」时服务就退出的情况。
2. **按下表处理各状态**：
| 记录状态 | 处理 |
| --- | --- |
| queued | 不变，保留原来的 `queue_order` 和 `queue_deadline`；已超时的，由第一轮巡检处理 |
| creating | 容器在运行：改为 starting，`started_at` 不变。<br>否则，`ever_started=0`（首次创建中断）：改为 failed（PROVISION_FAILED），删除残留的容器，释放资源。<br>否则，`ever_started=1`（重新启动中断）：保留容器和 C 盘，`reserved=0`，改回 queued，保留原来的 `queue_order`，重新计算 `queue_deadline` |
| starting / running | 容器在运行：保持不变，所有时间字段也不变。<br>否则：改为 failed（CONTAINER_EXITED），`reserved=0` |
| stopping / deleting / `reserved=1` 的 failed | 由第一轮巡检继续处理 |
| stopped | 不处理，C 盘随容器保留 |
 
3. **处理采集记录**：
   - 状态为 `recording`、但所属虚拟机已不在 running（或已被删除）的记录，改为 failed，`error` 写「服务重启时虚拟机不在运行」；
   - 所属虚拟机仍在 running、且 `confirmed=1` 的，保持不变，不重新开始采集；
   - 状态为 `recording`、所属虚拟机仍在 running、但 `confirmed=0` 的，由巡检线程用同一个 `recording_id` 重试开始接口（10.2），不生成新的记录。
4. **处理任务空间**：`pending_op` 不为空的任务空间，按 6.2 继续执行。这一步不依赖 Docker。
5. **清理复制残留**：
   - 删除各项目 `tasks/` 下残留的 `.copying-*` 临时目录；
   - 有目录、但数据库中没有记录的任务目录，改名为 `.<task_id>.orphan-<时间>` 保留下来，不删除。

- 带本服务 label、但在数据库中找不到对应记录的容器，只记录 warning，不删除。
- 服务正常退出时，只停止巡检线程，不停止任何虚拟机。

## 14. 测试与交付

测试只验证到容器这一层：宿主机目录、容器挂载、读写权限，以及对 Agent 服务的调用。Windows 内部的盘符、专业软件、录像内容，在外部组件交付后的联调中验证，不属于本项目的范围。

### 14.1 单元测试

使用假的 Docker、HTTP 和采集实现，用可控的时钟模拟时间流逝。具体用例按前文的规则自行设计，至少覆盖以下几类：

- **校验与错误码**：
  - 每个错误码至少一个用例；
  - 非法的 ID（例如包含 `../`）会被拒绝；
  - 校验失败后，不留下任何记录和目录。
- **排队与资源**：
  - 严格按 FIFO 顺序；
  - 并发请求不会超额预留资源；
  - 取消和领取同时发生时，只有一方成功；
  - 重新启动的虚拟机排到队尾；
  - 资源只在确认停止后才释放。
- **状态与时间**：
  - 各状态下启动、停止、删除的处理（11.3）；
  - 排队超时、启动超时、运行到期；
  - 健康检查的各类失败，以及连续失败计数。
- **任务空间**：
  - 自动创建，以及并发创建同一个空间；
  - 冻结、解冻可以重复执行；
  - 冻结、解冻中途失败后保留 `pending_op`，期间不能作为复制的源空间或上游，重试成功后状态一致；
  - 复制的各个错误码，以及失败后的清理，包括改名后写数据库失败时，目标目录被删除；
  - `mounted_by` 的计算。
- **采集**：
  - 自动开始只触发一次；
  - 重复开始时返回同一条记录；
  - 开始接口失败或超时后，用同一个 `recording_id` 重试，不生成新的记录；累计失败 3 次后变为 failed；
  - 开始采集与虚拟机的停止、删除同时发生时，不会在虚拟机离开 running 后开始采集；
  - 采集失败不会阻塞停止流程；
  - 容器退出后，记录变为 failed。
- **恢复**：第 13 节中的每一条规则，包括重新启动中断时保留 C 盘、复制残留的清理。

### 14.2 真实环境测试

测试前先记录 CPU、内存、两块磁盘的剩余空间、镜像 ID 和 KVM 状况。只操作本次测试创建的容器和项目目录，不要动已有的 `winarena` 等容器。

| 场景 | 验证内容 |
| --- | --- |
| 主流程 | 两个环境各创建一台虚拟机：<br>1. 创建后返回 queued；<br>2. 每 5 秒轮询一次，直到状态变为 running，轮询要设置次数上限；<br>3. `docker exec <容器> qemu-img info -U /storage/data.qcow2` 显示的 backing 文件正确；<br>4. probe、VNC、RDP 都能访问 |
| 挂载 | `docker inspect` 中每个挂载的来源、目标、读写模式，都与 5.2 一致 |
| 读写权限 | 用 `docker exec` 在容器内写文件来验证：<br>1. `/shared` 可写；<br>2. 冻结后不可写，宿主机上 `lsattr` 能看到 `i` 属性；解冻后恢复可写；<br>3. `/upstream/<task_id>` 始终只读；<br>4. 同一项目的两个容器，`/project` 中的文件互相可见；<br>5. 在容器内先打开一个文件并保持写入，冻结后继续写入必须失败。这是通过条件：如果仍能写入，在 TEST_REPORT.md 中报告，不要自行变通 |
| 复制 | 复制出的空间内容一致、可写；源空间内容不变，且仍处于冻结状态 |
| 停止与启动 | 1. 停止后资源已释放，记录停止耗时，用来验证停止信号已转发；<br>2. 再次启动后重新排队，最终变为 running；<br>3. `docker logs` 中出现 `Existing disk found, skip initialization.`，说明 C 盘被保留 |
| 删除 | 1. 容器及其匿名卷都已删除，再次查询返回 404；<br>2. 任务目录、采集目录、采集记录都还在 |
| 采集 | 进入 running 后，生成了采集记录和对应的目录。如果 Agent 服务还没有采集接口，开始接口重试 3 次后，记录变为 failed，虚拟机保持 running |
| 排队与超时 | 1. 预算只够一台时，第二台排队；第一台停止后，第二台自动开始创建；<br>2. `queue_timeout_minutes=1` 时，变为 QUEUE_TIMEOUT；<br>3. `timeout_minutes=1` 时，到期后变为 stopped（expired）；<br>4. `probe_path` 指向不存在的路径，并缩短启动超时：变为 STARTUP_TIMEOUT，容器保留 |
| 端口开关 | 关闭 VNC 或 RDP 时，对应的端口确实没有发布 |
| 服务重启 | 在有 running 和 queued 虚拟机的情况下重启服务：<br>1. ID、容器、端口、到期时间、排队顺序都不变；<br>2. 不会重复创建虚拟机，也不会重复开始采集 |

### 14.3 交付物

- 代码、`requirements.txt`、配置示例。
- **README.md**：运行命令、外部组件的对接约定、权限要求（root、`chattr`）、平台的调用顺序、6.2 中冻结的边界说明。
- **TEST_REPORT.md**：每项测试的实际请求、vm_id、端口、输出和结果。
  - 真实测试与 mock 测试分开记录；
  - **不能用 mock 测试代替真实验收**；
  - 没有做到的项目，如实标为「未验证」或「阻塞」。
- 测试结束后，删除本次创建的虚拟机和项目目录。删除前，先对已冻结的目录执行 `chattr -R -i`。

**完成标准**：单元测试全部通过；14.2 中的场景全部实测通过。