cd ~/projects/github_coding_agent

docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  down --remove-orphans

docker network rm cognimoss-local_default 2>/dev/null || true

docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  up -d
