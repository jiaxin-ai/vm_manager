# VM Manager v1

HarnessStudio 的「专业软件虚拟化子系统」：一个独立的 REST 服务。它在单台 Linux 宿主机上，用 Docker 容器运行预装了专业软件的 Windows 虚拟机，并管理任务空间（D 盘）、上游（E 盘）、项目公共空间（F 盘）和轨迹采集（G 盘）。

- 接口规范：[`api.md`](api.md)（18 个接口，路径、字段、返回码以它为准）
- 开发任务：[`task.md`](task.md)
- 测试报告：[`TEST_REPORT.md`](TEST_REPORT.md)

## 模块

| 文件 | 职责 |
| --- | --- |
| `main.py` | 读取配置、启动前检查（task.md 4.3）、恢复状态、启动巡检线程、启动 uvicorn |
| `api.py` | 18 个路由、请求/响应模型、统一错误转换 |
| `vm_manager.py` | 虚拟机：校验、资源计算、FIFO 排队、巡检线程、启动/停止/删除、重启恢复 |
| `workspace_manager.py` | 目录规则、任务空间的创建/冻结/解冻/复制、`mounted_by` |
| `recording_manager.py` | 采集记录、开始/结束采集 |
| `backend.py` | 封装 Docker SDK、Agent 服务的 HTTP 调用和宿主机信息（测试时替换为假实现） |
| `db.py` | SQLite（WAL）建表、查询、条件更新 |
| `common.py` | ApiError、按 key 加锁、时间格式 |
| `scripts/start_windows.sh` | 容器启动脚本（只读挂载到容器内） |
| `environments.json` / `settings.json` | 软件环境 / 服务参数 |
| `tests/` | 单元测试（假的 Docker、HTTP、采集，以及可控时钟） |

## 运行

### 依赖

- Linux，Python 3.10+，Docker（能访问 `/var/run/docker.sock`），`/dev/kvm`，`chattr`/`lsattr`（e2fsprogs），`qemu-img`（配置了差分盘环境时必需，否则可选）
- `data_root` 必须位于支持 `chattr +i` 的文件系统上（如 ext4、xfs）
- 镜像必须**事先**存在于本地；服务运行期间不拉取镜像

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 配置

- `settings.json`：`access_host` 必须改成调用方能访问到的宿主机 IP 或域名（不能保留占位值 `REPLACE_WITH_HOST_LAN_IP`）。`vm_overhead_ram_gb`、`default_timeout_minutes`、`default_queue_timeout_minutes`、`boot_timeout_seconds`、`health_failure_threshold`、`health_failure_seconds`（可选，默认 300）在正式部署时必须与 api.md 一致。
- `environments.json`：每个软件环境一项，有两种磁盘形式：
  - **单个基础盘**（vmanager:1.0 的 AutoCAD、AutoCAD+SolidWorks+ANSYS）：`base_storage` 目录下的 `base_img`（只能是文件名），格式为 `base_format`。
  - **基础盘 + 差分盘**（vmanager:1.1 的 KiCad、LTspice、Blender、ParaView、OpenModelica）：多出一个 `diff_img`，写成 `<子目录>/<文件名>`（相对 `base_storage`）。差分盘必须是 qcow2，它记录的后备盘必须正好是 `/base_storage/<base_img>`，后备格式等于 `base_format`。差分盘所在子目录里的 `windows*` 启动文件（TPM 状态等）用来替换 `base_storage` 下的同名文件，所以这些文件名在 `base_storage` 下也必须存在。目录结构：

    ```
    <base_storage>/                    /data/hdd/codes/WindowsAgentArena/src/win-arena-container/vm/storage
    ├── data_base.img                  base_img：公共基础盘（raw）
    ├── windows*                       公共启动文件
    └── kicad-10.0.7/
        ├── data.qcow2                 diff_img：差分盘，后备盘 /base_storage/data_base.img
        └── windows*                   这个软件自己的启动文件
    ```

  **上线后的基础盘和差分盘都不得修改或替换**：差分盘建立在基础盘上，基础盘一变，所有差分盘都会损坏；虚拟机的 C 盘又建立在差分盘上，差分盘一变，建立在它上面的虚拟机都会损坏。上线后不要再把软件子目录当作 `/storage` 直接开机（例如用 WindowsAgentArena 的 `myscripts/start_vms.sh`），那样会直接写差分盘。要更新软件，就做一个新的子目录，按下面的「新增软件环境」新增一个条目。虚拟机本身不会写差分盘：每台虚拟机的 C 盘都是容器里新建的增量盘，`base_storage` 只读挂载。
