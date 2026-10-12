# VM Manager v1 测试报告

- 测试日期：2026-10-12（UTC）。报告中的时间都是 UTC；宿主机本地时区为 UTC-7，`ls` 显示的本地时间比 UTC 早 7 小时。
- 代码版本：见 `git log`。真实测试期间只改了两处代码（服务退出时停止巡检线程、data_root 不可写时给出明确错误，见第 6 节），改完都重新跑了单元测试。
- 真实测试与 mock 测试分开记录：第 3 节是单元测试（假的 Docker/HTTP/采集，以及可控时钟），第 4 节是真实环境测试。**第 4 节的结论都来自真实执行，没有用 mock 结果代替。**

## 1. 结论

| 项目 | 结果 |
| --- | --- |
| 单元测试（14.1） | **167 passed**（`.venv/bin/python -m pytest -q tests/`，约 18 秒） |
| 真实环境：主流程（两个环境） | 通过 |
| 真实环境：挂载 | 通过 |
| 真实环境：读写权限（含“先打开文件、冻结后继续写入必须失败”） | 通过 |
| 真实环境：复制 | 通过 |
| 真实环境：停止与启动 | 通过 |
| 真实环境：删除 | 通过 |
| 真实环境：采集（Agent 没有采集接口，重试 3 次后 failed，虚拟机保持 running） | 通过（符合预期） |
| 真实环境：排队与超时（4 项） | 通过 |
| 真实环境：端口开关 | 通过 |
| 真实环境：服务重启 | 通过 |
| Docker 断开、KVM/镜像/基础磁盘缺失等场景 | **未验证**（会触碰宿主机边界，见第 5 节；只有单元测试） |

## 2. 测试条件（测试前记录）

| 项目 | 值 |
| --- | --- |
| CPU | Intel Core i9-9900K，1 路 × 8 核 × 2 线程 = 16 个逻辑 CPU |
| 内存 | MemTotal 65680456 kB（62 GiB），MemAvailable 47503036 kB（约 45 GiB） |
| data_root 所在磁盘 | `/data/hdd`（/dev/sda1，ext4）：总量 9.1T，已用 348G，剩余 8.3T |
| Docker 数据目录 | `DockerRootDir=/mnt/ssd/docker`，位于 `/`（/dev/nvme0n1p2，ext4）：总量 916G，已用 86G，剩余 784G |
| 镜像 | `julyai/mywinarena:v1`，ID `sha256:7980fb2e65563789bc175110bd117977376f0b80b7801bf4b2613d917461f052`，53.6GB，镜像没有声明 VOLUME |
| KVM | `/dev/kvm` 存在（`crw-rw----+ root kvm`），`kvm-ok`：KVM acceleration can be used |
| 基础磁盘 | `data_v1_autocad_30G.img`（raw，50 GiB）、`data.img`（raw，200 GiB），用 `qemu-img info -U --output=json` 只读确认 |
| Docker | 29.8.1，API 1.56；Docker SDK for Python 7.2.0 |
| 软件 | Python 3.10.12，FastAPI 0.143.0，Pydantic 2.14.1，uvicorn 0.54.0，requests 2.34.2，pytest 9.1.1 |
| 已有容器 | 测试前 `docker ps -a` 为空（没有 winarena 容器），卷数量 0 |
| 服务参数 | `settings.json`：`access_host=10.11.13.84`（eno1 的内网 IP），`data_root=/data/hdd/vm_manager`，其余为 task.md 4.2 的默认值 |
| 运行方式 | `sudo .venv/bin/python main.py`（root，单进程，单 worker），后台运行，日志写入 `logs/vmm.log` |

## 3. 单元测试（mock）

命令：`.venv/bin/python -m pytest -q tests/`，结果 **167 passed**。用到的假实现在 `tests/conftest.py` 中：`FakeBackend`（假的 Docker、Agent、宿主机信息）、`FakeClock`（可控时钟）、`SyncExecutor`（后台任务同步执行），chattr 也换成了假实现（单元测试不以 root 运行）。`test_probe.py` 例外，它用本机真实的 HTTP/TCP 服务测试 `backend.probe` 和 `agent_post`。

