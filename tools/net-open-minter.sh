#!/bin/bash
# net-open-minter.sh —— 放行「桥接容器 → 宿主 minter 端口」，并验证包真的能到
# ---------------------------------------------------------------------------
# 什么时候需要它
#   部署后 `python3 tools/compose_wiring_check.py --probe` 报
#   「容器内连不上 minter：host.docker.internal:<port> timed out」
#
# 为什么必须手工放行（不是配置漏了）
#   minter 走 **host 网络**（必须：bridge + NAT 会改写 TCP MSS/指纹 ⇒ CF 判机器人 ⇒
#   `render()` 全部超时），而 ark-compat 在 **bridge 网络** ⇒ 它访问 minter 是
#   **出网到宿主**，受宿主 INPUT 链管辖。
#   而 **Docker 只为它自己 `-p` 映射的端口自动加放行规则**（走 docker-proxy/DNAT），
#   host 网络的服务**不经过 docker-proxy** ⇒ Docker 不会替你加规则 ⇒ 包被 ufw 丢弃。
#   症状与 E2E-AVM-008 的 3 条 429 同族：服务全健康、宿主 curl 通、容器就是连不上。
#
# 做法
#   复用项目自带的 `tools/host_preflight.py`（幂等，按 **docker 桥接接口**放行而不是
#   写死网段，含「容器内实测 + 探测不过自动回滚」），本脚本只负责：
#     取端口 → 调它 → 收尾用 compose_wiring_check --probe 复核。
#
# 用法（**宿主机侧**，需 root）
#   sudo bash net-open-minter.sh                # 自动从 .env / compose 取端口后放行
#   sudo bash net-open-minter.sh --port 8899    # 显式指定
#   sudo bash net-open-minter.sh --check        # 只体检，不改任何规则
#   sudo bash net-open-minter.sh --revert       # 回滚本次放行
#
# 环境变量
#   DEPLOY_DIR  部署目录（默认 /opt/avmdeploy）
#   SRC_DIR     项目源码目录（含 tools/），默认自动探测 DEPLOY_DIR/src*
set -uo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/opt/avmdeploy}"
SRC_DIR="${SRC_DIR:-}"
PORT=""
MODE="apply"

while [ $# -gt 0 ]; do
  case "$1" in
    --port)   PORT="${2:-}"; shift 2 ;;
    --revert|--rollback) MODE="revert"; shift ;;
    --check)  MODE="check"; shift ;;
    --dir)    DEPLOY_DIR="${2:-}"; shift 2 ;;
    -h|--help) /usr/bin/sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（-h 看用法）" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" = "0" ] || { echo "需要 root（改防火墙规则）" >&2; exit 1; }
cd "$DEPLOY_DIR" 2>/dev/null || { echo "找不到部署目录 $DEPLOY_DIR" >&2; exit 1; }

# ---- ① 取 minter 端口：显式 > .env 的 AVM_MINTER_URL > compose 里的 PORT ----
if [ -z "$PORT" ]; then
  PORT=$(/usr/bin/grep -E '^AVM_MINTER_URL=' .env 2>/dev/null \
         | /usr/bin/sed -E 's#^[^=]*=.*:([0-9]{2,5}).*#\1#' | /usr/bin/head -1)
fi
if ! echo "$PORT" | /usr/bin/grep -qE '^[0-9]+$'; then
  # AVM_MINTER_URL 留空 = 回退到 minter 服务自己的 PORT
  PORT=$(/usr/bin/grep -E '^\s*PORT:' docker-compose.yml 2>/dev/null \
         | /usr/bin/grep -oE '[0-9]{4,5}' | /usr/bin/head -1)
fi
echo "minter 端口 = ${PORT:-<探测失败>}"
echo "$PORT" | /usr/bin/grep -qE '^[0-9]+$' || { echo "取不到端口，请用 --port 指定" >&2; exit 1; }

# ---- ② 找源码目录（含 tools/）----
# 源码目录的推断顺序（越靠前越优先）：
#   ① 显式 SRC_DIR
#   ② 本脚本自己就在 <src>/tools/ 里 ⇒ 取它的上级（随源码包分发时走这条，最稳）
#   ③ 部署目录下版本最高的 src*（现场手动解压多个版本时走这条）
#   ④ 部署目录本身
if [ -z "$SRC_DIR" ]; then
  SELF_DIR=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
  if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/host_preflight.py" ]; then
    SRC_DIR=$(dirname "$SELF_DIR")
  fi