- 配置在启动时读取，修改后需要重启服务。每台虚拟机创建时会保存一份环境配置副本，之后修改配置不影响已有的虚拟机。
- `listen_host` 默认 `0.0.0.0`。多用户部署建议改成 `127.0.0.1`，只让 Studio 访问。
- 可选鉴权：`settings.json` 增加 `auth_token_file`（或设置环境变量 `VM_MANAGER_TOKEN_FILE`），文件内容是一行随机令牌。设置后所有 `/v1/*`、`/docs`、`/openapi.json` 都要求 `Authorization: Bearer <令牌>`；文件不存在或为空时服务拒绝启动。Studio 用 `STUDIO_VMM_TOKEN_FILE` 指向同一个只读文件。不配置时行为与旧版一致。令牌不要写进仓库或命令行；文件权限建议 0600。

### 新增软件环境

以「基础盘 + 差分盘」的形式（vmanager:1.1）新增一个软件。vm_manager 这边只需要在 `environments.json` 里加一项并重启服务；Studio 不用改，它从 `GET /v1/environments` 读取环境列表。前提是差分盘已经按下面的要求做好。

1. **做差分盘**：在 `base_storage` 下新建一个子目录（例如 `mujoco-3.17.0-cpu`），用 vmanager:1.1 开一台制作用的虚拟机：`base_storage` 只读挂载到 `/base_storage`，子目录挂载到 `/storage`；以 `/base_storage/data_base.img`（raw）为后备盘创建 `/storage/data.qcow2`，并把根目录的 `windows*` 启动文件复制进子目录。WindowsAgentArena 的 `myscripts/start_vms.sh` 就是这样做的。在这台虚拟机里安装软件。
2. **映射盘符**：vm_manager 只提供 Samba 共享，D、E、F、G 盘要由 Windows 自己映射。关机前，在这台虚拟机里以登录用户（Docker）执行：

   ```bat
   net use D: \\host.lan\Data /persistent:yes
   net use E: \\host.lan\Upstream /persistent:yes
   net use F: \\host.lan\Project /persistent:yes
   net use G: \\host.lan\Recordings /persistent:yes
   ```

   映射保存在这个用户的 `HKCU\Network` 里，之后每次登录都会自动恢复。少了这一步，虚拟机照样能开机，但没有 D、E、F、G 盘。
导出前先检查 `Invoke-RestMethod http://127.0.0.1:5000/probe` 返回 `Probe successful`，并实际完成一次录制，确认生成视频、截图和轨迹。新 Runtime 会在登录时检查并修复 Agent / Recorder 依赖；已有 Hook 更新方法见 `docker/runtime11/README.md`。仅能打开软件不代表操作服务可用。

3. **正常关机，之后不再直接启动这块盘**：执行 `docker stop -t 300 <制作用的容器>`，等容器日志里出现 `Shutdown completed!` 再删除容器。从此不要再直接启动这个子目录。
4. **检查差分盘**：`qemu-img info <base_storage>/<子目录>/data.qcow2` 应当显示 `file format: qcow2`、`backing file: /base_storage/data_base.img`、`backing file format: raw`。
5. **加环境条目**：`environments.json` 属于 root，要用 sudo 编辑。追加一项，例如：

   ```json
   {
     "environment_id": "win11-kicad10.0.7",
     "os": "windows11",
     "software": [{"name": "KiCad", "version": "10.0.7"}],
     "min_spec": {"cpu_cores": 4, "ram_gb": 8},
     "docker_image": "vmanager:1.1",
     "base_storage": "/data/hdd/codes/WindowsAgentArena/src/win-arena-container/vm/storage",
     "base_img": "data_base.img",
     "base_format": "raw",
     "diff_img": "kicad-10.0.7/data.qcow2",
     "probe_path": "/probe"
   }
   ```

   `environment_id` 沿用「win11-软件名版本」的写法，CPU 版末尾加 `-cpu`。上线后不要再改 ID：Studio 和已有的虚拟机都靠它识别环境。`min_spec` 是创建虚拟机时允许的最低配置。
