# vmanager:1.1（镜像内置 Runtime）

本目录是独立的最小增量构建，不使用 WindowsAgentArena/runtime/vmanager 中原有的另一套方案。
继承本机 vmanager:1.0（不拉取更新，构建前后核对 image ID）；只新增 /opt/runtime 并在原 /run/samba.sh 中增加一个只读共享。不改入口、QEMU、网络/5000 转发、Base Image、WAA 任务及 D/E/F/G；不启动 Windows、不安装容器 Python 依赖。

## 构建

```bash
python3 /data/hdd/codes/HarnessStudio/vm_manager/docker/runtime11/build.py
```

默认原样复制以下完整目录（不改原文件），在临时构建目录计算逐文件 SHA256 后 COPY 进新镜像：

- /data/hdd/codes/HarnessStudio/server → /opt/runtime/server
- /data/hdd/vm_manager/projects/trajrec-test-20261019/shared/trajrec-bundle → /opt/runtime/trajrec-bundle

临时目录在构建结束自动清理。运行时不需要上述宿主目录、也不要为 /opt/runtime 添加挂载。镜像发布 manifest.json、两个 PowerShell 脚本和此文档。原 Data/Upstream/Project/Recordings 配置及权限不变。Runtime 使用同一 host.lan/Samba 连接，不映射任何盘符。和原共享一样，需要 SAMBA/NETWORK 开启。

现有运行中的 1.0 容器不会自动变成 1.1。本次不会替换它们。请在自己安排的维护窗口，通过现有 VM Manager 的容器创建/镜像选择机制使用 vmanager:1.1，保留原有磁盘及挂载配置。不要让两个容器同时打开同一个 Windows 磁盘；不要将 1.1 挂成 Windows Base Image。现有 VM 的实际切换由使用者进行。

## Windows 一次性接入（用户手动执行）

先确保该 VM 已运行在 vmanager:1.1 容器下。以原 WAA 登录账户打开**管理员 Windows PowerShell 5.1**。

1. 确认共享可读：

```powershell
Get-ChildItem '\\host.lan\Runtime'
Get-Content '\\host.lan\Runtime\manifest.json' -Raw
Test-Path '\\host.lan\Runtime\server\main.py'
Test-Path '\\host.lan\Runtime\trajrec-bundle\recorder\integration\harnessstudio\install_recorder.ps1'
```

2. 查明现有任务调用哪个启动脚本（不创建新任务）：

```powershell
Get-ScheduledTask | Where-Object TaskName -Match 'WindowsArena|WAA' | ForEach-Object {
    $_ | Select-Object TaskName, State
    $_.Actions | Format-List Execute, Arguments, WorkingDirectory
}
Get-Content 'C:\OEM\on-logon.ps1'
```

本机 1.0 Docker 镜像内 /oem/on-logon.ps1 的原逻辑是 `python $pythonScriptFile --port $pythonServerPort`，脚本目录为 C:\oem，端口 5000；未读取或修改 VM 内实际脚本。若 VM 已定制，以实际任务为准。

