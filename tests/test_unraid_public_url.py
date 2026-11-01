"""Validation tests for LAN and reverse-proxy public URLs."""

import http.client
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from gpu_tuner.server import make_handler, public_url_host


class PublicURLTests(unittest.TestCase):
    def test_valid_urls(self):
        cases = (
            ("http://192.168.1.10:8765", "192.168.1.10:8765"),
            ("http://192.168.1.10:18765", "192.168.1.10:18765"),
            ("http://localhost:8765", "localhost:8765"),
            ("https://gpu-tuner.example.com", "gpu-tuner.example.com"),
            ("https://gpu-tuner.example.com/", "gpu-tuner.example.com"),
            ("https://gpu-tuner.example.com:443", "gpu-tuner.example.com"),
            ("https://gpu-tuner.example.com:8443", "gpu-tuner.example.com:8443"),
            ("http://gpu-tuner.example.com", "gpu-tuner.example.com"),
            ("http://gpu-tuner.example.com:80", "gpu-tuner.example.com"),
            ("https://GPU-TUNER.EXAMPLE.COM", "gpu-tuner.example.com"),
            ("https://192.168.1.10", "192.168.1.10"),
            ("http://[::1]:8765", "[::1]:8765"),
        )
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(public_url_host(url), expected)

    def test_invalid_urls(self):
        cases = (
            "ftp://gpu-tuner.example.com",
            "//gpu-tuner.example.com",
            "https://",
            "https://user:pass@gpu-tuner.example.com",
            "https://gpu-tuner.example.com/admin",
            "https://gpu-tuner.example.com/?token=secret",
            "https://gpu-tuner.example.com?",
            "https://gpu-tuner.example.com#",
            "https://gpu-tuner.example.com:0",
            "https://gpu-tuner.example.com:99999",
            "https://gpu-tuner.example.com:",
            "https://gpu-tuner.example.com:abc",
            "https://gpu-tuner.example.com:0443",
            "https://bad_host.example.com",
            "https://-bad.example.com",
            "https://bad..example.com",
            "https://*.example.com",
            "https://gpu-tuner.example.com\n",
            "https://gpu-tuner.example.com\r\nInjected: value",
            "https://gpu-tuner.example.com\\evil",
        )
        for url in cases:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    public_url_host(url)

    def test_empty_configuration(self):
        self.assertIsNone(public_url_host(""))


class NoPublicURLHandlerTests(unittest.TestCase):
    """The handler must work when GPU_TUNER_PUBLIC_URL is unset."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config_patch = patch(
            "gpu_tuner.server.config_dir", return_value=temp.name
        )
        config_patch.start()
        self.addCleanup(config_patch.stop)
        env_patch = patch.dict(os.environ, {
            "GPU_TUNER_PUBLIC_URL": "",
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

    def test_request_routes_without_public_url(self):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        try:
            conn.request("GET", "/", headers={"Host": "localhost:8765"})
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 401)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
