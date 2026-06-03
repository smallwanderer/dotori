#!/usr/bin/env bash
set -euo pipefail

docker compose -f docker-compose.certbot.yml run --rm certbot renew --webroot --webroot-path /var/www/certbot
docker compose exec nginx nginx -s reload