3. 安装前置调用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File '\\host.lan\Runtime\Install-WaaHook.ps1' -Startup 'C:\OEM\on-logon.ps1'
```

脚本先验证上述已知启动格式，再备份原脚本，复制更新器到 C:\OEM，并在原脚本最前面加入同步调用。原来的 Caddy 和 main.py 启动顺序/参数保持；原 `python` 解析到的 sys.executable 同时用于 pip 与 main.py。新版会在备份后为原任务加上 -NoProfile（见文末 2026-11-24 更新），不启动第二个 Agent，不使用另一套 Agent Python。格式不匹配时拒绝改动，按实际脚本手动加入同样的前置调用，并让原 Agent 命令使用 `$vmanagerPython`；不要另建任务。

4. 重启 Windows，让原 WAA 登录启动流程首次执行更新（原任务若在登录时触发，仍在登录时执行，不改为独立系统服务）。不要在 Agent 正在监听 5000 时手动执行更新器；会拒绝并记日志。原任务应在原交互用户桌面以管理员权限运行，以便现有 Recorder 安装器安装 transcript hook；若尚未勾选“使用最高权限运行”，由用户在**原任务**中勾选，不创建新任务。

日志：`C:\ProgramData\VManagerRuntime\update.log`。状态：同目录 `state.json`。失败/中断标记 `pending.json` 不要手工删除，重试会重新同步和安装。网络不可用、哈希不符、pip/Recorder 安装失败均阻止该次 WAA 启动。

更新器先校验共享文件并在 C:\OEM 下暂存，再替换目标目录；旧目录保留为 `server.before-*`、`trajrec-bundle.before-*`。保留本地非发行文件（如 recordings/screenshots）；仅清除上一版本清单中已删除的发行文件。没有版本变化则不复制、不安装。requirements.txt 的哈希或实际 Python 路径变化才重新安装 Agent 依赖；初次及失败恢复必装。使用已有环境 `python -m pip install -r`，保留 requirements.txt，不卸载无关包。

Recorder bundle 的任意文件变化都会触发原 install_recorder.ps1，参数为 `-Wheels C:\OEM\trajrec-bundle\wheels`，另加 `-Python` 指向实际 Agent Python。不传 -PatchAgent。Recorder 原安装器自身按已有实现维护 C:\trajrec\venv（由已有 Python 创建，不安装另一套 Python）。它会复制 routes 模块；更新器在 finally 恢复原 server 包的 routes 文件，确保不改所交付的 Agent 源码。当前 server 与 Recorder routes 内容已核对一致。

依赖安装不是数据库式事务：失败后旧目录备份仍在，但 Python 环境可能已被 pip 部分更新。因此失败时阻止 Agent 启动，修复原因后重新运行原 WAA 流程，不能绕过更新器直接启动半更新服务。新源码本身的功能兼容性由其维护者负责；此更新器不改源码/端口/API。

## 备份、恢复原启动脚本

安装器输出唯一备份路径 `C:\OEM\on-logon.ps1.before-runtime11-*.bak`。也可安装前自行执行：

```powershell
Copy-Item 'C:\OEM\on-logon.ps1' 'C:\OEM\on-logon.ps1.manual-backup'
```

恢复时指定**所需的确切备份**（先查看内容）：

```powershell
Copy-Item -LiteralPath 'C:\OEM\on-logon.ps1.manual-backup' -Destination 'C:\OEM\on-logon.ps1' -Force
```

不必删除更新器或新建/删除任务。恢复启动脚本只移除更新钩子，不回滚已安装依赖。若要回滚代码，先停止原 Agent，再保留当前目录并恢复对应的 before-* 目录；依赖应由同一个 Python 按目标版本重新安装、校验成功后才恢复启动。

## 以后更新

1. 由使用者正常更新上述源目录中的 server、requirements.txt 或 trajrec-bundle。
2. 再运行 build.py 构建（推荐新标签保留已有 1.1，例如 `python3 build.py --tag vmanager:1.1-20261022`）。脚本只快照源码、不修改它们；构建期间请不要同时写入发布源。
3. 维护窗口使用新镜像创建相应 VM 容器，保留原 VM 磁盘及 D/E/F/G 挂载；不删除原镜像、容器、数据。仅重启旧容器不会载入新镜像。
4. Windows 下次按原 WAA 流程启动时读取新 manifest。只改 server 代码不重装 requirements；requirements 改动安装 Agent 依赖；Recorder 改动调用原安装器。更新器自身变更时，用户手工从 Runtime 覆盖 C:\OEM\Update-Runtime.ps1；不自动执行未经配置的新启动逻辑。

## 验证

宿主执行（只使用新建的无 VM 验证容器，不挂原有数据）：

```bash
python3 -m venv /tmp/vmanager11-check-tools
/tmp/vmanager11-check-tools/bin/pip install smbprotocol
/tmp/vmanager11-check-tools/bin/python /data/hdd/codes/HarnessStudio/vm_manager/docker/runtime11/verify.py
```

该检查校验镜像内所有文件哈希，确认无宿主挂载，比较 1.0/1.1 的入口、环境和关键启动脚本，启动独立 Samba 实例测试 Runtime 可读/拒绝写入，并核对四个共享路径及读写属性。完整 Windows 登录、UNC 名称解析、盘符和 API 只能由用户在 VM 内最终验证：

```powershell
Get-ChildItem '\\host.lan\Runtime\server'
Get-ChildItem '\\host.lan\Runtime\trajrec-bundle'
Get-SmbMapping
Get-PSDrive D,E,F,G
Get-Content 'C:\ProgramData\VManagerRuntime\update.log' -Tail 60
Invoke-RestMethod 'http://127.0.0.1:5000/probe'
Get-CimInstance Win32_Process | Where-Object CommandLine -Match 'server\\main.py' | Select-Object ExecutablePath,CommandLine
```

重启第二次，日志应显示 hashes unchanged；按需要测试源码更新、requirements 更新、Recorder 更新及不可用共享导致拒绝启动。D/E/F/G 应仍指向原共享，Upstream 仍只读；Runtime 不分配盘符。宿主验证无法替代这些 Windows 端验证。

## 2026-11-20：依赖完整性修复

旧服务代码直接导入 psutil、pywin32、pywinauto、comtypes、pyperclip，但上游 requirements 未完整声明。构建现在在发布快照中合并 `requirements-windows.txt`（不改上游源码），并加入 `check-dependencies.py`。每次登录更新都实际检查 Agent 和 Recorder 的 Python 导入；版本未变但依赖损坏时仍重装。安装后再次验证，通过才写入成功状态。

源码目录已移动时，可从已发布镜像保留原服务和 Recorder，再构建新版本：

```bash
python3 docker/runtime11/build.py --from-runtime-image vmanager:1.1 --tag vmanager:1.1-runtimefix-20261120
```

已有磁盘的 Hook 不会自行更换本地更新器。切换新 Docker 镜像后，在管理员 PowerShell 再执行原 `Install-WaaHook.ps1` 命令即可；现在会备份并刷新本地更新器，保留原 Hook 和登录任务。不要修改全局 PowerShell 执行策略。

导出前必须验证：`http://127.0.0.1:5000/probe` 返回成功、五个 UNC 共享可访问、实际录制开始和结束成功并生成产物。仅看软件首页和任务最高权限不代表服务就绪。依赖缺失时不能把镜像当作通过验收。