6. **先单独跑一遍启动前检查**：任何一个条目不通过，整个服务都会启动失败，所有环境都不可用。所以重启前先执行：

   ```bash
   cd /data/hdd/codes/HarnessStudio/vm_manager && sudo .venv/bin/python -c 'import main; from backend import Backend; s, e = main.load_config("settings.json", "environments.json"); main.startup_checks(s, e, Backend()); print("启动前检查通过，环境数", len(e))'
   ```

   没有输出「启动前检查通过」时，按报错修改，不要重启。
7. **重启服务**（见下面的「启动与停止」），然后用 `curl -s http://127.0.0.1:8000/v1/environments` 确认新环境已经出现。停止服务不会停止已经在运行的虚拟机。
8. **在 Studio 里验证**：在任务的软件区点「添加软件环境」，能看到新环境；启动一台，确认它进入运行状态、D、E、F、G 盘都在，然后删除这台虚拟机。
9. **Agent 看到的分类**（可选）：Studio 按软件名给环境分类（机械设计、工程仿真、电子设计、建模渲染、二维制图），不认识的软件名归为机械设计。要改分类，在 harnessstudio 的 `plugins/studio/src/vm-seat.ts` 里给 `categoryRules` 加一条，然后重新部署 Studio。

「单个基础盘」形式（vmanager:1.0）的环境同样是加条目、检查、重启，只是不写 `diff_img`；基础盘同样必须已经配置好 D、E、F、G 映射。

### 启动与停止

服务必须以 **root** 运行（`chattr +i/-i` 需要 CAP_LINUX_IMMUTABLE），单进程、单个 uvicorn worker：

```bash
# 前台
sudo .venv/bin/python main.py
# 后台，日志写入文件
mkdir -p logs
(setsid sudo env PYTHONDONTWRITEBYTECODE=1 $PWD/.venv/bin/python $PWD/main.py >> logs/vmm.log 2>&1 < /dev/null &)
# 使用其他配置文件（例如测试时临时修改参数）
sudo .venv/bin/python main.py --settings /path/settings.json --environments /path/environments.json
```

停止：向监听端口的进程发送 SIGTERM，只按明确的 PID 操作：

```bash
sudo ss -ltnp 'sport = :8000'          # 找到 pid
sudo kill -TERM <pid>
```

服务退出时只停止巡检线程，**不会停止任何虚拟机**；重新启动后会按 task.md 第 13 节恢复状态（同一台虚拟机不会被重复创建，也不会重复开始采集）。

启动前检查任一项不满足，服务都会以退出码 1 结束，并在日志中给出原因：环境 ID 重复、`base_img` 不是文件名、`diff_img` 不是「子目录/文件名」、`access_host` 是占位值、`/dev/kvm` 不存在、Docker 无法连接、镜像不在本地、基础磁盘或差分盘不存在、差分盘目录里的 `windows*` 文件在 `base_storage` 下没有同名文件、`data_root` 不可写或不支持 `chattr +i`、基础磁盘的实际格式与 `base_format` 不一致；有差分盘环境时还有：宿主机没有 `qemu-img`、差分盘不是 qcow2 或带有损坏标记、差分盘记录的后备盘不是 `/base_storage/<base_img>` 或后备格式与 `base_format` 不一致。

### 测试

```bash
.venv/bin/python -m pytest -q tests/
```

单元测试不需要 root、Docker 或 KVM。

## 宿主机目录与权限

```
<data_root>/                             （默认 /data/hdd/vm_manager）
├── vmm.db                               本服务的数据库，不挂载给任何虚拟机
└── projects/<project_id>/
    ├── shared/                          F 盘：项目公共空间（读写）
    ├── tasks/<task_id>/                 D 盘：本任务空间（读写，冻结后只读）；作为上游时为 E:\<task_id>（只读挂载）
    └── recordings/<vm_id>/<rec_id>/     G 盘：本虚拟机的采集结果
```

- 目录按需自动创建，服务**从不删除** `projects/` 下的内容（只有复制产生的临时目录除外）；删除虚拟机时，任务空间、公共空间、采集目录和采集记录都保留。
- 容器以特权模式、root 身份运行，所以虚拟机写入的文件在宿主机上属于 root。
- 冻结通过 `chattr -R +i` 实现。**要手动删除已冻结的目录，必须先执行** `sudo chattr -R -i <目录>`。
- 每台虚拟机只挂载与自己相关的目录（本任务、声明的上游、所属项目的公共空间、自己的采集目录），不挂载 `data_root` 或 `projects/`。

