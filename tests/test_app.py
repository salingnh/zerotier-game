import http.client
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


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
        with patch.object(app, "controller_networks", return_value=networks), \
             patch.object(app, "member_count", return_value=3):
            rooms = app.list_rooms()
        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0]["name"], "AOE")
        self.assertEqual(rooms[0]["members"], 3)

    def test_member_count_counts_registered_not_presence(self):
        with patch.object(app, "controller_request", return_value={"a": 1, "b": 2}):
            self.assertEqual(app.member_count("7375be7d6e000001"), 2)

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

    def test_healthz_and_head(self):
        code, _, data = self.request("HEAD", "/")
        self.assertEqual((code, data), (200, b""))
        code, _, data = self.request("GET", "/healthz")
        self.assertEqual(json.loads(data)["ok"], True)

    def test_list_rooms_api(self):
        with patch.object(app, "list_rooms", return_value=[{"id": "7375be7d6eabcdef", "name": "HL", "members": 2}]):
            code, _, data = self.request("GET", "/api/rooms")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(data)["rooms"][0]["members"], 2)

    def test_post_rejects_cross_origin_and_non_json(self):
        code, _, data = self.request("POST", "/api/rooms", "name=bad", {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(code, 415)
        code, _, data = self.request("POST", "/api/rooms", b'{"name":"bad"}', {
            "Content-Type": "application/json", "Origin": "https://evil.example", "Host": "zero.salingnh.com"
        })
        self.assertEqual(code, 403)

    def test_post_returns_created_network(self):
        with patch.object(app, "create_room", return_value={"id": "7375be7d6e000001", "name": "AOE", "members": 0}):
            code, _, data = self.request("POST", "/api/rooms", b'{"name":"AOE"}', {"Content-Type": "application/json"})
        self.assertEqual(code, 201)
        self.assertEqual(json.loads(data)["room"]["name"], "AOE")


if __name__ == '__main__':
    unittest.main()