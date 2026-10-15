#!/usr/bin/env python3
"""Tiny, dependency-free ZeroTier game room portal.

Only listen on loopback. Put authenticated HTTPS (e.g. Caddy Basic Auth)
in front of the portal before exposing it to the Internet.
"""

import ipaddress
import json
import os
import secrets
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

API_ROOT = "http://127.0.0.1:9993"
ROOM_PREFIX = "game-room:"
BIND_HOST = "127.0.0.1"  # Never bind the unauthenticated API publicly.
BIND_PORT = int(os.environ.get("ZT_GAME_PORT", "9394"))
MAX_ROOMS = int(os.environ.get("ZT_GAME_MAX_ROOMS", "64"))
MAX_CREATES_PER_HOUR = int(os.environ.get("ZT_GAME_MAX_CREATES_PER_HOUR", "10"))
ONLINE_WINDOW_MINUTES = int(os.environ.get("ZT_GAME_ONLINE_WINDOW_MINUTES", "5"))

CREATE_LOCK = threading.Lock()
RECENT_CREATES = deque()


class ControllerError(Exception):
    pass


def token_path():
    credentials = os.environ.get("CREDENTIALS_DIRECTORY")
    if credentials:
        return Path(credentials) / "zerotier-token"
    # Explicitly provided only for local tests/development.
    override = os.environ.get("ZT_GAME_TOKEN_FILE")
    if override:
        return Path(override)
    raise RuntimeError("Missing systemd credential: zerotier-token")


def controller_request(method, endpoint, payload=None):
    token = token_path().read_text(encoding="utf-8").strip()
    if not token:
        raise ControllerError("Empty ZeroTier controller token")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"X-ZT1-AUTH": token}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(API_ROOT + endpoint, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=8) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise ControllerError(f"ZeroTier Controller API unavailable ({type(exc).__name__})") from exc


def controller_networks():
    ids = controller_request("GET", "/controller/network")
    if not isinstance(ids, list):
        raise ControllerError("Unexpected controller network listing")
    networks = []
    for network_id in ids:
        if not isinstance(network_id, str) or len(network_id) != 16:
            raise ControllerError("Invalid network id returned by controller")
        network = controller_request("GET", "/controller/network/" + network_id)
        if not isinstance(network, dict):
            raise ControllerError("Unexpected controller network data")
        networks.append(network)
    return networks


def is_room(network):
    return str(network.get("name", "")).startswith(ROOM_PREFIX)


def normalized_node_id(value):
    """Return a valid ZeroTier node ID, normalized for set comparisons."""
    if not isinstance(value, str):
        return None
    value = value.lower()
    if len(value) != 10 or any(c not in "0123456789abcdef" for c in value):
        return None
    return value


def active_peer_addresses(now_ms=None):
    """Peers from which this controller node received traffic recently.

    This is a *global*, best-effort presence signal from /peer, not a
    network-specific connection state. A member active in another network
    can therefore also appear online in a game room they previously joined.
    """
    peers = controller_request("GET", "/peer")
    if not isinstance(peers, list):
        raise ControllerError("Unexpected ZeroTier peer listing")

    if now_ms is None:
        now_ms = int(time.time() * 1000)
    oldest_ms = now_ms - ONLINE_WINDOW_MINUTES * 60_000
    active = set()
    for peer in peers:
        if not isinstance(peer, dict) or peer.get("role") != "LEAF":
            continue
        node_id = normalized_node_id(peer.get("address"))
        paths = peer.get("paths")
        if node_id is None or not isinstance(paths, list):
            continue
        for path in paths:
            if not isinstance(path, dict) or path.get("active") is not True or path.get("expired") is True:
                continue
            received_ms = path.get("lastReceive")
            # Use inbound activity, not lastSend, so outbound attempts alone
            # cannot make an unreachable player appear online.
            if type(received_ms) is int and oldest_ms <= received_ms <= now_ms + 5000:
                active.add(node_id)
                break
    return active