## 外部组件的对接约定

| 组件 | 约定 |
| --- | --- |
| Docker 镜像 | `environments.json` 中的 `docker_image`：单个基础盘的环境用 `vmanager:1.0`（构建见 `docker/`），基础盘 + 差分盘的环境用 `vmanager:1.1`（构建见 `docker/runtime11/`）。镜像内的共享文件夹服务把 `/shared`→D、`/upstream`→E、`/project`→F、`/recordings`→G 发布给 Windows |
| 启动脚本 | `scripts/start_windows.sh` 只读挂载到 `/opt/vm-manager/start_windows.sh`，以 `/bin/bash` 执行。增量盘 `/storage/data.qcow2` 已存在时跳过初始化（重新启动后保留 C 盘），否则复制 `/base_storage/windows*` 启动文件，并以 `/base_storage/$BASE_IMG`（格式 `$BASE_FORMAT`）为后备盘创建增量盘；把停止信号转发给 Windows，使其正常关机 |
| 基础磁盘 | `base_storage` 目录只读挂载到 `/base_storage`。单个基础盘的环境传 `BASE_IMG=<base_img>`、`BASE_FORMAT=<base_format>`。差分盘环境传 `BASE_IMG=<diff_img>`、`BASE_FORMAT=qcow2`，磁盘链为 C 盘增量 → 差分盘 → `/base_storage/<base_img>`；差分盘目录里的每个 `windows*` 文件再逐个只读挂载到 `/base_storage/` 下的同名文件上，启动脚本复制的就是这一份。C 盘是容器内 `/storage/data.qcow2` 上的增量：停止后保留，删除虚拟机时随容器一起删除 |
| Windows 盘符 | 本服务只通过镜像发布 Samba 共享（`\\host.lan\Data`、`Upstream`、`Project`、`Recordings`），D、E、F、G 由磁盘里的 Windows 在登录时自行映射。1.0 的两个基础盘已经配置；1.1 的五个差分盘在 2026-10-27 用 `net use … /persistent:yes` 补上了映射（见「新增软件环境」第 2 步） |
| 容器端口 | VNC 8006、RDP 3389、Agent 5000。宿主机端口由 Docker 随机分配，**每次启动都可能变化**；Agent 端口始终发布，VNC/RDP 按 `expose` 决定 |
| Agent 服务（虚拟机内） | 健康检查：`GET http://127.0.0.1:<agent 端口><probe_path>` 返回 HTTP 200，且 JSON 中 `status == "Probe successful"`；同时开放的 VNC 返回 2xx/3xx、RDP 能建立 TCP 连接 |
| 采集接口（由 Agent 提供） | 开始：`POST {recorder_start_path}`，请求体 `{"recording_id","output_dir":"G:\\rec_xxx","project_id","task_id","vm_id","chat_id"}`，以 HTTP 200 表示成功，对同一个 `recording_id` 重复调用不会重复启动；停止：`POST {recorder_stop_path}`，请求体 `{"recording_id"}`，以 HTTP 200 表示采集进程已经结束、文件已经写完，重复调用不出错。超时为 `recorder_timeout_seconds` |
| 容器识别 | 本服务创建的容器都带有 label `vmm.managed=true` 和 `vmm.vm_id=<vm_id>`，名称为 `vm-<vm_id>`。服务只操作带这些 label 的容器 |

## HarnessStudio 的调用顺序

1. `GET /v1/environments`：选择软件环境；可以用 `GET /v1/host/resources` 查看当前容量。
2. （可选）`POST /v1/workspaces`：预先创建任务空间。创建虚拟机时也会自动创建。
3. `POST /v1/vms`：创建虚拟机，立即返回 `queued`。**请求超时后不要直接重试**，先用 `GET /v1/vms?task_id=…` 确认是否已经创建。
4. 每 5 秒轮询一次 `GET /v1/vms/{vm_id}`，直到 `status=running`。排队时可以查看 `queue.position/reason/expires_at`。失败时查看 `error.code`。
5. 使用 `access.host` 加上 `vnc_port`/`rdp_port`/`agent_port` 连接虚拟机。**以最新返回的端口为准**。
6. 虚拟机进入 running 后会自动开始采集；`POST /v1/vms/{vm_id}/recordings`、`POST /v1/recordings/{id}/stop` 用于补充控制。
7. 任务完成后，先让用户或 Agent 保存文件，再调用 `POST /v1/workspaces/{id}/freeze`。
8. 下游任务创建虚拟机时，通过 `upstream_task_ids` 只读挂载已冻结的上游；需要修改上游成果时，用 `POST /v1/workspaces/{id}/copy` 为新任务复制一份可写空间。
9. 使用完毕后，`POST /v1/vms/{id}/stop`（保留 C 盘，之后可以 `start`）或 `DELETE /v1/vms/{id}`。这两个接口都是同步的，需要等待 Windows 关机，客户端超时请设为 180 秒以上。