| 14.1 要求 | 文件 / 主要用例 |
| --- | --- |
| 每个错误码至少一个用例 | `test_validation.py`：VALIDATION_ERROR（18 种字段错误、非法 JSON、筛选参数、采集请求体）、ENVIRONMENT_NOT_FOUND、BELOW_ENVIRONMENT_MINIMUM、REQUEST_EXCEEDS_CAPACITY、UPSTREAM_NOT_FOUND、UPSTREAM_NOT_FROZEN、DOCKER_UNAVAILABLE、HOST_NOT_READY（kvm/image/base_disk）、VM/WORKSPACE/RECORDING_NOT_FOUND、VM_BUSY、VM_FAILED、VM_NOT_RUNNING、INTERNAL_ERROR、WORKSPACE_EXISTS、WORKSPACE_NOT_FROZEN；STOP_FAILED、DELETE_FAILED 在 `test_lifecycle.py`/`test_queue.py`；RECORDING_START_FAILED、RECORDING_STOP_FAILED 在 `test_recordings.py` |
| 非法 ID（`../` 等）被拒绝 | `test_create_vm_validation_error`、`test_workspace_validation_error`、`test_path_must_stay_under_projects`（符号链接逃逸） |
| 校验失败不留下记录和目录 | 每个失败用例都调用 `assert_nothing_written` |
| 严格 FIFO | `test_strict_fifo_head_blocks_followers`、`test_each_round_starts_at_most_one` |
| 并发请求不超额预留 | `test_concurrent_scheduling_does_not_over_reserve`（8 个线程同时调度）、`test_concurrent_creates_get_distinct_queue_positions` |
| 取消与领取只有一方成功 | `test_cancel_and_claim_only_one_wins`（20 轮真实线程竞争）、`test_cas_on_queued_only_once` |
| 重新启动排到队尾 | `test_restarted_vm_goes_to_queue_tail` |
| 确认停止后才释放资源 | `test_resources_released_only_after_confirmed_stop`、`test_failed_stop_retried_by_inspector` |
| 各状态下启动/停止/删除（11.3） | `test_lifecycle.py` 中 start/stop/delete 的参数化用例 |
| 排队超时、启动超时、运行到期 | `test_queue_timeout`、`test_startup_timeout_keeps_container`、`test_startup_timeout_with_bad_probe_path`、`test_expiry_stops_vm` |
| 健康检查的各类失败与连续计数 | `test_health_check_failures_counted_and_reset`、`test_container_exited`、`test_docker_down_does_not_count_failures_or_release`；`test_probe.py`：非 200、非 JSON、status 不符、超时、连接拒绝、VNC 非 2xx/3xx、RDP 端口关闭、忽略代理环境变量 |
| 任务空间 | `test_workspaces.py`：自动创建与复用、12 个线程并发创建同一空间、冻结/解冻可重复执行、chattr 失败保留 pending_op 且不能作复制源/上游、60 秒后重试成功、复制的各错误码、cp 失败清理临时目录、改名后写库失败删除目标目录、`mounted_by` |
| 采集 | `test_recordings.py`：自动开始只一次、重复开始返回同一条、失败后用同一 ID 重试且 3 次后 failed（失败前先调用一次停止）、与停止并发（开始采集持锁时停止返回 VM_BUSY；停止后不再开始）、采集失败不阻塞停止/删除、容器退出后 failed、真实线程池下自动采集与删除串行 |
| 恢复（第 13 节每一条） | `test_recovery.py`：creating 的三种情况（含重新启动中断时保留 C 盘、保留 queue_order）、starting/running 容器在/不在、补齐 container_id 与端口、queued/stopped 不变、stopping/deleting 由第一轮巡检继续、Docker 不可用时推迟恢复、采集记录三条规则、pending_op 补做、复制残留清理与孤儿目录改名、未知 label 容器不删除、有 running+queued 时整体重启 |
| 启动前检查（4.3） | `test_startup.py`：9 种失败、非 root 时 chattr 检查失败、data_root 不可写 |

## 4. 真实环境测试（14.2）

测试项目：`vmmtest_p1`、`vmmtest_p2`、`vmmtest_p3`。所有请求都发往 `http://127.0.0.1:8000`；访问性检查使用 `access_host=10.11.13.84`。轮询每 5 秒一次，每条 curl 都设置了超时（10 秒），单次轮询最多 18～42 次。

### 4.0 服务启动

```
$ (setsid sudo -n env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python main.py >> logs/vmm.log 2>&1 < /dev/null &)
02:28:20Z INFO main: VM Manager listening on 0.0.0.0:8000 (access_host=10.11.13.84, data_root=/data/hdd/vm_manager)
$ sudo ss -ltnp | grep :8000 → users:(("python",pid=2731846,fd=15))
```
启动前检查全部通过，包括以 root 在 data_root 上执行 chattr +i 后写入失败，以及 qemu-img 格式检查。

`GET /v1/host/resources` → `{"cpu":{"total":16,"allocated":0,"available":14},"memory_gb":{"total":62,"allocated":0,"available":41},"disk_gb":{"total":9239,"used":813,"available":8426},"vms":{"running":0,"queued":0}}`

### 4.1 主流程（通过）

准备上游：`POST /v1/workspaces {"project_id":"vmmtest_p1","task_id":"tA"}` → `ws_aeeb631d`。在宿主机写入 `result.txt`、`sub/n.txt` 后执行 `POST /v1/workspaces/ws_aeeb631d/freeze` → `status=frozen`，`lsattr` 显示目录和文件都有 `i`。

