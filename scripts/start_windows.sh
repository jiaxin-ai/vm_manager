#!/bin/bash
# 容器启动脚本（task.md 9.4）。由 VM Manager 以只读方式挂载到 /opt/vm-manager/start_windows.sh。
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
