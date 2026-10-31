"""Reverse-proxy requests reach the backend over plain HTTP."""

import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from gpu_tuner.password_auth import hash_password
from gpu_tuner.server import make_handler


HOST = "gpu-tuner.example.com"
ORIGIN = f"https://{HOST}"
PASSWORD = "Example-Strong-Password-123"


class ReverseProxyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = Path(temp.name)
        (config / "password.hash").write_text(
            hash_password(PASSWORD) + "\n", encoding="ascii"
        )
        config_patch = patch(
            "gpu_tuner.server.config_dir", return_value=str(config)
        )
        config_patch.start()
        self.addCleanup(config_patch.stop)
        env_patch = patch.dict(os.environ, {
            "GPU_TUNER_PUBLIC_URL": ORIGIN,
            "GPU_TUNER_LAN_IP": "",
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(h=None, token="desktop-test-token", port=8765),
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path="/api/login", host=HOST, origin=ORIGIN,
                cookie=None, body=None):
        headers = {"Host": host, "Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        if cookie:
            headers["Cookie"] = cookie
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        try:
            conn.request("POST", path, json.dumps(
                {"password": PASSWORD} if body is None else body
            ), headers)
            response = conn.getresponse()
            response.read()
            return response.status, dict(response.getheaders())
        finally:
            conn.close()

    def test_https_login_sets_secure_cookie(self):
        status, headers = self.request()
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"]
        for attribute in ("; Secure", "; HttpOnly", "; SameSite=Strict"):
            self.assertIn(attribute, cookie)

    def test_logout_clears_secure_cookie(self):
        status, headers = self.request()
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, headers = self.request(
            "/api/logout", cookie=cookie, body={}
        )
        self.assertEqual(status, 200)
        self.assertIn("; Secure", headers["Set-Cookie"])
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

    def test_wrong_scheme_origin_rejected(self):
        self.assertEqual(self.request(origin=f"http://{HOST}")[0], 403)

    def test_unapproved_origin_rejected(self):
        self.assertEqual(
            self.request(origin="https://attacker.example.com")[0], 403
        )

    def test_missing_origin_rejected(self):
        self.assertEqual(self.request(origin=None)[0], 403)

    def test_unapproved_host_rejected(self):
        self.assertEqual(
            self.request(host="attacker.example.com")[0], 421
        )

    def test_direct_local_http_login_still_works(self):
        status, headers = self.request(
            host="localhost:8765", origin="http://localhost:8765"
        )
        self.assertEqual(status, 200)
        self.assertNotIn("; Secure", headers["Set-Cookie"])


if __name__ == "__main__":
    unittest.main()