| | VM1 | VM2 |
| --- | --- | --- |
| 请求 | `POST /v1/vms {"project_id":"vmmtest_p1","task_id":"tB","chat_id":"chat_01","environment_id":"win11-autocad2026","spec":{"cpu_cores":4,"ram_gb":8},"upstream_task_ids":["tA"]}` | `POST /v1/vms {"project_id":"vmmtest_p1","task_id":"tC","chat_id":"chat_02","environment_id":"win11-autocad2026-solidworks2025-ansys2024r1","spec":{"cpu_cores":4,"ram_gb":8}}` |
| 响应 | `{"vm_id":"vm_fcb4e060","status":"queued","queue_position":1,"workspace_id":"ws_692f7c0a"}` | `{"vm_id":"vm_6c26e3a5","status":"queued","queue_position":1,"workspace_id":"ws_547c3ff5"}`（VM1 已被领取，所以它是第 1 位） |
| 容器 | `6b1044a19e05`（`vm-vm_fcb4e060`） | `6de6047dc44e`（`vm-vm_6c26e3a5`） |
| 端口 vnc/rdp/agent | 32786 / 32784 / 32785 | 32789 / 32787 / 32788 |
| queued → starting | 02:28:46 → 02:28:46 | 02:28:46 → 02:28:46 |
| running | 02:35:05（6 分 19 秒） | 02:37:10（8 分 24 秒） |
| `docker exec <c> qemu-img info -U /storage/data.qcow2` | `backing file: /base_storage/data_v1_autocad_30G.img`，`backing file format: raw`，virtual size 50 GiB | `backing file: /base_storage/data.img`，`backing file format: raw`，virtual size 200 GiB |
| probe（`curl http://10.11.13.84:<agent>/probe`） | `{"message":"Service is operational","status":"Probe successful"}` | 同左 |
| VNC（`curl http://10.11.13.84:<vnc>/`） | HTTP 301 | HTTP 301 |
| RDP（向 `10.11.13.84:<rdp>` 发送 X.224 Connection Request） | 收到 X.224 Connection Confirm `030000130ed0…`，确认是真实的 RDP 服务 | 同左 |

running 时的详情（VM1，节选）：`"access":{"host":"10.11.13.84","vnc_port":32786,"rdp_port":32784,"agent_port":32785}`，`"mounts":{"workspace":{"workspace_id":"ws_692f7c0a","writable":true},"upstreams":[{"task_id":"tA","workspace_id":"ws_aeeb631d","path":"E:\\tA"}],"project_shared_path":"/data/hdd/vm_manager/projects/vmmtest_p1/shared","recordings_path":"/data/hdd/vm_manager/projects/vmmtest_p1/recordings/vm_fcb4e060"}`，`"started_at":"2026-10-12T02:28:46Z","expires_at":"2026-10-12T04:28:46Z"`。

### 4.2 挂载（通过）

`docker inspect vm-vm_fcb4e060 --format '{{range .Mounts}}…{{end}}'`：

```
bind /data/hdd/ProSoftAgentArena/src/win-arena-container/vm/storage -> /base_storage RW=false mode=ro
bind /data/hdd/vm_manager/projects/vmmtest_p1/shared -> /project RW=true mode=rw
bind /data/hdd/vm_manager/projects/vmmtest_p1/recordings/vm_fcb4e060 -> /recordings RW=true mode=rw
bind /data/hdd/vm_manager/projects/vmmtest_p1/tasks/tB -> /shared RW=true mode=rw
bind /data/hdd/vm_manager/projects/vmmtest_p1/tasks/tA -> /upstream/tA RW=false mode=ro
bind /data/hdd/codes/vm_manager/scripts/start_windows.sh -> /opt/vm-manager/start_windows.sh RW=false mode=ro
```
VM2 的挂载相同，只是没有 `/upstream`，`/shared` 对应 `tasks/tC`。没有挂载 `data_root` 或 `projects/` 这样的上级目录，也没有 `volume` 类型的挂载。

其他参数也与 9.2 一致：`Entrypoint=["/bin/bash"] Cmd=["/opt/vm-manager/start_windows.sh"]`，`RAM_SIZE=8G CPU_CORES=4 BASE_IMG=data_v1_autocad_30G.img BASE_FORMAT=raw`，`NanoCpus=4000000000 Memory=9663676416 MemorySwap=9663676416`（9 GiB，不允许用 swap），`Privileged=true CapAdd=[NET_ADMIN]`，`Devices=/dev/kvm rwm`，`RestartPolicy=no AutoRemove=false`，label 中有 `vmm.managed=true vmm.vm_id=vm_fcb4e060`。

### 4.3 读写权限（通过）

| # | 命令（在容器内执行） | 输出 | 结果 |
| --- | --- | --- | --- |
| 1 | `docker exec vm-vm_fcb4e060 sh -c 'echo "written by vm1" > /shared/from_vm1.txt && cat …'` | `written by vm1`，rc=0 | /shared 可写 |
| 2 | `POST /v1/workspaces/ws_692f7c0a/freeze` 后：`echo new > /shared/after_freeze.txt`、`echo more >> /shared/from_vm1.txt`、`rm /shared/from_vm1.txt` | 都返回 `Operation not permitted`（rc=2/2/1） | 冻结后不可写 |
| 2 | 宿主机 `sudo lsattr -d tasks/tB; sudo lsattr tasks/tB` | 目录和每个文件都是 `----i---------e-------` | 能看到 `i` 属性 |
| 2 | `POST …/ws_692f7c0a/unfreeze` 后：`echo "after unfreeze" > /shared/after_unfreeze.txt && echo more >> /shared/from_vm1.txt` | rc=0；`lsattr` 中不再有 `i` | 解冻后恢复可写 |
| 3 | `touch /upstream/tA/new.txt`；`echo x >> /upstream/tA/result.txt` | `Read-only file system`；`Operation not permitted` | 上游只读 |
| 3 | 临时解冻 tA（`POST …/ws_aeeb631d/unfreeze`）后再执行 `touch /upstream/tA/new.txt`，然后重新冻结 | `Read-only file system` | 上游**始终**只读，不受解冻影响 |
| 4 | VM1：`echo "project file from vm1" > /project/from_vm1.txt`；VM2：`cat /project/from_vm1.txt && echo … > /project/from_vm2.txt`；VM1：`cat /project/from_vm2.txt` | 两边都能读到对方的文件 | /project 互相可见 |
| 5 | 见下 | `WRITE_FAILED rc=1`，`echo: write error: Operation not permitted` | **通过条件满足** |

