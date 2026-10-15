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
    # Read the controller token locally; it never enters shell history or argv.
    python3 - <<'PY'
import json
from pathlib import Path
from urllib.request import Request, urlopen

token = Path('/var/lib/zerotier-one/authtoken.secret').read_text().strip()
base = 'http://127.0.0.1:9993'

def api(method, path):
    with urlopen(Request(base+path, method=method,
                         headers={'X-ZT1-AUTH': token}), timeout=10) as r:
        raw = r.read()
        return json.loads(raw) if raw else None

# First collect, then delete only portal-tagged networks.
ids = api('GET', '/controller/network')
targets = []
for nwid in ids:
    network = api('GET', '/controller/network/'+nwid)
    if str(network.get('name', '')).startswith('game-room:'):
        targets.append(nwid)
print(f'Removing {len(targets)} game-room:* networks')
for nwid in targets:
    api('DELETE', '/controller/network/'+nwid)
    print('Deleted network:', nwid)
PY
fi

systemctl disable --now zerotier-game.service 2>/dev/null || true
rm -f /etc/systemd/system/zerotier-game.service
rm -f /etc/default/zerotier-game
rm -rf /opt/zerotier-game
systemctl daemon-reload
systemctl reset-failed zerotier-game.service 2>/dev/null || true

cat <<'EOF'
ZeroTier Game Room Portal completely removed (service, code, config).
ZeroTier Controller, existing networks (unless --purge-rooms was used),
WireGuard, Caddy and their state were preserved.

Note: Caddy's existing zero.salingnh.com site was NOT edited; if it still
points to 127.0.0.1:9394, it will return 502 until replaced or disabled.
EOF