fi
if [ -z "$SRC_DIR" ] || [ ! -f "$SRC_DIR/tools/host_preflight.py" ]; then
  SRC_DIR=""
  # ⚠️ 必须显式倒序：现场并存 src027 / src031 时，glob 顺序**不保证**是最新的
  #    （实测会挑中旧的 src027）⇒ sort -r 取版本最高。
  for d in $(ls -d "$DEPLOY_DIR"/src* 2>/dev/null | sort -r) "$DEPLOY_DIR"; do
    [ -f "$d/tools/host_preflight.py" ] && { SRC_DIR="$d"; break; }
  done
fi
PREFLIGHT="${SRC_DIR:-}/tools/host_preflight.py"
WIRING="${SRC_DIR:-}/tools/compose_wiring_check.py"
echo "源码目录   = ${SRC_DIR:-<未找到>}"

# ---- ③ 当前规则与端口占用（体检）----
echo
echo "--- 放行前：ufw 中该端口的规则 ---"
ufw status 2>/dev/null | /usr/bin/grep -E "(^|[[:space:]])${PORT}(/tcp)?[[:space:]]" \
  || echo "  （无 —— 这正是容器连不上的原因）"
echo "--- minter 是否在监听 ---"
ss -tlnp 2>/dev/null | /usr/bin/grep -E ":${PORT} " | /usr/bin/awk '{print "  ", $4}' || echo "  （没在监听）"

if [ "$MODE" = "check" ]; then
  echo
  echo "[check] 只体检，未改动任何规则。"
  exit 0
fi

# ---- ④ 放行 / 回滚（优先复用项目自带的上线助手）----
echo
if [ -f "$PREFLIGHT" ]; then
  if [ "$MODE" = "revert" ]; then
    echo "--- 回滚：$PREFLIGHT --rollback --port $PORT ---"
    python3 "$PREFLIGHT" --rollback --port "$PORT"
  else
    echo "--- 放行：$PREFLIGHT --apply --port $PORT ---"
    python3 "$PREFLIGHT" --apply --port "$PORT"
  fi
  RC=$?
else
  echo "⚠️ 未找到 $PREFLIGHT，改用 ufw 网段放行（等效但不按接口，换地址池需重跑）"
  if [ "$MODE" = "revert" ]; then
    ufw delete allow proto tcp from 172.16.0.0/12 to any port "$PORT" 2>&1 | tail -1
  else
    ufw allow proto tcp from 172.16.0.0/12 to any port "$PORT" 2>&1 | tail -1
  fi
  RC=$?
fi
echo "  退出码 = $RC"

# ---- ⑤ 收尾复核：容器内真实探测「包能到」----
echo
echo "--- 放行后：ufw 规则确认 ---"
ufw status 2>/dev/null | /usr/bin/grep -E "(^|[[:space:]])${PORT}(/tcp)?[[:space:]]" || echo "  （仍无规则？）"

echo
if [ "$MODE" != "revert" ] && [ -f "$WIRING" ]; then
  echo "--- 最终复核：容器内探针（宿主能连 ≠ 容器能连）---"
  python3 "$WIRING" --probe 2>&1 | tail -4
else
  echo "⚠️ 回滚后请自行确认容器内已不可达：python3 ${WIRING:-<src>/tools/compose_wiring_check.py} --probe"
fi

cat <<'NOTE'

安全边界（重要）
  · 只放行 **docker 私有网段 / docker 桥接接口**，不放到公网 —— 验证方法：从机器外部
    `curl -m 5 <公网IP>:<port>` 应当不通。
  · minter 是「过闸能力」的发放口（拿到 token 就能过验证码闸门）⇒ 除防火墙外，
    务必同时设强 `AVM_MINTER_KEY`（两侧同源于该变量）。
  · 有公网 IP 的机器还可把 minter 收到内网：`AVM_MINTER_BIND_HOST`（注意：绑 127.0.0.1
    要求 ark-compat 也走 host 网络，否则 bridge 容器连不上）。
NOTE
