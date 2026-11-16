#!/usr/bin/env bash
# SEKA WEB development installer â explicitly NOT production-ready.
# Installs only /opt/seka-web-dev, /var/lib/seka-web-dev, a dedicated user
# and the seka-web-dev systemd unit. Does NOT touch PostgreSQL or Samba.
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin

REPO='player5/TrashReleases'
# Immutable source snapshot (source files, no binary artifacts in GitHub).
REF='ef20ef59eb9cdb5290dd2929e78c54aa2d0b6124'
APP='/opt/seka-web-dev/current'
DATA='/var/lib/seka-web-dev'
UNIT='/etc/systemd/system/seka-web-dev.service'
SERVICE='seka-web-dev.service'
PORT="${SEKA_WEB_PORT:-547}"
HOST="${SEKA_WEB_HOST:-0.0.0.0}"

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ "$PORT" =~ ^[0-9]+$ ]] || fail "Invalid port: $PORT"
(( 10#$PORT >= 1 && 10#$PORT <= 65535 )) || fail "Port outside 1..65535"
[[ "$HOST" == '0.0.0.0' || "$HOST" == '127.0.0.1' ]] || fail 'SEKA_WEB_HOST must be 0.0.0.0 or 127.0.0.1'
if [[ "${1:-}" == '--check' ]]; then
  echo "Installer syntax and parameters OK. Source $REPO at $REF, host $HOST, port $PORT."
  exit 0
fi
[[ $# -eq 0 ]] || fail 'Usage: bash install.sh [--check]'
(( EUID == 0 )) || fail 'Run through sudo: sudo bash /tmp/seka-web-install.sh'
[[ -f /etc/os-release ]] || fail 'Unsupported OS (expected Ubuntu Linux)'
. /etc/os-release
[[ "$ID" == 'ubuntu' ]] || fail 'This installer has been designed for Ubuntu only'
for cmd in curl tar systemctl id useradd chown chmod mktemp; do command -v "$cmd" >/dev/null 2>&1 || fail "Missing required tool: $cmd"; done
if [[ -d "$APP" || -f "$UNIT" ]]; then
  fail "Existing installation detected ($APP or $UNIT); refusing to overwrite. Inspect 'systemctl status $SERVICE'"
fi
if command -v ss >/dev/null 2>&1 && ss -H -ltn "sport = :$PORT" | grep -q .; then
  fail "Port $PORT is occupied; choose SEKA_WEB_PORT=<another-port>"
fi

WORK="$(mktemp -d)"
cleanup() { rm -rf -- "$WORK"; }
trap cleanup EXIT
printf 'Downloading pinned SEKA WEB DEV source from public GitHub...\n'
curl -fSL --retry 3 --connect-timeout 20 --max-time 180 \
  "https://codeload.github.com/$REPO/tar.gz/$REF" -o "$WORK/source.tar.gz" \
  || fail 'Public GitHub download failed; no files installed'
mkdir -p "$WORK/src"
tar -xzf "$WORK/source.tar.gz" --strip-components=1 -C "$WORK/src" \
  || fail 'Invalid downloaded source archive'
for p in 'app/seka_web/core.py' 'app/seka_web/management.py' 'app/seka_web/static/index.html' 'app/requirements.txt'; do
  [[ -s "$WORK/src/$p" ]] || fail "Missing package component: $p"
done

find_python() {
  local p
  for p in /usr/bin/python3.14 /usr/local/bin/python3.14 /usr/bin/python3.13 /usr/local/bin/python3.13 /usr/bin/python3.12 /usr/local/bin/python3.12 /usr/bin/python3.11 /usr/local/bin/python3.11 /usr/bin/python3 /usr/local/bin/python3; do
    if [[ -x "$p" ]] && "$p" -I -c 'import sys,struct; assert (3,11) <= sys.version_info[:2] <= (3,14); assert struct.calcsize("P") == 8' >/dev/null 2>&1; then
      printf '%s\n' "$p"; return 0
    fi
  done
  return 1
}
PY="$(find_python)" || fail '64-bit Python 3.11â3.14 not found; no system Python replaced'
echo "Python selected: $($PY --version 2>&1), at $PY"

# Ensure venv is present. Only install the matching Ubuntu venv package if needed.
if ! "$PY" -m venv "$WORK/venv-probe" >/dev/null 2>"$WORK/venv-error"; then
  echo 'Python venv unavailable. Installing the matching Ubuntu python3-venv package...'
  if ! command -v apt-get >/dev/null; then cat "$WORK/venv-error" >&2; fail 'apt-get unavailable'; fi
  DEBIAN_FRONTEND=noninteractive apt-get update -qq || fail 'apt-get update failed'
  PY_VER="$($PY -c 'import sys; print(str(sys.version_info.major)+"."+str(sys.version_info.minor))')"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "python${PY_VER}-venv" || fail 'Python venv package installation failed'
  "$PY" -m venv "$WORK/venv-probe" || fail 'Python virtualenv creation failed'
fi
rm -rf "$WORK/venv-probe"

# New installation: do not overwrite an existing app or database.
mkdir -p "$(dirname "$APP")" "$DATA"
NEW_APP=0
cleanup_partial() {
  local code=$?
  if (( code != 0 )) && (( NEW_APP == 1 )) && [[ ! -f "$UNIT" ]]; then
    rm -rf -- "$APP"
    echo 'Partial application installation removed. Existing services unchanged.' >&2
  fi
  cleanup
}
trap cleanup_partial EXIT
[[ ! -e "$APP" ]] || fail 'Installation target exists, aborting'
mkdir -p "$APP"
NEW_APP=1
cp -a "$WORK/src/app/." "$APP/"
"$PY" -m venv "$APP/.venv" || fail 'Failed to create application venv'
"$APP/.venv/bin/python" -m pip install --disable-pip-version-check --no-input -r "$APP/requirements.txt" \
  || fail 'Python dependencies could not be installed'
PYTHONPATH="$APP" "$APP/.venv/bin/python" -c 'import fastapi, uvicorn, multipart; from seka_web.core import app' \
  || fail 'Installed app failed Python import check'
if ! id -u sekawebdev >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin sekawebdev || fail 'User creation failed'
fi
chown -R sekawebdev:sekawebdev "$APP" "$DATA"
chmod 750 "$DATA"

cat > "$UNIT" <<UNITFILE
[Unit]
Description=SEKA WEB Development (SQLite prototype)
After=network.target
[Service]
Type=simple
User=sekawebdev
Group=sekawebdev
WorkingDirectory=$APP
Environment=SEKA_WEB_DATA=$DATA
Environment=SEKA_WEB_MODULES=$APP/modules
ExecStart=$APP/.venv/bin/python -m uvicorn seka_web.core:app --host $HOST --port $PORT
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$DATA
[Install]
WantedBy=multi-user.target
UNITFILE
chmod 644 "$UNIT"
systemctl daemon-reload
systemctl enable --now "$SERVICE" || fail 'Could not enable/start systemd unit; check journalctl -u seka-web-dev'
READY=0
for i in {1..30}; do
  if curl -fsS --connect-timeout 1 --max-time 2 "http://127.0.0.1:$PORT/health" -o "$WORK/health.json" >/dev/null 2>&1; then
    READY=1; break
  fi
  sleep 1
done
if (( READY != 1 )); then
  systemctl --no-pager status "$SERVICE" || true
  fail 'SEKA WEB did not answer /health. Unit remains installed for diagnosis; not claiming success.'
fi

PASSWORD="$($APP/.venv/bin/python -c 'import secrets;print(secrets.token_urlsafe(24))')"
BOOTJSON="$($APP/.venv/bin/python -c 'import json,sys;print(json.dumps({"username":"admin","password":sys.argv[1]}))' "$PASSWORD")"
HTTP="$(curl -sS --connect-timeout 2 --max-time 10 -o "$WORK/bootstrap-result.json" -w '%{http_code}' \
  -H 'Content-Type: application/json' -X POST --data "$BOOTJSON" "http://127.0.0.1:$PORT/api/bootstrap")" \
  || fail 'Local admin bootstrap request failed'
[[ "$HTTP" == 200 ]] || fail "Admin bootstrap returned HTTP $HTTP; service running but admin setup requires diagnosis"
CREDENTIALS="$DATA/initial-admin.txt"
printf 'SEKA WEB DEV initial login\nURL: http://SERVER_IP:%s/\nUsername: admin\nPassword: %s\n' "$PORT" "$PASSWORD" > "$CREDENTIALS"
chown root:root "$CREDENTIALS"
chmod 600 "$CREDENTIALS"
NEW_APP=0
printf '\nSEKA WEB DEVELOPMENT installed and running.\nURL: http://SERVER_IP:%s/\nUser: admin\nPassword: %s\n\n' "$PORT" "$PASSWORD"
printf 'Service: systemctl status seka-web-dev\nCredential backup: sudo cat %s\n' "$CREDENTIALS"
printf 'WARNING: SQLite development prototype without TLS; keep inside trusted test LAN.\n'
