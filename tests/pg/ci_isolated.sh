#!/usr/bin/env bash
# PostgreSQL 比較を隔離網の公式イメージで実行する（CI 用。gate ジョブと test ジョブの両方から呼ぶ）。
# 必要な環境変数: SIGIL, POSTGRES_IMAGE, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT。
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
set -euo pipefail
network="sigil-db-pg-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
container="$network"
cleanup() {
  docker rm -f -v "$container" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT
export POSTGRES_USER=sigil_ci POSTGRES_DB=sigil_ci POSTGRES_PORT=5432
POSTGRES_PASSWORD="$(openssl rand -hex 32)"
export POSTGRES_PASSWORD
echo "::add-mask::$POSTGRES_PASSWORD"
docker pull "$POSTGRES_IMAGE"
docker network create --internal "$network"
docker run -d --name "$container" --network "$network" \
  --env POSTGRES_USER --env POSTGRES_DB --env POSTGRES_PASSWORD \
  --env POSTGRES_HOST_AUTH_METHOD=scram-sha-256 \
  --env 'POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=scram-sha-256' \
  "$POSTGRES_IMAGE" -c password_encryption=scram-sha-256
test "$(docker network inspect --format '{{.Internal}}' "$network")" = true
test "$(docker inspect --format '{{len .NetworkSettings.Networks}}' "$container")" = 1
test "$(docker inspect --format '{{len .HostConfig.PortBindings}}' "$container")" = 0
ready=false
for attempt in {1..60}; do
  if docker exec "$container" pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"; then
    ready=true
    break
  fi
  sleep 1
done
test "$ready" = true
pid="$(docker inspect --format '{{.State.Pid}}' "$container")"
sudo --preserve-env=SIGIL,POSTGRES_USER,POSTGRES_DB,POSTGRES_PASSWORD,POSTGRES_PORT,PATH nsenter --target "$pid" --net -- tests/pg/run.sh
