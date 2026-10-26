#!/usr/bin/env bash
set -Eeuo pipefail

# VM Manager 专用版本，与原脚本的区别：
#   1. 固定发布 4 个共享：Data(/shared)、Upstream(/upstream，只读)、Project(/project)、Recordings(/recordings)
#   2. 不再向共享目录写入 readme.txt，也不再修改目录权限（chmod 777）
#      Samba 使用 force user = root 读写，不需要放开目录权限；
#      冻结用的 chattr +i 对 root 同样生效，所以冻结后依然不可写。

: "${SAMBA:="Y"}"

[[ "$SAMBA" == [Nn]* ]] && return 0
[[ "$NETWORK" == [Nn]* ]] && return 0

hostname="host.lan"
interface="dockerbridge"

if [[ "$DHCP" == [Yy1]* ]]; then
  hostname="$IP"
  interface="$VM_NET_DEV"
fi

addShare() {
  local dir="$1"
  local name="$2"
  local comment="$3"
  local writable="$4"

  # 目录已存在时（宿主机挂载进来的情况）不做任何修改
  mkdir -p "$dir" || return 1

  {      echo ""
          echo "[$name]"
          echo "    path = $dir"
          echo "    comment = $comment"
          echo "    writable = $writable"
          echo "    guest ok = yes"
          echo "    guest only = yes"
          echo "    force user = root"
          echo "    force group = root"
  } >> "/etc/samba/smb.conf"

  return 0
}

{      echo "[global]"
        echo "    server string = Dockur"
        echo "    netbios name = $hostname"
        echo "    workgroup = WORKGROUP"
        echo "    interfaces = $interface"
        echo "    bind interfaces only = yes"
        echo "    security = user"
        echo "    guest account = nobody"
        echo "    map to guest = Bad User"
        echo "    server min protocol = NT1"
        echo ""
        echo "    # disable printing services"
        echo "    load printers = no"
        echo "    printing = bsd"
        echo "    printcap name = /dev/null"
        echo "    disable spoolss = yes"
} > "/etc/samba/smb.conf"

addShare "/shared"     "Data"       "Task workspace"      "yes" || error "Failed to create share Data!"
addShare "/upstream"   "Upstream"   "Upstream workspaces" "no"  || error "Failed to create share Upstream!"
addShare "/project"    "Project"    "Project shared space" "yes" || error "Failed to create share Project!"
addShare "/recordings" "Recordings" "Recordings"          "yes" || error "Failed to create share Recordings!"

if ! smbd; then
  error "Samba daemon failed to start!"
  smbd -i --debug-stdout || true
fi

if [[ "${BOOT_MODE:-}" == "windows_legacy" ]]; then
  # Enable NetBIOS on Windows 7 and lower
  if ! nmbd; then
    error "NetBIOS daemon failed to start!"
    nmbd -i --debug-stdout || true
  fi
else
  # Enable Web Service Discovery on Vista and up
  wsdd -i "$interface" -p -n "$hostname" &
  echo "$!" > /var/run/wsdd.pid
fi

return 0
