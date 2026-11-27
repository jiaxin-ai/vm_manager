#!/bin/bash
# 容器启动脚本（task.md 9.4）。由 VM Manager 以只读方式挂载到 /opt/vm-manager/start_windows.sh。
echo '1. Construct basic env...'
if [ -f /storage/data.qcow2 ]; then
  echo 'Existing disk found, skip initialization.'
else
  mkdir -p /storage || exit 1
  cp -n /base_storage/windows* /storage/ || { echo 'ERROR: failed to copy windows* files'; exit 1; }
  rm -f /storage/windows*.vars
  qemu-img create -f qcow2 -F "$BASE_FORMAT" -b "/base_storage/$BASE_IMG" /storage/data.qcow2 \
    || { echo 'ERROR: failed to create data.qcow2'; exit 1; }
fi
# The images ship Samba's state and log folders owned by uid 1000. smbd runs as root and refuses them
# ("invalid ownership on directory /run/samba/msg.lock"), so Windows would get no D/E/F/G and no Runtime share.
chown -R 0:0 /run/samba /var/log/samba /var/lib/samba /var/cache/samba 2>/dev/null || true
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