本机修复发布完成后，`vmanager:1.1` 指向修复版，旧版本另存为 `vmanager:1.1-before-runtimefix-20261120`。现有容器不会因标签变化而自动更新；应正常关闭后重建容器。Revit 环境固定使用 `vmanager:1.1-runtimefix-20261120`，便于追溯。

## 2026-11-24：固定启动解释器与镜像发布

制作软件镜像时使用 `vmanager:1.1-bootfix-20261124`。在原登录账户的管理员 PowerShell 中执行（Python 路径应是该镜像实际使用的解释器）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File \\host.lan\Runtime\Install-WaaHook.ps1 -Python C:\Users\Docker\AppData\Local\Programs\Python\Python310\python.exe
```

新版安装器在改动前备份原脚本、更新器及计划任务 XML；将既有任务加上 `-NoProfile`，并把已核实的 Python 完整路径写入启动脚本。不会新建任务，不依赖交互式 profile 或 PATH 中另一套 Python。省略 `-Python` 时仅使用已有 Runtime 状态中的路径；无法确定就报错，不猜解释器。

导出前必须完成一次新 Runtime 更新，确认 `/probe` 成功，再正常关机。不要把“等待首次开机安装依赖”的磁盘作为已完成镜像发布。新版依赖检查先校验共享中的检查器，再复制到本地执行；依然检查实际导入，缺包仍报错或修复，未降低探活标准。`update.log` 记录 manifest、Agent 和 Recorder 检查阶段，便于区分 Windows 启动、共享访问及依赖安装耗时。

正式环境通过 `settings.json` 的可选 `vm_storage_root` 指定独立运行盘目录，推荐本机 NVMe；每台实例使用 `<vm_storage_root>/<vm_id>/` 绑定到 `/storage`。新建实例生效，现有容器不自动迁移。该目录中的磁盘在删除实例后仍保留，确认不需要数据后再清理。制作容器也要显式 `-v <制作盘目录>:/storage`，避免 C 盘写入 Docker 的容器层。基础盘、软件差分盘和运行盘都应优先使用 SSD，机械盘保留归档备份。

更换镜像时先保留旧磁盘、启动元数据、环境配置和 Docker 标签；对发布副本离线合并/导出，禁止向运行中的后备盘提交差分。发布后新建独立实例验收：从 POST 创建请求开始计时，到 Agent `/probe`、VNC 与 RDP 全部通过、平台显示 `running` 为止。首次验收期间不并行复制镜像，不用人工启动服务代替开机任务。

正常关机等待使用 300 秒（制作容器 `--stop-timeout 300`，VM Manager `stop_timeout_seconds: 300`）。仅容器退出不够：必须确认日志出现 `Shutdown completed!`；如果超时强停，先恢复启动并正常关机，再发布磁盘。
