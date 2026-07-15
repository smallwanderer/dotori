#!/usr/bin/env bash
set -euo pipefail

PROVIDER_ENV="data/config/network_access/provider.env"

if [[ ! -f "$PROVIDER_ENV" ]]; then
  echo "$PROVIDER_ENV is required."
  exit 1
fi

docker compose -f docker-compose.certbot.yml run --rm certbot renew --webroot --webroot-path /var/www/certbot
docker compose --env-file .env --env-file "$PROVIDER_ENV" --profile direct-https exec nginx nginx -s reload
