# 本次验证结果

最终镜像：`vmanager:1.1`

Image ID：`sha256:d084c28d6dda0ff3b8ba89d759b0a902192a8ed19ffeff7bc999bc4f4b51a137`

原 vmanager:1.0 保持：`sha256:0129f7ac1013b0d9e2eab49ad1ca32eaf289949c227415ebddee6c646c013fcd`

- 构建成功，见 build.log。
- 镜像内 server 20 个文件、trajrec-bundle 48 个文件逐一 SHA256 校验成功；验证容器没有宿主数据挂载。
- 1.0 所有镜像层仍为 1.1 的前缀；入口、Cmd、环境变量、暴露端口、工作目录、Volumes、Healthcheck 一致。
- /run/entry.sh、boot.sh、network.sh、disk.sh、/oem/on-logon.ps1 与 1.0 哈希一致。
- /run/samba.sh 只增加 Runtime 共享；原四个共享定义逐字保留。
- 独立验证容器内启动原 Samba；通过宿主 SMB 客户端实际读取两个目录中的文件并逐字节比对成功。
- 实际写入 Runtime 和 Upstream 得到 STATUS_ACCESS_DENIED；Data、Project、Recordings 写入及读回成功。测试只写临时验证容器内部目录，不触碰现有 VM 数据。
- 两个 PowerShell 脚本通过 PowerShell 7.6.7 语法解析。更新器在隔离文件系统中通过 12 个模拟测试：初次安装、无变化跳过、仅代码变化、requirements 变化、Recorder 变化、哈希错误、端口占用、pip 失败、失败重试、Recorder 失败、Recorder 重试、最终无变化跳过；另检查旧发行文件移除且本地 recordings 数据保留。
- 模拟测试使用真实更新器，替代了 Python/pip、Recorder 安装进程、robocopy 和 Windows 端口查询；不代表已在 Windows 执行真实安装。
- 实际 Windows PowerShell 5.1 执行、Windows host.lan 解析/UNC 读取、D/E/F/G 盘符、依赖安装及 /probe 尚未在 VM 验证，按 README 手动完成。

本次仅新增 docker/runtime11 下 Dockerfile、build.py、Update-Runtime.ps1、Install-WaaHook.ps1、README.md、verify.py 以及此报告/build.log/validation.log。未改仓库既有文件、Agent/Recorder 源文件、Windows Base Image、vmanager:1.0 或已有容器/数据。验证容器均为本次新建，验证完毕已清理；没有启动或进入 Windows VM。
