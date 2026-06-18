import unittest

from ikabot.bridge import (
    BridgeError,
    _normalize_request_body,
    _replace_action_request_for_get,
    _safe_response_headers,
    _validate_bind_host,
)


class BridgeTests(unittest.TestCase):
    def test_normalize_request_body_accepts_documented_shape(self):
        request = _normalize_request_body(
            {
                "method": "post",
                "path": "action=Example&ajax=1",
                "params": {"currentCityId": 12345},
                "form": {"actionRequest": "REQUESTID", "enabled": True},
                "options": {"ignoreExpire": True, "noIndex": False, "noQuery": False, "rawResponse": False},
            }
        )

        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], "action=Example&ajax=1")
        self.assertEqual(request["params"], {"currentCityId": "12345"})
        self.assertEqual(request["form"], {"actionRequest": "REQUESTID", "enabled": "True"})
        self.assertTrue(request["options"]["ignoreExpire"])

    def test_normalize_request_body_rejects_absolute_url(self):
        with self.assertRaises(BridgeError) as ctx:
            _normalize_request_body({"method": "GET", "path": "https://example.com/"})

        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "invalid_request")

    def test_normalize_request_body_rejects_get_form(self):
        with self.assertRaises(BridgeError):
            _normalize_request_body({"method": "GET", "path": "view=city", "form": {"x": "y"}})

    def test_replace_action_request_for_get_uses_session_token(self):
        class FakeSession:
            def _Session__token(self):
                return "fresh-token"

        path, params = _replace_action_request_for_get(
            FakeSession(),
            "view=city&actionRequest=REQUESTID",
            {"actionRequest": "REQUESTID", "other": "unchanged"},
        )

        self.assertEqual(path, "view=city&actionRequest=fresh-token")
        self.assertEqual(params, {"actionRequest": "fresh-token", "other": "unchanged"})

    def test_safe_response_headers_removes_secrets(self):
        headers = _safe_response_headers(
            {
                "Content-Type": "text/html",
                "Set-Cookie": "secret=1",
                "Cookie": "secret=1",
                "Authorization": "Bearer secret",
                "Proxy-Authorization": "Basic secret",
            }
        )

        self.assertEqual(headers, {"Content-Type": "text/html"})

    def test_validate_bind_host_rejects_non_loopback(self):
        with self.assertRaises(SystemExit):
            _validate_bind_host("0.0.0.0")


if __name__ == "__main__":
    unittest.main()