第 5 项的做法：在容器内用 `exec 3>>/shared/held_open.txt` 打开文件并写入一行，然后等待宿主机的信号；宿主机调用冻结接口（0.75 秒返回）后，通知容器继续向同一个 fd 写入：

```
docker exec -d vm-vm_fcb4e060 bash -c 'exec 3>>/shared/held_open.txt; echo "line written before freeze" >&3; …; while [ ! -f /tmp/go ]; do sleep 0.2; done; if echo "line written after freeze" >&3 2>/tmp/err; then echo WRITE_OK > /tmp/res; else echo "WRITE_FAILED rc=$?" > /tmp/res; fi'
POST /v1/workspaces/ws_692f7c0a/freeze → 200
docker exec vm-vm_fcb4e060 touch /tmp/go
docker exec vm-vm_fcb4e060 sh -c 'cat /tmp/res; cat /tmp/err'
→ WRITE_FAILED rc=1
→ bash: line 1: echo: write error: Operation not permitted
宿主机 held_open.txt 的内容只有 "line written before freeze"
```
宿主机内核是 6.8、文件系统是 ext4。ext4 对已经打开的文件也会检查 immutable 标志，所以冻结后继续写入会失败。

另外观察到：镜像内的共享文件夹服务启动时会在 `/shared` 中写入 `readme.txt`，并把目录权限改为 777。这是外部组件的行为，本服务没有处理。

### 4.4 复制（通过）

```
POST /v1/workspaces/ws_aeeb631d/copy {"project_id":"vmmtest_p1","task_id":"tD"}
→ 200 {"workspace_id":"ws_1a2af5e6","project_id":"vmmtest_p1","task_id":"tD","status":"writable","path":"/data/hdd/vm_manager/projects/vmmtest_p1/tasks/tD","source_workspace_id":"ws_aeeb631d","mounted_by":[],"created_at":"2026-10-12T02:29:45Z","frozen_at":null}
sudo diff -r tasks/tA tasks/tD → content identical
复制前后对 tA 中每个文件计算 sha256，结果相同 → source unchanged
sudo lsattr -R tasks/tD → 都没有 i；向 tD/result.txt 追加内容、新建文件都成功 → copy writable
sudo sh -c "echo x >> tasks/tA/result.txt" → Operation not permitted
GET /v1/workspaces/ws_aeeb631d → status=frozen, frozen_at 不变
tasks/ 下没有残留的 .copying-* 目录
```

### 4.5 停止与启动（通过）

```
POST /v1/vms/vm_fcb4e060/stop
→ 200 {"vm_id":"vm_fcb4e060","status":"stopped","stop_reason":"manual","stopped_at":"2026-10-12T02:37:34Z"}  curl 用时 7.53 s
日志：vm vm_fcb4e060 container stopped in 7.5s；vm vm_fcb4e060 stopped, resources released
docker logs：❯ Received SIGTERM, sending ACPI shutdown signal... / Waiting for Windows to shutdown... (1/110) (2/110) / ❯ Shutdown completed!
docker ps：Exited (143)（SIGTERM 被处理，正常退出；没有等到 120 秒后的 SIGKILL）
GET /v1/host/resources：cpu.allocated 8 → 4，memory_gb.allocated 18 → 9（VM2 仍在运行）
```
停止耗时：已开机的 Windows 为 7.5 s 和 13.9 s；还在启动中的 Windows 为 98.2 s 和 23.8 s（到期停止）、10.4 s（启动超时后停止）。删除运行中的虚拟机分别用时 36.3 s 和 28.4 s，包含关机时间。这说明停止信号已经转发给 Windows：如果没有转发，每次都会等满 120 秒后被强制结束（退出码 137）。

```
POST /v1/vms/vm_fcb4e060/start → 200 {"vm_id":"vm_fcb4e060","status":"queued","queue_position":1}
02:37:40 claimed → starting（沿用原容器 6b1044a19e05，端口变为 vnc 32796 / rdp 32794 / agent 32795）
02:39:27 running（第二次开机 1 分 47 秒）
docker logs vm-vm_fcb4e060 | grep：
  1:1. Construct basic env...
  2:Formatting '/storage/data.qcow2', … backing_file=/base_storage/data_v1_autocad_30G.img …   ← 首次启动
  21:1. Construct basic env...
  22:Existing disk found, skip initialization.                                                ← 重新启动
停止前 data.qcow2 的 disk size 为 2.12 GiB，重新启动后为 2.15 GiB（保留了原来的增量）
```

### 4.6 删除（通过）