## 冻结的边界（task.md 6.2）

- 冻结只作用于**已经写到磁盘上的文件**，不会替软件保存未保存的内容。平台应当先让用户或 Agent 保存文件，再调用冻结。
- 已经打开的文件，在冻结后也不能再写入：容器层（内核、ext4）已经实测验证，向已打开的 fd 继续写入会返回 `Operation not permitted`。经过 Samba（Windows 中的 D 盘）时的行为，需要在联调时验证。
- E 盘挂载的是上游目录本身，不是快照：上游被解冻并修改后，下游能看到新的内容。是否允许解冻一个已被下游引用的空间，由平台决定，本服务不做判断。
- 冻结或解冻执行到一半失败时（返回 500），会保留 `pending_op`，由巡检线程每 60 秒重试一次，服务重启时也会继续执行。在此期间，这个空间一律视为“未冻结”：不能作为复制的源空间，也不能作为上游。

## 常见问题

- **创建后一直是 queued**：查看详情中的 `queue.reason`。`waiting_turn` 表示前面还有请求（严格 FIFO）；`insufficient_resources` 表示 CPU/内存不足，或者 data_root、Docker 数据目录所在磁盘的剩余空间少于 `min_free_disk_gb`；`docker_unavailable` 表示 Docker 暂时无法连接。超过 `queue_timeout_minutes` 后变为 `failed/QUEUE_TIMEOUT`。
- **REQUEST_EXCEEDS_CAPACITY**：单台虚拟机的占用（CPU = `cpu_cores`，内存 = `ram_gb + vm_overhead_ram_gb`）超过了宿主机预算（逻辑 CPU 数 − `host_reserved_cpu`，物理内存 − `host_reserved_ram_gb`），宿主机空闲时也无法创建。
- **STARTUP_TIMEOUT**：从开始创建算起，`boot_timeout_seconds` 内健康检查都没有通过。容器会被停止但保留，可以用 `docker logs vm-<vm_id>` 排查，然后删除虚拟机后重新创建。实测首次开机约 6～8.5 分钟，重新开机约 1.5～2 分钟。
- **HEALTH_CHECK_FAILED / CONTAINER_EXITED**：运行中 Agent 服务持续 `health_failure_seconds`（默认 300 秒）无响应且连续检查失败达到 `health_failure_threshold` 次，或者容器自行退出。在虚拟机里重启 Windows 时容器一直在运行，只要几分钟内恢复就不算失败；恢复后自动开始新的采集记录。容器停止后保留（含 C 盘），可以再次 `start`，沿用原来的容器；容器已被删除时返回 409 VM_FAILED，只能删除后重新创建。
- **STOP_FAILED / DELETE_FAILED**：可以再次调用重试；巡检线程也会每 60 秒自动补做一次。
- **采集记录一直是 failed，error 为“开始采集失败 3 次”**：虚拟机内的 Agent 还没有提供采集接口（或者接口返回非 200）。虚拟机本身不受影响，保持 running。
- **disk_gb.used 比 df 显示的大**：按规范，`used = total − free`，其中包含文件系统的保留块。
- **停止要多久**：已开机的 Windows 实测约 7～15 秒；Windows 还在启动时，可能需要接近 `stop_timeout_seconds`。超时后 Docker 会强制结束容器。
- **想清空测试数据**：先删除虚拟机，停止服务；然后对要删除的项目目录执行 `sudo chattr -R -i`，再删除；如果需要，最后删除 `vmm.db*`。

