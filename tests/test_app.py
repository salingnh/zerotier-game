import http.client
import json
import os
import sys
import threading
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class ControllerCredentialsTests(unittest.TestCase):
    def test_reads_controller_token_from_systemd_credential_env(self):
        with TemporaryDirectory() as root:
            Path(root, "controller-env").write_text(
                '# Never log this secret\n'
                'ZT_GAME_CONTROLLER_URL="https://zt-controller.example.com:9993"\n'
                "ZT_GAME_CONTROLLER_TOKEN='example_token_123'\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": root}, clear=True):
                self.assertEqual(
                    app.controller_settings(),
                    ("https://zt-controller.example.com:9993", "example_token_123"),
                )

    def test_legacy_systemd_authtoken_still_supported(self):
        with TemporaryDirectory() as root:
            Path(root, "zerotier-token").write_text("local_secret\n", encoding="utf-8")
            with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": root}, clear=True):
                self.assertEqual(
                    app.controller_settings(),
                    ("http://127.0.0.1:9993", "local_secret"),
                )

    def test_explicit_env_file_is_usable_for_admin_purge(self):
        with TemporaryDirectory() as root:
            p = Path(root, "configured.env")
            p.write_text("ZT_GAME_CONTROLLER_TOKEN=admin_token_123\n", encoding="utf-8")
            with patch.dict(os.environ, {"ZT_GAME_CONTROLLER_ENV_FILE": str(p)}, clear=True):
                self.assertEqual(
                    app.controller_settings(),
                    ("http://127.0.0.1:9993", "admin_token_123"),
                )

    def test_empty_env_token_does_not_fall_back_to_local_secret(self):
        with TemporaryDirectory() as root:
            Path(root, "controller-env").write_text("ZT_GAME_CONTROLLER_TOKEN=\n", encoding="utf-8")
            Path(root, "zerotier-token").write_text("local_secret\n", encoding="utf-8")
            with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": root}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "ZT_GAME_CONTROLLER_TOKEN is empty"):
                    app.controller_settings()

    def test_bad_env_files_are_rejected(self):
        cases = (
            "ZT_GAME_CONTROLLER_TOKEN=x\nZT_GAME_CONTROLLER_TOKEN=y\n",
            "export ZT_GAME_CONTROLLER_TOKEN=foo\n",
            "SOME_OTHER_KEY=abc\n",
            "ZT_GAME_CONTROLLER_TOKEN abc\n",
        )
        for content in cases:
            with self.subTest(content=content), TemporaryDirectory() as root:
                p = Path(root, "controller.env")
                p.write_text(content, encoding="utf-8")
                with patch.dict(os.environ, {"ZT_GAME_CONTROLLER_ENV_FILE": str(p)}, clear=True):
                    with self.assertRaises(RuntimeError):
                        app.controller_settings()

    def test_remote_controller_requires_https_and_no_credentials_in_url(self):
        urls = (
            "http://192.0.2.1:9993",
            "https://alice:secret@controller.example.org",
            "https://controller.example.org/other/path",
            "ftp://controller.example.org",
            "https://controller.example.org?test=1",
        )
        for url in urls:
            with self.subTest(url=url), patch.dict(os.environ, {
                    "ZT_GAME_CONTROLLER_URL": url, "ZT_GAME_CONTROLLER_TOKEN": "secret",
                }, clear=True):
                with self.assertRaises(RuntimeError):
                    app.controller_settings()

    def test_controller_request_uses_configured_url_and_token_header(self):
        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self):
                return b'{"id":"sample"}'

        with patch.object(app, "controller_settings",
                          return_value=("https://controller.example.org:9993", "private_token")), \
             patch.object(app, "urlopen", return_value=FakeResponse()) as opener:
            self.assertEqual(app.controller_request("GET", "/controller/network"), {"id": "sample"})
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://controller.example.org:9993/controller/network")
        self.assertEqual(request.get_header("X-zt1-auth"), "private_token")


