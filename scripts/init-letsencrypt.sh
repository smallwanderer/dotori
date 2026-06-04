#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f .env ]]; then
  echo ".env file is required."
  exit 1
fi

set -a
source .env
set +a

DOMAIN="${LETSENCRYPT_DOMAIN:-}"
EMAIL="${LETSENCRYPT_EMAIL:-}"

if [[ -z "$DOMAIN" || "$DOMAIN" == "localhost" ]]; then
  echo "Set LETSENCRYPT_DOMAIN to the real domain in .env."
  exit 1
fi

if [[ -z "$EMAIL" ]]; then
  echo "Set LETSENCRYPT_EMAIL in .env."
  exit 1
fi

if [[ "${1:-}" == "--staging" ]]; then
  STAGING_ARG="--staging"
else
  STAGING_ARG=""
fi

CERT_DIR="data/certbot/conf/live/$DOMAIN"
ARCHIVE_DIR="data/certbot/conf/archive/$DOMAIN"
RENEWAL_CONF="data/certbot/conf/renewal/$DOMAIN.conf"
WEBROOT_DIR="data/certbot/www"
DUMMY_CERT_CREATED=0
mkdir -p "$WEBROOT_DIR/.well-known/acme-challenge"

create_dummy_cert() {
  echo "Creating a temporary self-signed certificate for nginx startup."
  mkdir -p "$CERT_DIR"
  docker compose -f docker-compose.certbot.yml run --rm --entrypoint openssl certbot req -x509 -nodes -newkey rsa:2048 -days 1 \
    -keyout "/etc/letsencrypt/live/$DOMAIN/privkey.pem" \
    -out "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" \
    -subj "/CN=$DOMAIN"
}

restore_dummy_cert_on_failure() {
  if [[ "$DUMMY_CERT_CREATED" == "1" && ! -f "$CERT_DIR/fullchain.pem" ]]; then
    create_dummy_cert
  fi
}

trap restore_dummy_cert_on_failure EXIT

if [[ -d "$CERT_DIR" && ! -f "$RENEWAL_CONF" && -f "$CERT_DIR/fullchain.pem" && -f "$CERT_DIR/privkey.pem" ]]; then
  echo "Found a temporary certificate directory without Certbot renewal metadata."
  DUMMY_CERT_CREATED=1
elif [[ ! -f "$CERT_DIR/fullchain.pem" || ! -f "$CERT_DIR/privkey.pem" ]]; then
  create_dummy_cert
  DUMMY_CERT_CREATED=1
fi

docker compose up -d --force-recreate nginx

if [[ "$DUMMY_CERT_CREATED" == "1" ]]; then
  rm -rf "$CERT_DIR" "$ARCHIVE_DIR" "$RENEWAL_CONF"
fi

echo "Requesting Let's Encrypt certificate for $DOMAIN."
docker compose -f docker-compose.certbot.yml run --rm certbot certonly --webroot \
  --webroot-path /var/www/certbot \
  --email "$EMAIL" \
  --agree-tos \
  --no-eff-email \
  --force-renewal \
  $STAGING_ARG \
  -d "$DOMAIN"

docker compose exec nginx nginx -s reload

trap - EXIT
echo "Done. Verify with: curl -I https://$DOMAIN/"