```
删除前：docker volume ls -q | wc -l → 0；容器的挂载全部是 bind
DELETE /v1/vms/vm_6c26e3a5 → 200 {"vm_id":"vm_6c26e3a5","status":"deleted"}（36.3 s，包含关机）
docker ps -a --filter id=6de6047dc44e… → 0 个；docker volume ls → 0
GET /v1/vms/vm_6c26e3a5 → 404 {"error":{"code":"VM_NOT_FOUND","message":"虚拟机不存在","details":{"vm_id":"vm_6c26e3a5"}}}
再次 DELETE → 200 deleted
tasks/tC/ 仍在；recordings/vm_6c26e3a5/rec_857126ab/ 仍在
GET /v1/recordings?vm_id=vm_6c26e3a5 → rec_857126ab（failed）仍在
GET /v1/workspaces?project_id=vmmtest_p1&task_id=tC → ws_547c3ff5 仍在
```
匿名卷：镜像没有声明 VOLUME，所以不会产生匿名卷。代码按 9.3 调用 `remove(v=True)`，本次只能确认前后卷数量都是 0。

### 4.7 采集（通过，符合预期）

VM1 进入 running 时（02:35:05）自动开始采集：
```
recording rec_17867e50 created for vm_fcb4e060
start attempt 1 failed: /recording/start: HTTP 500   02:35:05.87（自动开始）
start attempt 2 failed: /recording/start: HTTP 500   02:35:07.70（巡检重试，同一个 recording_id）
start attempt 3 failed: /recording/start: HTTP 500   02:35:09.71（巡检重试，同一个 recording_id）
cleanup stop failed: /recording/stop: HTTP 500       （改为 failed 之前，先用同一个 ID 停止一次）
GET /v1/recordings?vm_id=vm_fcb4e060 → rec_17867e50 status=failed, error="开始采集失败 3 次：/recording/start: HTTP 500",
     result_path=/data/hdd/vm_manager/projects/vmmtest_p1/recordings/vm_fcb4e060/rec_17867e50（目录已创建）
虚拟机状态：running
```
VM2（rec_857126ab）的结果相同；VM1 之后每次重新进入 running，都只产生一条新记录（rec_bf18c847、rec_b8889ff9），各自重试 3 次后变为 failed。直接请求 Agent 确认它还没有采集接口：`POST http://127.0.0.1:32785/recording/start` 返回 HTTP 500（不是 404）。对已失败的记录调用停止接口（`POST /v1/recordings/rec_17867e50/stop`），返回原记录，内容不变。

### 4.8 排队与超时（通过）

先用测试配置重启服务：在草稿目录中准备 `settings_test.json`（`host_reserved_cpu=10`，CPU 预算 = 16−10 = 6，只够一台；`boot_timeout_seconds=180`）和 `environments_test.json`（在原有两个环境之外，增加 `win11-autocad2026-badprobe`，其 `probe_path` 为 `/no-such-probe`），启动命令为 `main.py --settings … --environments …`。这两个文件都在草稿目录中，仓库中的配置文件没有修改。

1. **预算只够一台时第二台排队**：VM1 运行中（4 核），`cpu.available=2`。
   `POST /v1/vms {"project_id":"vmmtest_p3","task_id":"tQ",…4 核}` → `vm_734d8fe2` queued，详情为 `"queue":{"position":1,"reason":"insufficient_resources",…}`。
   02:41:56 `POST /v1/vms/vm_fcb4e060/stop`，02:42:10 停止完成（13.9 s）→ 02:42:12 日志 `vm vm_734d8fe2 claimed from queue, creating`，02:42:12 进入 starting（vnc 32799 / rdp 32797 / agent 32798）。**通过**
2. **queue_timeout_minutes=1**：`POST /v1/vms {…"task_id":"tT",…"lifecycle":{"queue_timeout_minutes":1}}` → `vm_d13ca1c4` queued，`"queue":{"position":2,"reason":"waiting_turn","expires_at":"2026-10-12T02:41:05Z"}`。02:41:06 变为 `failed`，`error.code=QUEUE_TIMEOUT`（"排队超过 1 分钟仍未开始创建"），`stop_reason`、`stopped_at` 都为 null。**通过**
3. **timeout_minutes=1**（使用正式配置）：`POST /v1/vms {"project_id":"vmmtest_p2","task_id":"tE",…,"expose":{"vnc":false,"rdp":true},"lifecycle":{"timeout_minutes":1}}` → `vm_7b168c6c`，`started_at=02:29:53Z`，`expires_at=02:30:53Z`。02:30:54 `expired, stopping`，02:32:32 `stopped`，`stop_reason=expired`。因为 Windows 还在启动，关机用了 98.2 s。`vm_472efa13`（RDP 关闭）同样在 02:33:51 到期，02:34:15 变为 stopped/expired。**通过**
4. **probe_path 不存在 + 缩短启动超时**：`POST /v1/vms {"project_id":"vmmtest_p3","task_id":"tP","environment_id":"win11-autocad2026-badprobe",…}` → `vm_c5e412f2`，02:42:55 进入 starting。02:45:55 变为 `failed`，`STARTUP_TIMEOUT`（"开始创建后 180 秒仍未就绪"）；02:46:05 后台停止容器（10.4 s）后 `resources released`。`docker ps -a` 显示 `vm-vm_c5e412f2 Exited (143)`，容器被保留；详情中 `stop_reason=failure`、`stopped_at=02:46:05Z`；`cpu.allocated=0`。**通过**

### 4.9 端口开关（通过）