class RoomTests(unittest.TestCase):
    def setUp(self):
        app.RECENT_CREATES.clear()

    def test_room_names_are_validated(self):
        self.assertEqual(app.clean_room_name("  AOE   tối nay  "), "AOE tối nay")
        for name in ("", "   ", "A" * 61, "test\x00room", None, 123):
            with self.subTest(name=name), self.assertRaises(ValueError):
                app.clean_room_name(name)

    def test_allocator_avoids_existing_routes_and_pools(self):
        networks = [{"routes": [{"target": "10.230.1.0/24"}],
                     "ipAssignmentPools": [{"ipRangeStart": "10.230.2.10", "ipRangeEnd": "10.230.2.250"}]}]
        self.assertEqual(str(app.select_subnet(networks)), "10.230.3.0/24")

    def test_only_portal_rooms_are_listed(self):
        networks = [
            {"id": "7375be7d6e000001", "name": "game-room:AOE", "creationTime": 12},
            {"id": "7375be7d6e000002", "name": "sali-game-lan", "creationTime": 13},
        ]
        active = {"1111111111"}
        with patch.object(app, "controller_networks", return_value=networks), \
             patch.object(app, "active_peer_addresses", return_value=active) as peer_call, \
             patch.object(app, "online_member_count", return_value=1) as counter:
            rooms = app.list_rooms()
        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0]["name"], "AOE")
        self.assertEqual(rooms[0]["online"], 1)
        peer_call.assert_called_once_with()
        counter.assert_called_once_with("7375be7d6e000001", active)

    def test_no_rooms_skips_global_peer_query(self):
        with patch.object(app, "controller_networks", return_value=[]), \
             patch.object(app, "active_peer_addresses") as peer_call:
            self.assertEqual(app.list_rooms(), [])
        peer_call.assert_not_called()

    def test_active_peers_require_recent_inbound_traffic_and_active_path(self):
        now = 1_700_000_000_000
        fresh = {"active": True, "expired": False, "lastReceive": now - 20_000}
        peers = [
            {"address": "1111111111", "role": "LEAF", "paths": [fresh]},
            {"address": "1111111111", "role": "LEAF", "paths": [fresh]},  # deduplicate
            {"address": "2222222222", "role": "LEAF", "paths": [
                {"active": False, "expired": True, "lastReceive": now - 1000}]},
            {"address": "3333333333", "role": "LEAF", "paths": [
                {"active": True, "expired": False,
                 "lastReceive": now - app.ONLINE_WINDOW_MINUTES * 60_000 - 1}]},
            {"address": "4444444444", "role": "PLANET", "paths": [fresh]},
            {"address": "5555555555", "role": "LEAF", "paths": [
                {"active": True, "expired": False, "lastReceive": 0, "lastSend": now}]},
            {"address": "6666666666", "role": "LEAF", "paths": [
                {"active": True, "expired": False, "lastReceive": now + 30_000}]},
            {"address": "7777777777", "role": "LEAF", "paths": [
                {"active": False, "expired": True, "lastReceive": now - 10_000},
                {"active": True, "expired": False, "lastReceive": now - 1000}]},
            {"address": "not-a-node", "role": "LEAF", "paths": [fresh]},
            {"address": "8888888888", "role": "LEAF", "paths": None},
            None,
        ]
        with patch.object(app, "controller_request", return_value=peers) as request:
            active = app.active_peer_addresses(now_ms=now)
        self.assertEqual(active, {"1111111111", "7777777777"})
        request.assert_called_once_with("GET", "/peer")

    def test_active_peers_reject_invalid_api_response(self):
        with patch.object(app, "controller_request", return_value={}):
            with self.assertRaises(app.ControllerError):
                app.active_peer_addresses()

    def test_online_member_count_intersects_room_members(self):
        members = {"1111111111": 1, "2222222222": 2, "3333333333": 3}
        with patch.object(app, "controller_request", return_value=members):
            self.assertEqual(
                app.online_member_count("7375be7d6e000001", {"1111111111", "3333333333", "9999999999"}), 2)

    def test_online_member_count_accepts_list_shape_and_deduplicates(self):
        members = ["1111111111", {"address": "1111111111"}, {"address": "2222222222"}, None]
        with patch.object(app, "controller_request", return_value=members):
            self.assertEqual(
                app.online_member_count("7375be7d6e000001", {"1111111111", "9999999999"}), 1)

    def test_online_member_count_rejects_bad_member_response(self):
        with patch.object(app, "controller_request", return_value=None):
            with self.assertRaises(app.ControllerError):
                app.online_member_count("7375be7d6e000001", set())

    def test_room_presence_is_scoped_to_registered_members(self):
        networks = [
            {"id": "7375be7d6e000001", "name": "game-room:AOE", "creationTime": 12},
            {"id": "7375be7d6e000002", "name": "game-room:HL", "creationTime": 13},
        ]
        records = {
            "/controller/network/7375be7d6e000001/member": {"1111111111": 1, "2222222222": 1},
            "/controller/network/7375be7d6e000002/member": {"2222222222": 1, "3333333333": 1},
        }
        with patch.object(app, "controller_networks", return_value=networks), \
             patch.object(app, "active_peer_addresses", return_value={"1111111111", "3333333333"}) as peers, \
             patch.object(app, "controller_request", side_effect=lambda _method, url: records[url]):
            result = {room["name"]: room["online"] for room in app.list_rooms()}
        self.assertEqual(result, {"AOE": 1, "HL": 1})
        peers.assert_called_once()

    def test_create_room_public_broadcast_and_route(self):
        observed = []
        def fake(method, endpoint, payload=None):
            observed.append((method, endpoint, payload))
            if endpoint == "/status":
                return {"address": "7375be7d6e"}
            if method == "POST" and endpoint.endswith("______"):
                return {"id": "7375be7d6eabc123"}
            return {"id": "7375be7d6eabc123"}
        with patch.object(app, "controller_networks", return_value=[]), \
             patch.object(app, "controller_request", side_effect=fake):
            room = app.create_room(" AOΕ test ")
        self.assertEqual(room["id"], "7375be7d6eabc123")
        config = observed[-1][2]
        self.assertEqual(config["name"], "game-room:AOΕ test")
        self.assertFalse(config["private"])
        self.assertTrue(config["enableBroadcast"])
        self.assertEqual(config["routes"][0]["target"], "10.230.1.0/24")
        self.assertEqual(config["rules"], [{"type": "ACTION_ACCEPT"}])

    def test_rollback_if_controller_configuration_fails(self):
        observed = []
        def fake(method, endpoint, payload=None):
            observed.append((method, endpoint))
            if endpoint == "/status":
                return {"address": "7375be7d6e"}
            if endpoint.endswith("______"):
                return {"id": "7375be7d6eabc123"}
            if method == "POST":
                raise app.ControllerError("failed")
            return {}
        with patch.object(app, "controller_networks", return_value=[]), \
             patch.object(app, "controller_request", side_effect=fake):
            with self.assertRaises(app.ControllerError):
                app.create_room("bad")
        self.assertIn(("DELETE", "/controller/network/7375be7d6eabc123"), observed)

    def test_request_limit(self):
        for _ in range(app.MAX_CREATES_PER_HOUR):
            app.RECENT_CREATES.append(app.time.monotonic())
        with self.assertRaisesRegex(ValueError, "giới hạn"):
            app.create_room("again")


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, data=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.request(method, path, data, headers or {})
        r = conn.getresponse()
        raw = r.read()
        meta = dict(r.getheaders())
        code = r.status
        conn.close()
        return code, meta, raw

    def test_page_has_csp_and_no_secrets(self):
        code, headers, data = self.request("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("nonce-", headers["Content-Security-Policy"])
        self.assertNotIn(b"zerotier-token", data)
        self.assertIn(b"Online", data)
        self.assertNotIn(b"__WINDOW_MINUTES__", data)
        self.assertIn(b"https://www.zerotier.com/download/", data)
        self.assertIn(b"id=\"join-guide\"", data)
        self.assertIn(b"sudo zerotier-cli join NETWORK_ID", data)
        self.assertIn(b"join-id", data)
        self.assertIn(b"id=\"join-command\"", data)

    def test_healthz_and_head(self):
        code, _, data = self.request("HEAD", "/")
        self.assertEqual((code, data), (200, b""))
        code, _, data = self.request("GET", "/healthz")
        self.assertEqual(json.loads(data)["ok"], True)

    def test_list_rooms_api(self):
        with patch.object(app, "list_rooms", return_value=[{"id": "7375be7d6eabcdef", "name": "HL", "online": 2}]):
            code, _, data = self.request("GET", "/api/rooms")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(data)["rooms"][0]["online"], 2)

    def test_post_rejects_cross_origin_and_non_json(self):
        code, _, data = self.request("POST", "/api/rooms", "name=bad", {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(code, 415)
        code, _, data = self.request("POST", "/api/rooms", b'{"name":"bad"}', {
            "Content-Type": "application/json", "Origin": "https://evil.example", "Host": "zero.salingnh.com"
        })
        self.assertEqual(code, 403)

    def test_post_returns_created_network(self):
        with patch.object(app, "create_room", return_value={"id": "7375be7d6e000001", "name": "AOE", "online": 0}):
            code, _, data = self.request("POST", "/api/rooms", b'{"name":"AOE"}', {"Content-Type": "application/json"})
        self.assertEqual(code, 201)
        self.assertEqual(json.loads(data)["room"]["name"], "AOE")


if __name__ == '__main__':
    unittest.main()
