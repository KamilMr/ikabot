import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ikabot.bridge import (
    BridgeError,
    BridgeHTTPServer,
    BridgeRequestHandler,
    BridgeState,
    _normalize_request_body,
    _replace_action_request_for_get,
    _parse_upstream_json,
    _safe_response_headers,
    _typed_resource_id,
    _validate_bind_host,
)


class FakeSession:
    logged = True

    def __init__(self):
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        return "fake-html"


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

    def test_typed_resource_id_decodes_numeric_ids(self):
        self.assertEqual(_typed_resource_id("/city/12345", "city"), "12345")
        self.assertEqual(_typed_resource_id("/island/54321", "island"), "54321")
        self.assertIsNone(_typed_resource_id("/city/12345", "island"))

    def test_typed_resource_id_rejects_non_numeric_ids(self):
        with self.assertRaises(BridgeError) as ctx:
            _typed_resource_id("/city/not-a-number", "city")

        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "invalid_request")

    def test_parse_upstream_json_maps_parser_failures(self):
        with self.assertRaises(BridgeError) as ctx:
            _parse_upstream_json(lambda: (_ for _ in ()).throw(AttributeError("missing data")), "city", "123")

        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(ctx.exception.code, "upstream_parse_failed")
        self.assertEqual(ctx.exception.details["resource"], "city")
        self.assertEqual(ctx.exception.details["id"], "123")

    def test_city_endpoint_fetches_and_parses_authenticated_request(self):
        import ikabot.helpers.getJson as get_json

        original_get_city = get_json.getCity
        get_json.getCity = lambda html: {"id": "12345", "name": "Capital", "html": html}
        session = FakeSession()
        server = self._start_server(session)
        try:
            response = self._request_json(server, "/city/12345")

            self.assertEqual(response["ok"], True)
            self.assertEqual(response["data"]["name"], "Capital")
            self.assertEqual(
                session.paths,
                ["view=city&cityId=12345&backgroundView=city&currentCityId=12345&ajax=1"],
            )
        finally:
            get_json.getCity = original_get_city
            server.shutdown()
            server.server_close()

    def test_island_endpoint_requires_bearer_token(self):
        server = self._start_server(FakeSession())
        try:
            with self.assertRaises(HTTPError) as ctx:
                self._request_json(server, "/island/54321", token=None)

            self.assertEqual(ctx.exception.code, 401)
            ctx.exception.close()
        finally:
            server.shutdown()
            server.server_close()

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

    def _start_server(self, session):
        state = BridgeState("test-token")
        state.session = session
        server = BridgeHTTPServer(("127.0.0.1", 0), BridgeRequestHandler, state)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def _request_json(self, server, path, token="test-token"):
        host, port = server.server_address[:2]
        request = Request(f"http://{host}:{port}{path}")
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