| 虚拟机 | expose | `docker port` | `NetworkSettings.Ports` | 接口返回 |
| --- | --- | --- | --- | --- |
| vm_7b168c6c | vnc=false, rdp=true | `3389/tcp -> 0.0.0.0:32790`，`5000/tcp -> 0.0.0.0:32791`（另有对应的 `[::]` 绑定） | `"8006/tcp":null` | `vnc_port:null, rdp_port:32790, agent_port:32791` |
| vm_472efa13 | vnc=true, rdp=false | `5000/tcp -> 0.0.0.0:32792`，`8006/tcp -> 0.0.0.0:32793` | `"3389/tcp":null` | `vnc_port:32793, rdp_port:null, agent_port:32792` |

### 4.10 服务重启（通过）

终止服务时，只按监听 8000 端口的 PID 执行 `sudo kill -TERM <pid>`：先用 `ss -ltnpH 'sport = :8000'` 查出 PID，再用 `ps -o pid,user,cmd -p <pid>` 确认它就是本服务。

- **重启 1**（02:39:49，切换为测试配置；VM1 running）：重启前后对 vms 表中的 `vm_id,status,container_id,ports,expires_at,queue_order,reserved` 以及 recordings 的数量做快照，`diff` 完全一致。
- **重启 2**（02:41:41，VM1 running + vm_734d8fe2 queued）：快照内容包括 vms 的 `vm_id,status,container_id,ports,started_at,expires_at,queue_order,queue_deadline,reserved`、全部采集记录（id、状态、重试次数）以及 `docker ps -a --filter label=vmm.managed=true`。重启后 `diff` 输出 **STATE IDENTICAL AFTER RESTART**：ID、容器、端口、到期时间、排队顺序都没有变化，没有新建容器，也没有新的采集记录（`rec_bf18c847` 保持 failed，没有重新开始采集）。重启后排队项仍是 `insufficient_resources`，VM1 继续通过健康检查（之后的手动停止返回 `stop_reason=manual`）。
- **重启 3**（02:46:39，切回正式配置）：日志 `vm_manager: inspector stopped; virtual machines are left as they are`。已停止的容器都没有被启动或删除。

### 4.11 额外验证（不在 14.2 中，也都是真实执行）

- **CONTAINER_EXITED**：先确认 `vm-vm_edd5d9b6` 带有 `vmm.managed:true vmm.vm_id:vm_edd5d9b6` label（本服务创建的容器），再执行 `docker kill vm-vm_edd5d9b6`。02:47:05 变为 `failed/CONTAINER_EXITED`，`stop_reason=failure`，`cpu.allocated` 只剩 VM1 的 4 核。
- **HEALTH_CHECK_FAILED**：对运行中的 `vm-vm_fcb4e060`（label 已确认）执行 `docker pause`。暂停本身失败了（`OCI runtime pause failed: timeout of 10s reached waiting for the cgroup to freeze`），但冻结过程让探测连续失败：02:48:31、02:48:36、02:48:41 记为第 1/2/3 次失败，之后变为 `failed/HEALTH_CHECK_FAILED`。后台停止容器（32.7 s，退出码 137，与那次失败的冻结有关）后释放资源，容器保留。
- **启动前检查失败**（4.3）：`access_host` 为占位值 → `启动前检查失败：access_host 仍是占位值…`，exit=1；以非 root 运行 → `data_root 不支持 chattr +i（需要 root 运行）…`，exit=1；data_root 不可写 → `data_root 不可写：/data/hdd/vm_manager：[Errno 13] Permission denied…`，exit=1。最后一项是在真实测试中发现的问题：修复前会直接抛出 PermissionError 堆栈，修复后给出上面的明确错误。
- **错误码抽查**：VM_FAILED（start 已失败的虚拟机）、VALIDATION_ERROR（`project_id:"../x"`）、ENVIRONMENT_NOT_FOUND、BELOW_ENVIRONMENT_MINIMUM、REQUEST_EXCEEDS_CAPACITY（`capacity` 为测试配置下的 `{"cpu_cores":6,"ram_gb":58}`）、UPSTREAM_NOT_FOUND、UPSTREAM_NOT_FROZEN、VM_NOT_RUNNING、VM_NOT_FOUND，返回码与 api.md 一致；这些失败请求都没有创建 `vmmtest_p9` 目录。

### 4.12 收尾清理

```
DELETE 6 台虚拟机（vm_edd5d9b6 vm_c5e412f2 vm_d13ca1c4 vm_472efa13 vm_7b168c6c vm_fcb4e060）→ 全部 200 deleted
GET /v1/vms → {"items":[]}；docker ps -a --filter label=vmm.managed=true → 0；docker ps -a → 空；docker volume ls → 0
sudo kill -TERM 2740100（监听 8000 的服务进程）→ service stopped，日志 inspector stopped；8000 端口不再有监听
sudo chattr -R -i projects/vmmtest_p1 projects/vmmtest_p2 projects/vmmtest_p3 → 带 i 的条目 4 → 0
sudo rm -rf -- /data/hdd/vm_manager/projects/vmmtest_p1 /data/hdd/vm_manager/projects/vmmtest_p2 /data/hdd/vm_manager/projects/vmmtest_p3
基础磁盘的 mtime 没有变化（data.img 10-07 03:22，data_v1_autocad_30G.img 09-21 07:45，本地时间）；镜像 ID 没有变化
```
保留的内容：`/data/hdd/vm_manager/vmm.db*` 和空的 `projects/`。数据库中虚拟机的记录已经随删除接口清除，但任务空间和采集记录按设计会保留（v1 没有删除接口），它们指向的目录已经被删除。按照“只在测试项目目录内删除文件”的边界，没有删除数据库文件；如果需要一个干净的环境，请在服务停止后手动执行 `sudo rm /data/hdd/vm_manager/vmm.db /data/hdd/vm_manager/vmm.db-wal /data/hdd/vm_manager/vmm.db-shm`。

