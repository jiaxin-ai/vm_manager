# VM Manager 专用镜像
# 以现有镜像为基础，只替换 Samba 共享的配置脚本，其余部分与已验证的镜像完全一致。
FROM julyai/mywinarena:v1

# 4 个共享：Data(/shared)、Upstream(/upstream，只读)、Project(/project)、Recordings(/recordings)
# 不再向共享目录写 readme.txt，也不再修改目录权限
COPY --chmod=755 samba.sh /run/samba.sh

# 恢复 dockur 的默认入口（VM Manager 会覆盖它，这里只是让镜像单独使用时也能正常启动）
ENTRYPOINT ["/usr/bin/tini", "-s", "/run/entry.sh"]
