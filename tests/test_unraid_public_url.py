"""Validation tests for LAN and reverse-proxy public URLs."""

import unittest

from gpu_tuner.server import public_url_host


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


if __name__ == "__main__":
    unittest.main()