## 5. 未验证 / 阻塞的项目

| 项目 | 状态 | 原因 |
| --- | --- | --- |
| Docker 断开：创建/启动/停止返回 503 DOCKER_UNAVAILABLE、排队 `docker_unavailable`、巡检跳过 Docker 相关步骤、重启时推迟恢复 | **未验证**（只有单元测试） | 需要停止 Docker 服务，触碰边界 |
| HOST_NOT_READY（接口 503，以及队首变为 failed） | **未验证**（只有单元测试） | 需要移走 /dev/kvm、镜像或基础磁盘，触碰边界 |
| STOP_FAILED / DELETE_FAILED 及其重试、PROVISION_FAILED | **未验证**（只有单元测试） | 需要让 Docker 调用失败，无法在不触碰边界的前提下制造 |
| 采集接口调用成功的路径（confirmed=1、停止成功、stopped 状态） | **未验证**（只有单元测试） | Agent 服务还没有采集接口（返回 HTTP 500） |
| 重启恢复中 creating 中断、Docker 断开等分支 | **未验证**（只有单元测试） | 需要在容器创建的瞬间杀掉服务，或者断开 Docker |
| 复制失败后的清理（cp 失败、改名后写库失败） | **未验证**（只有单元测试） | 需要人为制造磁盘或数据库故障 |
| 经过 Samba 时冻结的行为、Windows 内的盘符和软件、录像内容 | 不在本次范围 | task.md 规定在外部组件交付后的联调中验证 |

## 6. 实现与文档不一致之处

| 文档 | 实际实现 | 理由 |
| --- | --- | --- |
| task.md 8.3 准入检查：先查 KVM/镜像/基础磁盘，再查 Docker | **先查 Docker 能否连接**，再查 KVM、镜像、基础磁盘 | 按你的修正执行：检查镜像本身要访问 Docker，Docker 断开时会把队首误判为 HOST_NOT_READY。单元测试 `test_docker_unavailable_keeps_queue`、`test_validation_order_docker_before_host` 覆盖了这一点 |
| task.md 第 3 节的模块列表 | 多了 `common.py`（ApiError、按 key 加锁、时间格式） | 各 manager 和 api 都要用 ApiError 和锁。放进任何一个 manager 都会造成循环导入，所以单独放一个小文件，没有其他抽象 |
| task.md 10.2：自动开始采集时“等待获取”实例锁 | 巡检线程在 starting → running 的同一次持锁中，把实例锁**交给**后台线程，由后台线程执行同一个开始采集函数后释放 | 效果与“等待获取”相同：开始时虚拟机一定还是 running，并且与停止/删除互斥。如果后台线程在线程池里阻塞等锁，而锁的持有者又排在线程池队列后面，理论上会死锁；交接锁可以避免这种情况 |
| task.md 4.3 第 6 项：`qemu-img info --output=json` | 加上 `-U`（force-share，仍然只读） | 基础磁盘正被运行中的虚拟机作为 backing 使用时，不加 `-U` 可能因为文件锁冲突而失败 |
| task.md 9.4 脚本内容 | 开头多了 `#!/bin/bash` 和一行注释，其余逐字一致 | 便于识别。实测容器没有提前退出，不需要改成 `tail -f /dev/null` |
| task.md 2 / main.py：“启动服务” | 巡检线程在 FastAPI lifespan 的关闭阶段停止，而不是在 `uvicorn.run()` 之后 | 实测发现 uvicorn 处理完 SIGTERM 后会重新触发这个信号，`uvicorn.run()` 之后的代码不会执行，“服务正常退出时只停止巡检线程”的收尾就丢了。已修复，并在真实环境中验证 |
| main.py 命令行 | 增加了 `--settings`、`--environments` 两个可选参数（默认值为项目根目录下的两个文件） | 真实测试需要临时修改配置；用参数指向草稿目录中的副本，就不用改动仓库中的正式配置 |

## 7. 自行做出的判断

