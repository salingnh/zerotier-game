#!/usr/bin/env bash
# Completely remove this portal. Shared ZeroTier/WireGuard/Caddy services stay intact.
# Add --purge-rooms to explicitly delete networks created by this portal.
set -Eeuo pipefail

[[ $EUID -eq 0 ]] || { echo 'Run: sudo bash uninstall.sh' >&2; exit 1; }
PURGE=false
case "${1:-}" in
    "") ;;
    --purge-rooms) PURGE=true ;;
    *) echo 'Usage: sudo bash uninstall.sh [--purge-rooms]' >&2; exit 2 ;;
esac

if "$PURGE"; then
    [[ -t 0 ]] || { echo 'Refusing --purge-rooms without an interactive terminal.' >&2; exit 1; }
    echo 'WARNING: This will DELETE all controller networks named game-room:*.'
    echo 'Existing players will be disconnected. Other networks are preserved.'
    read -r -p 'Type DELETE to confirm: ' answer
    [[ "$answer" == DELETE ]] || { echo 'Cancelled (nothing removed).'; exit 1; }
    # Read either protected .env or legacy local token using app's own API client.
    SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
    python3 - "$SCRIPT_DIR" <<'PY'
import os
import sys
from pathlib import Path

script_dir = Path(sys.argv[1])
installed_dir = Path('/opt/zerotier-game')
sys.path.insert(0, str(installed_dir if (installed_dir / 'app.py').is_file() else script_dir))
installed_env = Path('/etc/zerotier-game/controller.env')
source_env = script_dir / '.env'
if installed_env.is_file():
    os.environ['ZT_GAME_CONTROLLER_ENV_FILE'] = str(installed_env)
elif source_env.is_file():
    os.environ['ZT_GAME_CONTROLLER_ENV_FILE'] = str(source_env)
os.environ['ZT_GAME_TOKEN_FILE'] = '/var/lib/zerotier-one/authtoken.secret'

import app

# First collect, then delete only portal-tagged networks.
ids = app.controller_request('GET', '/controller/network')
targets = []
for nwid in ids:
    network = app.controller_request('GET', '/controller/network/' + nwid)
    if str(network.get('name', '')).startswith('game-room:'):
        targets.append(nwid)
print(f'Removing {len(targets)} game-room:* networks')
for nwid in targets:
    app.controller_request('DELETE', '/controller/network/' + nwid)
    print('Deleted network:', nwid)
PY
fi

systemctl disable --now zerotier-game.service 2>/dev/null || true
rm -f /etc/systemd/system/zerotier-game.service
rm -f /etc/default/zerotier-game
rm -rf /etc/zerotier-game
rm -rf /opt/zerotier-game
systemctl daemon-reload
systemctl reset-failed zerotier-game.service 2>/dev/null || true

cat <<'EOF'
ZeroTier Game Room Portal completely removed (service, code, config).
Installed controller.env was removed; source checkout .env is untouched.
ZeroTier Controller, existing networks (unless --purge-rooms was used),
WireGuard, Caddy and their state were preserved.

Note: Caddy's existing zero.salingnh.com site was NOT edited; if it still
points to 127.0.0.1:9394, it will return 502 until replaced or disabled.
EOF