def online_member_count(network_id, active_addresses):
    members = controller_request("GET", f"/controller/network/{network_id}/member")
    if not isinstance(members, (dict, list)):
        raise ControllerError("Unexpected member listing")

    # The self-hosted controller normally returns {node_id: revision}.
    # Also accept the alternate list form for compatibility.
    entries = members.keys() if isinstance(members, dict) else members
    member_ids = set()
    for entry in entries:
        node_id = normalized_node_id(entry.get("address") if isinstance(entry, dict) else entry)
        if node_id is not None:
            member_ids.add(node_id)
    return len(member_ids & active_addresses)


def list_rooms():
    networks = [network for network in controller_networks() if is_room(network)]
    if not networks:
        return []
    # Query the controller node's global peer table only ONCE per refresh.
    active_addresses = active_peer_addresses()
    result = []
    for network in networks:
        name = str(network.get("name", ""))[len(ROOM_PREFIX):]
        result.append({
            "id": network["id"],
            "name": name,
            "online": online_member_count(network["id"], active_addresses),
            "created_at": network.get("creationTime", 0),
        })
    result.sort(key=lambda x: x["created_at"] or 0, reverse=True)
    return result


def used_ipv4_networks(networks):
    reserved = []
    for network in networks:
        for route in network.get("routes", []):
            target = route.get("target") if isinstance(route, dict) else None
            try:
                parsed = ipaddress.ip_network(target, strict=False)
            except (ValueError, TypeError):
                continue
            if parsed.version == 4 and parsed.prefixlen != 0:
                reserved.append(parsed)
        for pool in network.get("ipAssignmentPools", []):
            if not isinstance(pool, dict):
                continue
            try:
                start = ipaddress.ip_address(pool["ipRangeStart"])
                end = ipaddress.ip_address(pool["ipRangeEnd"])
            except (ValueError, KeyError, TypeError):
                continue
            if start.version == 4 and end.version == 4 and start <= end:
                reserved.extend(ipaddress.summarize_address_range(start, end))
    return reserved


def select_subnet(networks):
    reserved = used_ipv4_networks(networks)
    for third_octet in range(1, 255):
        subnet = ipaddress.ip_network(f"10.230.{third_octet}.0/24")
        if all(not subnet.overlaps(existing) for existing in reserved):
            return subnet
    raise ValueError("Đã hết dải mạng cho phòng chơi")


def clean_room_name(value):
    if not isinstance(value, str):
        raise ValueError("Tên phòng phải là chuỗi văn bản")
    name = " ".join(value.split())
    if not 1 <= len(name) <= 60 or any(ord(c) < 32 for c in name):
        raise ValueError("Tên phòng phải có từ 1 đến 60 ký tự")
    return name


def create_room(value):
    name = clean_room_name(value)
    # Serializes address assignment and applies one server-wide abuse limit.
    with CREATE_LOCK:
        now = time.monotonic()
        while RECENT_CREATES and RECENT_CREATES[0] <= now - 3600:
            RECENT_CREATES.popleft()
        if len(RECENT_CREATES) >= MAX_CREATES_PER_HOUR:
            raise ValueError("Đã đạt giới hạn tạo phòng trong một giờ")

        networks = controller_networks()
        if sum(map(is_room, networks)) >= MAX_ROOMS:
            raise ValueError("Đã đạt giới hạn số phòng. Liên hệ quản trị viên.")
        subnet = select_subnet(networks)
        node_id = controller_request("GET", "/status")["address"]
        if not isinstance(node_id, str) or len(node_id) != 10:
            raise ControllerError("Invalid controller node ID")

        # A two-stage create is compatible with the documented controller API.
        created = controller_request(
            "POST", f"/controller/network/{node_id}______", {}
        )
        network_id = created.get("id") if isinstance(created, dict) else None
        if not isinstance(network_id, str) or len(network_id) != 16:
            raise ControllerError("Controller did not return a network ID")

        config = {
            "name": ROOM_PREFIX + name,
            # Anyone with the network ID can join; do not use for sensitive LANs.
            "private": False,
            "enableBroadcast": True,
            "multicastLimit": 32,
            "ipAssignmentPools": [{
                "ipRangeStart": str(subnet.network_address + 10),
                "ipRangeEnd": str(subnet.network_address + 250),
            }],
            "routes": [{"target": str(subnet), "via": None}],
            "v4AssignMode": {"zt": True},
            "v6AssignMode": {"6plane": False, "rfc4193": False, "zt": False},
            "rules": [{"type": "ACTION_ACCEPT"}],
        }
        try:
            controller_request("POST", f"/controller/network/{network_id}", config)
        except Exception:
            # Best-effort rollback; never delete unrelated networks.
            try:
                controller_request("DELETE", f"/controller/network/{network_id}")
            except ControllerError:
                pass
            raise
        RECENT_CREATES.append(now)
        return {"id": network_id, "name": name, "online": 0}


