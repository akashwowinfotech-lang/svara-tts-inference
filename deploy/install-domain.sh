#!/usr/bin/env bash
# Run once as root after reviewing the adjacent Nginx and systemd files.
set -euo pipefail

domain=api.tts.ganas.ai
project_dir=/home/wowbharat/WOWBHARAT/TTS/upstream-svara
site_path=/etc/nginx/sites-available/$domain
enabled_path=/etc/nginx/sites-enabled/$domain
unit_path=/etc/systemd/system/speech-api.service

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer with sudo." >&2
  exit 1
fi
if [[ ! -s $project_dir/.secrets/admin-login.json ]]; then
  echo "Dashboard login is not configured." >&2
  exit 1
fi
if [[ ! -x $project_dir/.venv/bin/uvicorn ]]; then
  echo "The speech virtual environment is missing." >&2
  exit 1
fi
resolved=$(getent ahostsv4 "$domain" | awk 'NR==1 {print $1}')
if [[ $resolved != 103.183.80.247 ]]; then
  echo "DNS has changed ($resolved); check the server IP before installing." >&2
  exit 1
fi

install -m 0644 "$project_dir/deploy/speech-api.service" "$unit_path"
systemctl daemon-reload

# Move the current manually launched instance into the managed service.
if ! systemctl is-active --quiet speech-api.service; then
  manual_pid=$(ss -H -ltnp '( sport = :8088 )' | sed -n 's/.*users:(("uvicorn",pid=\([0-9]*\).*/\1/p' | head -1)
  if [[ -n $manual_pid ]]; then
    command_line=$(tr '\0' ' ' < "/proc/$manual_pid/cmdline")
    if [[ $command_line != *"$project_dir/.venv/bin/uvicorn server:app --app-dir api"* ]]; then
      echo "Port 8088 belongs to an unexpected process; refusing to stop it." >&2
      exit 1
    fi
    mapfile -t engine_pids < <(pgrep -P "$manual_pid" || true)
    kill -TERM "$manual_pid"
    for _ in {1..30}; do
      if ! kill -0 "$manual_pid" 2>/dev/null; then break; fi
      sleep 1
    done
    for engine_pid in "${engine_pids[@]}"; do
      if kill -0 "$engine_pid" 2>/dev/null; then
        kill -TERM "$engine_pid"
      fi
    done
  fi
fi

systemctl enable --now speech-api.service
for _ in {1..120}; do
  if curl -fsS --max-time 2 http://127.0.0.1:8088/health >/dev/null; then break; fi
  sleep 1
done
curl -fsS --max-time 5 http://127.0.0.1:8088/health >/dev/null

if [[ ! -e $site_path ]]; then
  install -m 0644 "$project_dir/deploy/$domain.nginx.conf" "$site_path"
fi
if [[ ! -e $enabled_path && ! -L $enabled_path ]]; then
  ln -s "$site_path" "$enabled_path"
fi
if ! nginx -t; then
  echo "Nginx configuration failed validation. Inspect $site_path." >&2
  exit 1
fi
systemctl reload nginx

certbot_args=(--nginx --non-interactive --agree-tos --redirect -d "$domain")
if [[ -n ${CERTBOT_EMAIL:-} ]]; then
  certbot_args+=(--email "$CERTBOT_EMAIL")
else
  certbot_args+=(--register-unsafely-without-email)
fi
certbot "${certbot_args[@]}"
nginx -t
systemctl reload nginx
systemctl enable --now certbot.timer
systemctl enable --now nginx.service

curl -fsS --max-time 15 "https://$domain/health"
printf '\nInstallation complete. Dashboard: https://%s/admin\n' "$domain"
