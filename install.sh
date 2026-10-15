#!/usr/bin/env bash
# Install/upgrade the tiny ZeroTier game-room portal without touching ZeroTier,
# Caddy, WireGuard or any controller network.
set -Eeuo pipefail

APP_NAME="zerotier-game"
APP_DIR="/opt/${APP_NAME}"
UNIT="/etc/systemd/system/${APP_NAME}.service"
CONFIG="/etc/default/${APP_NAME}"
SECRET_DIR="/etc/${APP_NAME}"
SECRET_ENV="${SECRET_DIR}/controller.env"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ENV="${SCRIPT_DIR}/.env"

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail "Run: sudo bash install.sh"
[[ -f "${SCRIPT_DIR}/app.py" ]] || fail "app.py not found next to install.sh"
[[ -f "${SCRIPT_DIR}/uninstall.sh" ]] || fail "uninstall.sh not found next to install.sh"
for cmd in python3 systemctl install ss curl; do
    command -v "$cmd" >/dev/null || fail "Required command missing: $cmd"
done
# A root-only .env copied into /etc/zerotier-game takes precedence over
# legacy local ZeroTier authtoken.secret. The .env is passed to systemd using
# LoadCredential, not EnvironmentFile: the controller key stays out of the
# process environment, unit file, command-line arguments and systemctl show.
[[ ! -L "$SECRET_DIR" && ! -L "$SECRET_ENV" ]] || fail "Refusing symlinked secret path"
CREDENTIAL_MODE=local
if [[ -f "$SECRET_ENV" ]]; then
    [[ -s "$SECRET_ENV" ]] || fail "$SECRET_ENV is empty"
    CREDENTIAL_MODE=env
elif [[ -e "$SECRET_ENV" ]]; then
    fail "$SECRET_ENV is not a regular file"
elif [[ -f "$SOURCE_ENV" ]]; then
    [[ ! -L "$SOURCE_ENV" ]] || fail "Refusing symlinked source .env"
    [[ -s "$SOURCE_ENV" ]] || fail ".env is empty"
    CREDENTIAL_MODE=env
fi

if [[ "$CREDENTIAL_MODE" == local ]]; then
    command -v zerotier-cli >/dev/null || fail "Install ZeroTier One before this portal, or configure .env"
    [[ -s /var/lib/zerotier-one/authtoken.secret ]] || fail "ZeroTier token is missing. Configure .env or install ZeroTier One"
    systemctl is-active --quiet zerotier-one || fail "zerotier-one service is not running"
fi

# An old zerotier-edge may still be using 9394. Do not remove it implicitly.
if systemctl is-active --quiet zerotier-edge.service 2>/dev/null; then
    fail "zerotier-edge occupies 9394. Stop it first: sudo systemctl disable --now zerotier-edge"
fi
if ss -H -ltn '( sport = :9394 )' | grep -q . && ! systemctl is-active --quiet "$APP_NAME.service" 2>/dev/null; then
    fail "TCP port 9394 is in use. Stop the other service before installing."
fi

python3 - "${SCRIPT_DIR}/app.py" <<'PYCOMPILE'
import sys
from pathlib import Path
p = Path(sys.argv[1])
compile(p.read_text(encoding="utf-8"), str(p), "exec")
PYCOMPILE
install -d -o root -g root -m 0755 "$APP_DIR"
install -o root -g root -m 0644 "${SCRIPT_DIR}/app.py" "$APP_DIR/app.py"
install -o root -g root -m 0755 "${SCRIPT_DIR}/uninstall.sh" "$APP_DIR/uninstall.sh"

UNIT_DEPENDENCIES=""
if [[ "$CREDENTIAL_MODE" == env ]]; then
    install -d -o root -g root -m 0700 "$SECRET_DIR"
    # Preserve an installed key on upgrades. For rotation, edit installed file.
    if [[ ! -e "$SECRET_ENV" ]]; then
        install -o root -g root -m 0600 "$SOURCE_ENV" "$SECRET_ENV"
    fi
    chown root:root "$SECRET_ENV"
    chmod 0600 "$SECRET_ENV"
    CREDENTIAL_LINE="LoadCredential=controller-env:$SECRET_ENV"
else
    CREDENTIAL_LINE="LoadCredential=zerotier-token:/var/lib/zerotier-one/authtoken.secret"
    UNIT_DEPENDENCIES=$'After=zerotier-one.service\nRequires=zerotier-one.service'
fi

if [[ ! -f "$CONFIG" ]]; then
    cat > "$CONFIG" <<'EOF'
# Local-only. Caddy can continue proxying to 127.0.0.1:9394.
ZT_GAME_PORT=9394
ZT_GAME_MAX_ROOMS=64
ZT_GAME_MAX_CREATES_PER_HOUR=10
ZT_GAME_ONLINE_WINDOW_MINUTES=5
EOF
    chmod 0644 "$CONFIG"
fi

cat > "$UNIT" <<EOF
[Unit]
Description=ZeroTier Game Room Portal (localhost only)
Wants=network-online.target
After=network-online.target
$UNIT_DEPENDENCIES

[Service]
Type=simple
DynamicUser=yes
Environment=PYTHONDONTWRITEBYTECODE=1
EnvironmentFile=-/etc/default/zerotier-game
$CREDENTIAL_LINE
ExecStart=/usr/bin/python3 -B /opt/zerotier-game/app.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
AmbientCapabilities=
MemoryMax=96M
TasksMax=32

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$UNIT"

systemctl daemon-reload
systemctl enable --now "$APP_NAME.service"
systemctl restart "$APP_NAME.service"

ready=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl --noproxy '*' -fsS --max-time 2 http://127.0.0.1:9394/healthz >/dev/null; then
        ready=1
        break
    fi
    sleep 1
done
if [[ "$ready" != 1 ]]; then
    systemctl --no-pager --full status "$APP_NAME.service" || true
    fail "Service did not pass health check. Inspect: journalctl -u $APP_NAME -n 50"
fi

# healthz is HTTP-only. Also verify controller token, TLS and access.
if ! curl --noproxy '*' -fsS --max-time 20 \
    http://127.0.0.1:9394/api/rooms >/dev/null; then
    fail "Controller API authentication/connectivity failed. Check $SECRET_ENV or local token and journalctl -u $APP_NAME"
fi

cat <<'EOF'
Installation successful.
  Local:     http://127.0.0.1:9394
  Check:     curl http://127.0.0.1:9394/api/rooms
  Service:   systemctl status zerotier-game
  Existing Caddy reverse_proxy 127.0.0.1:9394: unchanged
  Controller credential: protected root-only .env or local token

SECURITY: The application has NO built-in user authentication.
Before exposing it via zero.salingnh.com, enforce Caddy Basic Auth
(or equivalent) on the whole website, including /api/rooms.
ZeroTier, WireGuard, Caddy and their data were not modified.
EOF