PAGE = r'''<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Game LAN Rooms</title>
<style nonce="__NONCE__">
:root{color-scheme:dark;font-family:system-ui,-apple-system,sans-serif}
*{box-sizing:border-box}body{max-width:940px;margin:48px auto;padding:0 20px;background:#101725;color:#edf1f9}
h1{margin:0 0 6px;font-size:28px}.sub{color:#a9b6c9;margin:0 0 28px}
form{display:flex;gap:10px;margin-bottom:16px}input{flex:1;min-width:0;background:#1b2638;border:1px solid #344359;border-radius:9px;color:white;padding:12px;font:inherit}
button{background:#377aee;color:white;border:0;border-radius:9px;padding:11px 15px;cursor:pointer;font:inherit;font-weight:600}
button:disabled{opacity:.5;cursor:wait}.copy{background:#344359;padding:7px 12px;font-size:13px}
.wrap{overflow-x:auto;border:1px solid #344359;border-radius:10px}table{width:100%;border-collapse:collapse}
th,td{padding:14px 12px;border-bottom:1px solid #344359;text-align:left}
tr:last-child td{border:0}th{font-size:12px;color:#b0bbcc}code{font-size:13px;white-space:nowrap}
.num{text-align:center}#notice{min-height:32px;color:#a7e4b2}#notice.error{color:#ff9c9c}
small{color:#a9b6c9}a{color:#8eb8ff}@media(max-width:540px){body{margin:24px auto}form{flex-direction:column}}
</style>
</head>
<body>
<h1>Game LAN Rooms</h1>
<p class="sub">Tạo phòng ZeroTier chỉ với một tên. Gửi Network ID cho bạn bè để cùng tham gia.</p>
<form id="roomForm"><input id="name" placeholder="Tên phòng (ví dụ: AOE tối nay)" maxlength="60" required autocomplete="off"><button id="create">Tạo phòng</button></form>
<div id="notice" role="status" aria-live="polite"></div>
<div class="wrap"><table><thead><tr><th>Network ID</th><th>Tên phòng</th><th class="num">Online (ước tính)</th><th></th></tr></thead><tbody id="rooms"><tr><td colspan="4">Đang tải...</td></tr></tbody></table></div>
<p><small>“Online” là ước tính: thiết bị đã join phòng và có lưu lượng đến controller trong __WINDOW_MINUTES__ phút gần đây. Không bảo đảm đang chơi game; một node có thể hoạt động ở mạng ZeroTier khác. Phòng public: bất kỳ ai có Network ID đều có thể tham gia.</small></p>
<script nonce="__NONCE__">
const body=document.getElementById('rooms'),notice=document.getElementById('notice'),form=document.getElementById('roomForm'),input=document.getElementById('name'),btn=document.getElementById('create');
function message(s,err=false){notice.textContent=s;notice.className=err?'error':''}
function cell(s){const t=document.createElement('td');t.textContent=s;return t}
async function refresh(){try{const r=await fetch('/api/rooms',{cache:'no-store'}),j=await r.json();if(!r.ok)throw Error(j.error||'Không đọc được danh sách phòng');body.replaceChildren();for(const room of j.rooms){const tr=document.createElement('tr'),id=cell(room.id),name=cell(room.name),count=cell(String(room.online)),actions=document.createElement('td'),copy=document.createElement('button');id.style.whiteSpace='nowrap';count.className='num';copy.className='copy';copy.textContent='Copy ID';copy.onclick=async()=>{try{await navigator.clipboard.writeText(room.id);copy.textContent='Đã copy';setTimeout(()=>copy.textContent='Copy ID',1200)}catch{message('Không copy được; hãy chọn Network ID và copy thủ công.',true)}};actions.append(copy);tr.append(id,name,count,actions);body.append(tr)}if(!j.rooms.length){const tr=document.createElement('tr'),td=cell('Chưa có phòng nào. Hãy tạo phòng đầu tiên.');td.colSpan=4;tr.append(td);body.append(tr)}}catch(e){message(e.message,true)}}
form.addEventListener('submit',async e=>{e.preventDefault();btn.disabled=true;message('Đang tạo phòng...');try{const r=await fetch('/api/rooms',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:input.value})}),j=await r.json();if(!r.ok)throw Error(j.error||'Không tạo được phòng');input.value='';message('Đã tạo phòng '+j.room.id);await refresh()}catch(e){message(e.message,true)}finally{btn.disabled=false}});
refresh();setInterval(refresh,10000);
</script>
</body></html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = "ZTGame/1"
    protocol_version = "HTTP/1.1"

    def headers_common(self, length, mime, status=200, nonce=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if nonce:
            self.send_header("Content-Security-Policy", "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'; connect-src 'self'; script-src 'nonce-" + nonce + "'; style-src 'nonce-" + nonce + "'")
        self.end_headers()

    def json_response(self, obj, status=200, send_body=True):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.headers_common(len(data), "application/json; charset=utf-8", status)
        if send_body:
            self.wfile.write(data)

    def handle_read(self, head_only=False):
        route = urlsplit(self.path).path
        if route == "/healthz":
            return self.json_response({"ok": True}, send_body=not head_only)
        if route == "/":
            nonce = secrets.token_urlsafe(18)
            data = PAGE.replace("__NONCE__", nonce).replace("__WINDOW_MINUTES__", str(ONLINE_WINDOW_MINUTES)).encode("utf-8")
            self.headers_common(len(data), "text/html; charset=utf-8", nonce=nonce)
            if not head_only:
                self.wfile.write(data)
            return
        if route == "/api/rooms":
            try:
                return self.json_response({"rooms": list_rooms()}, send_body=not head_only)
            except Exception:
                return self.json_response({"error": "Không kết nối được ZeroTier Controller"}, 502, send_body=not head_only)
        return self.json_response({"error": "Not found"}, 404, send_body=not head_only)

    def do_GET(self):
        self.handle_read()

    def do_HEAD(self):
        self.handle_read(head_only=True)

    def do_POST(self):
        if urlsplit(self.path).path != "/api/rooms":
            return self.json_response({"error": "Not found"}, 404)
        if self.headers.get("Sec-Fetch-Site", "") == "cross-site":
            return self.json_response({"error": "Forbidden"}, 403)
        origin = self.headers.get("Origin")
        if origin and (urlsplit(origin).netloc.lower() != self.headers.get("Host", "").lower()):
            return self.json_response({"error": "Forbidden"}, 403)
        if self.headers.get_content_type() != "application/json":
            return self.json_response({"error": "Expected application/json"}, 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 1024:
                return self.json_response({"error": "Request quá lớn hoặc không hợp lệ"}, 400)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                return self.json_response({"error": "Invalid JSON"}, 400)
            room = create_room(body.get("name"))
            return self.json_response({"room": room}, 201)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return self.json_response({"error": str(exc)}, 400)
        except Exception:
            return self.json_response({"error": "Không tạo được phòng; kiểm tra ZeroTier Controller"}, 502)

    def log_message(self, *args):
        # Do not generate access logs on a small free-tier disk.
        pass


if __name__ == "__main__":
    if not (1 <= BIND_PORT <= 65535 and 1 <= MAX_ROOMS <= 254 and 1 <= MAX_CREATES_PER_HOUR <= 100 and 1 <= ONLINE_WINDOW_MINUTES <= 60):
        raise SystemExit("Invalid application settings")
    # Verify credential before accepting requests.
    if not token_path().read_text(encoding="utf-8").strip():
        raise SystemExit("ZeroTier token is empty")
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    print(f"ZT Game Room UI listening on http://{BIND_HOST}:{BIND_PORT}", flush=True)
    server.serve_forever()