`settings.json` 的 `access_host` 可设为 `auto`：服务每次启动从物理默认路由网卡检测局域网 IPv4，避开 VPN/TUN 和 Docker 网卡；无法检测则明确报错。固定 IP 或域名仍按原值使用。换网口后重启服务才重新检测；不会自动重启正在运行的虚拟机。

### 独立虚拟机系统盘（2026-11-24）

可选设置 `vm_storage_root` 为 SSD/NVMe 上的绝对目录，系统为新建实例绑定 `<vm_storage_root>/<vm_id>` 到容器 `/storage`；不会迁移已有容器，也不会删除旧盘。未设置时兼容原容器层存储。容量检查同时覆盖此目录所在文件系统。生产设置使用 `/home/ajx/.local/share/harnessstudio/vm-storage`。项目工作区、录像和数据库继续保存在原 `data_root`。

新版镜像制作与备份流程见 `docker/runtime11/README.md` 的「2026-11-24」章节。导出必须在 Windows 正常关机后执行。

### HDD 原件与有上限的 SSD 镜像缓存

生产配置通过 `image_cache_root` 开启只读镜像缓存；`image_cache_max_gb` 为缓存物理占用上限（GiB），`image_cache_reserve_gb` 为缓存填充时预留的 SSD 空闲空间。当前分别为 1400 / 200 GiB。未配置缓存时保持原来的直接挂载方式。

- 环境的 `base_storage` 指向 HDD 原件。第一次创建实例先复制所需基础盘、软件差分盘和 `windows*` 启动元数据到 SSD，再启动 Windows；后续命中缓存直接启动。
- 缓存保留镜像内的相对目录和 `/base_storage` 后备链路径；同一个原件在不同软件环境间使用硬链接复用。原件身份、大小或修改时间改变后生成新缓存，旧实例继续使用旧版本。
- 容量不足时按最近使用时间淘汰无实例引用的缓存。运行中和已停止但可恢复的实例都锁定缓存，只有删除实例后解除锁定。全部被占用时明确报错，不删除依赖镜像。
- 缓存准备在后台执行，不阻塞巡检；启动超时与运行时长从缓存准备完成、开始启动容器时计算。首次复制时间与缓存命中后的启动耗时应分别统计。
- `vm_storage_root` 中每个实例的可写 C 盘不是缓存，不会被缓存淘汰；其空间不计入 1400 GiB 镜像预算，但受 SSD 空闲空间检查保护。删除实例仍保留该目录及工作区，后续清理必须确认不再需要。
- 缓存使用临时文件、原件变更检查和原子发布，服务重启保留引用记录；不要手工删除 `objects`、`views`、`leases`，也不要修改正在被引用的缓存文件。

本机整个 T7 镜像库归档位置：`/data/hdd/vm_manager/image-library/t7-storage/`。修复过启动流程的 Revit 原件单独保存在 `/data/hdd/vm_manager/image-library/revit-2027/`，不要用 T7 的旧 Revit 覆盖它。其余已注册原件原先已位于 `/data/hdd`，继续保留其原路径。外盘原件作为备份保留。

以后制作软件差分镜像：正常关机后导出完整软件差分目录到 HDD，保留它所依赖的基础盘以及 `windows*` 元数据；注册环境的 `base_storage` 指向 HDD 目录。无需手动复制到 SSD，也不要将 `base_storage` 指向缓存目录。首次启动会自动准备缓存，常用环境可以提前调用 `ImageCache.prepare(env, temporary_id)` 预热，完成后 `release(temporary_id)`；尚未启动实例的预热缓存允许被淘汰。

T7 库的登记包含原始、CPU 和 GPU 命名的各差分盘，软件版本先沿用镜像目录标记；CATIA 目录未标版本，显示“版本待确认”。“GPU 镜像”只是该镜像变体的名称，当前启动流程没有配置 GPU 直通，不代表已经提供硬件 GPU 加速。Revit 使用修复版原件，不重复注册旧版盘。

迁移期间如果平台有任务运行，不为加载新增环境而重启 VM Manager。旧 Revit 路径 `/home/ajx/.local/share/harnessstudio/vm-images/revit-2027` 可保留为指向 HDD 原件的兼容符号链接，缓存键使用解析后的真实路径，因此不会额外复制一份缓存。新增环境登记写入 `environments.json` 后，需在安全重启 VM Manager 时加载；兼容链接不占用镜像数据空间，也不要在旧配置仍被进程引用时删除。
