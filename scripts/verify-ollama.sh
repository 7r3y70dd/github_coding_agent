docker network inspect cognimoss-local_default \
  --format 'bridge={{index .Options "com.docker.network.bridge.name"}} gateway={{(index .IPAM.Config 0).Gateway}}'

ip -br address show br-cognimoss

docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  run --rm --no-deps \
  --entrypoint sh \
  ollama-check -lc '
    curl -sv \
      --connect-timeout 3 \
      --max-time 10 \
      "$OLLAMA_URL/api/tags"
  '
