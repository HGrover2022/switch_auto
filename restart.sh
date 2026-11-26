#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PORT=50914
PY=".venv/bin/python"
LOCK=".restart.lock"

exec 9>"$LOCK"
flock -n 9 || { echo "另一个 restart.sh 正在运行，退出"; exit 1; }

[ -x "$PY" ] || { echo "缺少 $PY，请先运行 bash setup.sh"; exit 1; }
"$PY" -B -c "import ast, pathlib; [ast.parse(pathlib.Path(f).read_text(encoding='utf-8')) for f in ('switched_core.py', 'web_app.py')]"

# 检查现有服务是否在跑任务
if curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null | grep -q '"running":true'; then
  echo "当前有签到任务正在运行，拒绝重启"
  exit 1
fi

# 通过 /proc 精确匹配本项目进程
CUR_DIR="$(pwd -P)"
PIDS=()
for p in /proc/[0-9]*; do
  pid="${p#/proc/}"
  [ -r "$p/cmdline" ] || continue
  cwd="$(readlink -f "$p/cwd" 2>/dev/null || true)"
  cmd="$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null || true)"
  if [ "$cwd" = "$CUR_DIR" ] && echo "$cmd" | grep -q "web_app.py"; then
    PIDS+=("$pid")
  fi
done

for pid in "${PIDS[@]:-}"; do
  [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null || true
done

for _ in $(seq 1 15); do
  if ! ss -lntp 2>/dev/null | grep -q ":${PORT} "; then
    break
  fi
  sleep 1
done

if ss -lntp 2>/dev/null | grep -q ":${PORT} "; then
  echo "端口 ${PORT} 未释放，放弃启动"
  exit 1
fi

nohup "$PY" -B -u web_app.py >> web_app.log 2>&1 < /dev/null 9>&- &
echo $! > web_app.pid

for _ in $(seq 1 15); do
  if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    echo "启动成功 PID=$(cat web_app.pid) 端口=${PORT}"
    exit 0
  fi
  sleep 1
done

echo "启动超时，请检查 web_app.log"
exit 1
