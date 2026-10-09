#!/usr/bin/env bash
# 服务器侧一键部署：加载本地 docker 镜像包 + 启动 compose
# 用法：把 memory-hand-v0.1.0.tar.gz / docker-compose.yml / .env / deploy.sh 放到同一目录，
#      然后执行：bash deploy.sh
set -euo pipefail

IMAGE_TAR="${1:-memory-hand-v0.1.0.tar.gz}"
COMPOSE_FILE="docker-compose.yml"
ENV_FILE=".env"
PORT=8080

cd "$(dirname "$0")"

# --- preflight ---
[[ -f "$IMAGE_TAR"   ]] || { echo "image tar not found: $IMAGE_TAR";   exit 1; }
[[ -f "$COMPOSE_FILE" ]] || { echo "compose file not found: $COMPOSE_FILE"; exit 1; }
[[ -f "$ENV_FILE"     ]] || { echo "env file not found: $ENV_FILE";     exit 1; }
command -v docker >/dev/null || { echo "docker not installed"; exit 1; }

# docker compose v2 vs v1
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "neither 'docker compose' nor 'docker-compose' available"; exit 1
fi

# --- 1. stop old container (idempotent) ---
if docker ps -a --format '{{.Names}}' | grep -q '^memory-hand$'; then
  echo "[1/4] stopping existing container..."
  $DC down --remove-orphans || true
else
  echo "[1/4] no existing container"
fi

# --- 2. load image tar ---
echo "[2/4] loading image from $IMAGE_TAR ..."
docker load -i "$IMAGE_TAR"

# --- 3. start ---
echo "[3/4] starting service via $DC ..."
mkdir -p ./data_store
$DC up -d

# --- 4. health check ---
echo "[4/4] waiting for /health on port $PORT ..."
for i in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "OK: memory-hand is live at http://<public-ip>:$PORT"
    docker ps --filter name=memory-hand
    exit 0
  fi
  sleep 1
done

echo "health check timeout; showing last 50 log lines:"
$DC logs --tail 50
exit 1