1. **data_root**：使用 task.md 中的 `/data/hdd/vm_manager`。宿主机上已有的 `/data/hdd/vm-manager-data` 属于其他项目，没有使用，也没有修改。
2. **运行环境**：在项目目录下建立 `.venv`，以 `sudo .venv/bin/python main.py` 运行（root）；日志写入 `logs/vmm.log`，时间为 UTC；设置 `PYTHONDONTWRITEBYTECODE=1`，避免在仓库中生成属于 root 的 pyc 文件。
3. **lifecycle 默认值**：不传或传 null 时，取 settings 中的 `default_timeout_minutes`、`default_queue_timeout_minutes`。
4. **字段约束**：`cpu_cores`、`ram_gb`、`timeout_minutes`、`queue_timeout_minutes` 必须是正整数（StrictInt，>0）；`chat_id` 可以是任意字符串或 null；查询参数中未知的字段会被忽略。
5. **VALIDATION_ERROR 的 details.fields**：`field` 去掉 `body`/`query` 前缀后用 `.` 连接（如 `spec.cpu_cores`、`upstream_task_ids.0`）；请求体不是合法 JSON 时为 `body`；`reason` 使用 Pydantic 的原始英文说明。
6. **重复冻结**：仍然重新执行一次 `chattr`，但保留第一次的 `frozen_at`（它表示变为冻结的时间）。
7. **last_retry_at**：每次补做失败（接口或巡检）都写入当前时间，成功后清空；为 null 时立即重试。采集记录没有这个字段，task.md 也没有要求，所以未确认的采集记录每轮都重试（每轮最多一次），实测三次尝试的间隔约为 2 秒。
8. **stop_reason / stopped_at**：STARTUP_TIMEOUT、HEALTH_CHECK_FAILED、CONTAINER_EXITED，以及重新启动时的 PROVISION_FAILED，都设置 `stop_reason=failure`，`stopped_at` 为确认容器停止的时间。QUEUE_TIMEOUT、HOST_NOT_READY 和首次创建时的 PROVISION_FAILED，两者都为 null，因为这次运行还没有开始。
9. **失败的采集记录**也写入 `stopped_at`（结束时间）。
10. **手动停止采集时，“容器已退出”的判定**：所属虚拟机已被删除，或者状态不是 running，就直接改为 failed，error 写“虚拟机已退出，采集未正常结束”，不调用 Agent。
11. **realpath 越界**（例如 `projects/<pid>` 是指向外部的符号链接）：返回 500 INTERNAL_ERROR，因为 api.md 没有为它定义返回码。
12. **队首的 queue.reason**：巡检线程还没有评估过时，按 `waiting_turn` 返回。
13. **mounts.workspace.writable**：按任务空间的 `status == writable` 计算。
14. **生成 vm_id** 时，同时检查 recordings 表，避免新虚拟机复用已删除虚拟机的 ID 和采集目录。
15. **端口**：停止后、以及重新排队期间，仍返回上一次的端口（api.md 2.3 规定，停止或失败后仍返回原来分配的地址）；重新启动后用新读出的端口覆盖。
16. **删除流程**：停止容器确认成功后先置 `reserved=0`，再 `remove(v=True)`，这样删除失败时不会一直占着资源。首次创建失败的容器会先停止，再删除；删除失败时保持 `reserved=1`，由巡检线程补做停止。
17. **健康检查**：遇到第一项失败就停止后面的检查。Docker 客户端超时取 `max(180, stop_timeout_seconds+60)`。
18. **到期与超时的比较**：到期为 `now >= expires_at`（“到达”）；排队超时为 `now > queue_deadline`，启动超时为 `now > started_at + boot_timeout_seconds`（“超过”）。
19. **复制时只有目录、没有记录**：返回 WORKSPACE_EXISTS，`details.workspace_id` 为 null。
20. **未定义的路由或方法**：保持 FastAPI 默认的 404/405（`{"detail": ...}`），因为 api.md 没有为它们定义返回码，不自行新增。
21. **测试项目名**用 `vmmtest_p1..p3`，与真实数据明显区分。
22. **任务跟踪**：任务列表保存在会话草稿目录中，没有放进仓库。

## 8. 接口规范的问题和实测观察（未修改 api.md）

1. **disk_gb.used**：按 task.md 的公式 `used = total − free`，结果包含 ext4 的保留块，所以实测为 813 GiB，而 `df` 的 Used 为 348G。api.md 写的是“该磁盘已使用的容量”，两者含义不同，需要平台知道这个差异。
2. **memory_gb.available**：按 8.3 计算时，取“预算 − 已分配”与“MemAvailable − 预留”中的较小值。运行中的虚拟机会同时计入这两项（已分配、以及实际占用使 MemAvailable 下降），所以 available 偏保守。实测：两台运行时 available=28；停止一台后为 34。这是规范规定的行为。
3. **RDP 健康检查**（12.2）：规范只要求“能建立 TCP 连接”。Docker 默认使用 userland-proxy，即使 Windows 中的 RDP 还没有就绪，宿主机端口也能建立 TCP 连接，所以这一项的意义有限。好在 Agent 探活排在前面，而且实测 running 时 RDP 能返回真实的 X.224 Connection Confirm。
4. **Agent 对不存在的路径返回 HTTP 500，而不是 404**：`/recording/start`、`/recording/stop`、`/no-such-probe` 都是这样。按约定这些都判为失败，对本服务没有影响，联调时需要注意。
5. **api.md 2.5 的停止表**写的是“stopped / failed → 直接返回当前状态”，task.md 11.3 则补充了“failed 且 reserved=1 时先补做一次停止”。实现按 task.md。
6. **开机时间**：首次开机 6～8.5 分钟，重新开机约 1.5～1.8 分钟，都在 `boot_timeout_seconds=900` 之内。
7. **`docker pause`** 在这类 KVM 容器上会因为 cgroup 冻结超时而失败。这不影响本服务（本服务不使用 pause），只是作为观察记录。
8. **容器正常停止后的退出码是 143**：脚本中 bash 收到 TERM 后，`wait` 返回 143，这是正常现象。
