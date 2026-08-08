#!/usr/bin/env bash
set -euo pipefail

PROVIDER_ENV="data/config/network_access/provider.env"

if [[ ! -f "$PROVIDER_ENV" ]]; then
  echo "$PROVIDER_ENV is required. Create and edit the external access configuration first."
  exit 1
fi

set -a
source "$PROVIDER_ENV"
set +a

DOMAIN="${DOTORI_EXTERNAL_DOMAIN:-}"
EMAIL="${DOTORI_CERTIFICATE_EMAIL:-}"

if [[ -z "$DOMAIN" || "$DOMAIN" == "localhost" ]]; then
  echo "Set DOTORI_EXTERNAL_DOMAIN to the real domain in $PROVIDER_ENV."
  exit 1
fi

if [[ -z "$EMAIL" ]]; then
  echo "Set DOTORI_CERTIFICATE_EMAIL in $PROVIDER_ENV."
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
CHALLENGE_PROBE_NAME="certbot-readiness-check"
CHALLENGE_PROBE_FILE="$WEBROOT_DIR/.well-known/acme-challenge/$CHALLENGE_PROBE_NAME"
CHALLENGE_PROBE_CONTENT="certbot-ready"
DUMMY_CERT_CREATED=0
mkdir -p "$WEBROOT_DIR/.well-known/acme-challenge"

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required to verify the nginx ACME challenge endpoint."
  exit 1
fi

create_dummy_cert() {
  echo "Creating a temporary self-signed certificate for nginx startup."
  mkdir -p "$CERT_DIR"
  docker compose -f docker-compose.certbot.yml run --rm --entrypoint openssl certbot req -x509 -nodes -newkey rsa:2048 -days 1 \
    -keyout "/etc/letsencrypt/live/$DOMAIN/privkey.pem" \
    -out "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" \
    -subj "/CN=$DOMAIN"
}

restore_dummy_cert_on_failure() {
  rm -f "$CHALLENGE_PROBE_FILE"

  if [[ "$DUMMY_CERT_CREATED" == "1" && ! -f "$CERT_DIR/fullchain.pem" ]]; then
    create_dummy_cert
  fi
}

wait_for_challenge_endpoint() {
  printf '%s' "$CHALLENGE_PROBE_CONTENT" > "$CHALLENGE_PROBE_FILE"

  echo "Waiting for nginx to serve the ACME challenge endpoint."
  for _ in {1..30}; do
    if [[ "$(curl --silent --show-error --fail --max-time 2 \
      --header "Host: $DOMAIN" \
      "http://127.0.0.1/.well-known/acme-challenge/$CHALLENGE_PROBE_NAME" 2>/dev/null || true)" == "$CHALLENGE_PROBE_CONTENT" ]]; then
      rm -f "$CHALLENGE_PROBE_FILE"
      echo "The ACME challenge endpoint is ready."
      sleep 5
      return
    fi

    sleep 2
  done

  echo "Nginx did not serve the ACME challenge endpoint within 60 seconds."
  echo "Check nginx logs with: docker compose logs nginx"
  exit 1
}

trap restore_dummy_cert_on_failure EXIT

if [[ -d "$CERT_DIR" && ! -f "$RENEWAL_CONF" && -f "$CERT_DIR/fullchain.pem" && -f "$CERT_DIR/privkey.pem" ]]; then
  echo "Found a temporary certificate directory without Certbot renewal metadata."
  DUMMY_CERT_CREATED=1
elif [[ ! -f "$CERT_DIR/fullchain.pem" || ! -f "$CERT_DIR/privkey.pem" ]]; then
  create_dummy_cert
  DUMMY_CERT_CREATED=1
fi

docker compose --env-file .env --env-file "$PROVIDER_ENV" --profile direct-https up -d --force-recreate app nginx
wait_for_challenge_endpoint

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

docker compose --env-file .env --env-file "$PROVIDER_ENV" --profile direct-https exec nginx nginx -s reload

trap - EXIT
echo "Done. Verify with: curl -I https://$DOMAIN/"
