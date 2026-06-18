#! /usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local HTTP bridge for the hybrid Node.js rewrite.

The bridge owns one Ikabot :class:`Session` instance and exposes a small
localhost-only JSON API. It is intentionally implemented with Python's standard
library so it can run anywhere the existing CLI runs.
"""

import argparse
import datetime as _datetime
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote, urlparse

class _MissingRequestsTimeout(Exception):
    pass


class _MissingRequestsException(Exception):
    pass


try:
    import requests

    REQUEST_TIMEOUT_EXC = requests.exceptions.Timeout
    REQUEST_EXCEPTION_EXC = requests.exceptions.RequestException
except ImportError:  # Allows bridge validation tests to run before dependencies are installed.
    requests = None  # type: ignore[assignment]
    REQUEST_TIMEOUT_EXC = _MissingRequestsTimeout
    REQUEST_EXCEPTION_EXC = _MissingRequestsException

from ikabot.config import actionRequest, ikaFile

BRIDGE_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 0
MAX_JSON_BODY_BYTES = 1024 * 1024
HOP_BY_HOP_OR_SECRET_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "set-cookie",
    "cookie",
    "authorization",
    "host",
}
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class BridgeError(Exception):
    """HTTP-aware exception used for expected bridge failures."""

    def __init__(self, status: int, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


class BridgeState:
    def __init__(self, token: str):
        self.token = token
        self.started_at = _datetime.datetime.now(_datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        self.session: Optional[Session] = None
        self.session_error: Optional[str] = None
        self.session_traceback: Optional[str] = None
        self.session_ready = threading.Event()
        self.request_lock = threading.Lock()

    @property
    def authenticated(self) -> bool:
        return self.session is not None and bool(getattr(self.session, "logged", False))

    def start_session_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self._create_session, name="ikabot-bridge-session", daemon=True)
        thread.start()
        return thread

    def _create_session(self) -> None:
        try:
            from ikabot import config as ikabot_config
            from ikabot.web.session import Session

            login_email = os.environ.get("IKABOT_LOGIN_EMAIL")
            login_password = os.environ.get("IKABOT_LOGIN_PASSWORD")
            account_index = os.environ.get("IKABOT_ACCOUNT_INDEX")
            if login_email and login_password:
                ikabot_config.predetermined_input = [login_email, login_password]
                if account_index:
                    ikabot_config.predetermined_input.append(int(account_index))

            self.session = Session()
        except BaseException as exc:  # Session may call sys.exit(), which raises SystemExit.
            self.session_error = str(exc) or exc.__class__.__name__
            self.session_traceback = traceback.format_exc()
        finally:
            self.session_ready.set()

    def require_session(self) -> Session:
        if self.session is not None and bool(getattr(self.session, "logged", False)):
            return self.session
        details: Dict[str, Any] = {}
        if self.session_error:
            details["sessionError"] = self.session_error
        raise BridgeError(409, "session_not_ready", "Ikabot session is not logged in yet", details)


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = "IkabotBridge/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep the bearer token out of logs by not logging request headers. Route
        # logs still go to stderr for local debugging.
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    @property
    def state(self) -> BridgeState:
        return self.server.state  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            parsed_path = urlparse(self.path)
            path = parsed_path.path

            if method == "GET" and path == "/health":
                self._send_json(200, {"ok": True, "data": self._health_data()})
                return

            self._require_auth()

            if method == "GET" and path == "/session":
                self._send_json(200, {"ok": True, "data": self._session_data()})
                return

            if method == "POST" and path == "/request":
                self._handle_request_endpoint()
                return

            if method == "GET":
                city_id = _typed_resource_id(path, "city")
                if city_id is not None:
                    self._handle_city_endpoint(city_id)
                    return

                island_id = _typed_resource_id(path, "island")
                if island_id is not None:
                    self._handle_island_endpoint(island_id)
                    return

            raise BridgeError(404, "not_found", "Route not found")
        except BridgeError as exc:
            self._send_error(exc.status, exc.code, exc.message, exc.details)
        except REQUEST_TIMEOUT_EXC as exc:
            self._send_error(504, "upstream_timeout", "Upstream request timed out", {"type": exc.__class__.__name__})
        except REQUEST_EXCEPTION_EXC as exc:
            self._send_error(502, "upstream_error", "Upstream request failed", {"type": exc.__class__.__name__})
        except Exception as exc:
            self._send_error(500, "internal_error", "Unexpected bridge failure", {"type": exc.__class__.__name__})

    def _health_data(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "status": "ok",
            "version": BRIDGE_VERSION,
            "pid": os.getpid(),
            "startedAt": self.state.started_at,
            "authenticated": self.state.authenticated,
        }
        if self.state.session_error:
            data["status"] = "session_error"
        return data

    def _session_data(self) -> Dict[str, Any]:
        from ikabot.helpers.pedirInfo import getIdsOfCities
        from ikabot.helpers.varios import getCurrentCityId

        session = self.state.require_session()
        with self.state.request_lock:
            current_city_id = _safe_call(lambda: getCurrentCityId(session))
            city_ids = None
            cities = None
            try:
                city_ids, cities = getIdsOfCities(session)
            except Exception:
                city_ids, cities = None, None

            return {
                "loggedIn": bool(getattr(session, "logged", False)),
                "host": getattr(session, "host", None),
                "urlBase": getattr(session, "urlBase", None),
                "username": getattr(session, "username", None),
                "world": getattr(session, "word", None),
                "server": _server_name(session),
                "currentCityId": current_city_id,
                "cityIds": city_ids,
                "cities": cities,
            }

    def _handle_request_endpoint(self) -> None:
        body = self._read_json_body()
        request = _normalize_request_body(body)
        session = self.state.require_session()

        started = time.monotonic()
        with self.state.request_lock:
            if request["method"] == "GET":
                path, params = _replace_action_request_for_get(session, request["path"], request["params"])
                response = session.get(
                    path,
                    params=params,
                    ignoreExpire=request["options"]["ignoreExpire"],
                    noIndex=request["options"]["noIndex"],
                    noQuery=request["options"]["noQuery"],
                    fullResponse=True,
                )
            else:
                response = session.post(
                    request["path"],
                    payloadPost=request["form"],
                    params=request["params"],
                    ignoreExpire=request["options"]["ignoreExpire"],
                    noIndex=request["options"]["noIndex"],
                    noQuery=request["options"]["noQuery"],
                    fullResponse=True,
                )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if request["options"]["rawResponse"]:
            self._send_raw_response(response)
            return

        self._send_json(
            200,
            {
                "ok": True,
                "data": {
                    "status": response.status_code,
                    "headers": _safe_response_headers(response.headers),
                    "body": response.text,
                    "elapsedMs": elapsed_ms,
                    "finalUrl": response.url,
                },
            },
        )

    def _handle_city_endpoint(self, city_id: str) -> None:
        from ikabot.helpers.getJson import getCity

        session = self.state.require_session()
        path = f"view=city&cityId={city_id}&backgroundView=city&currentCityId={city_id}&ajax=1"
        with self.state.request_lock:
            html = session.get(path)
            data = _parse_upstream_json(lambda: getCity(html), "city", city_id)

        self._send_json(200, {"ok": True, "data": _json_safe(data)})

    def _handle_island_endpoint(self, island_id: str) -> None:
        from ikabot.helpers.getJson import getIsland

        session = self.state.require_session()
        path = f"view=island&islandId={island_id}&backgroundView=island&currentIslandId={island_id}&ajax=1"
        with self.state.request_lock:
            html = session.get(path)
            data = _parse_upstream_json(lambda: getIsland(html), "island", island_id)

        self._send_json(200, {"ok": True, "data": _json_safe(data)})

    def _require_auth(self) -> None:
        auth = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not auth.startswith(prefix):
            raise BridgeError(401, "unauthorized", "Bearer token is required")
        token = auth[len(prefix) :]
        if not hmac.compare_digest(token, self.state.token):
            raise BridgeError(401, "unauthorized", "Bearer token is invalid")

    def _read_json_body(self) -> Dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type.lower():
            raise BridgeError(400, "invalid_request", "Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise BridgeError(400, "invalid_request", "Content-Length is invalid")
        if length <= 0:
            raise BridgeError(400, "invalid_request", "JSON body is required")
        if length > MAX_JSON_BODY_BYTES:
            raise BridgeError(400, "invalid_request", "JSON body is too large")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BridgeError(400, "invalid_request", "Body must be valid UTF-8 JSON")
        if not isinstance(body, dict):
            raise BridgeError(400, "invalid_request", "JSON body must be an object")
        return body

    def _send_json(self, status: int, body: Dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_error(self, status: int, code: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        self._send_json(
            status,
            {
                "ok": False,
                "error": {
                    "code": code,
                    "message": message,
                    "details": details or {},
                    "requestId": uuid.uuid4().hex,
                },
            },
        )

    def _send_raw_response(self, response: Any) -> None:
        body = response.content
        self.send_response(response.status_code)
        for key, value in _safe_response_headers(response.headers).items():
            if key.lower() != "content-length":
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: Tuple[str, int], handler_class: type, state: BridgeState):
        self.state = state
        super().__init__(server_address, handler_class)


def _typed_resource_id(path: str, resource: str) -> Optional[str]:
    match = re.fullmatch(rf"/{resource}/([^/]+)", path)
    if not match:
        return None

    resource_id = unquote(match.group(1))
    if not resource_id or not re.fullmatch(r"\d+", resource_id):
        raise BridgeError(400, "invalid_request", f"{resource} id must be numeric")
    return resource_id


def _parse_upstream_json(fn: Any, resource: str, resource_id: str) -> Any:
    try:
        return fn()
    except (AttributeError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BridgeError(
            422,
            "upstream_parse_failed",
            f"Could not parse {resource} data from Ikariam response",
            {"resource": resource, "id": resource_id, "type": exc.__class__.__name__},
        )


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _normalize_request_body(body: Dict[str, Any]) -> Dict[str, Any]:
    method = str(body.get("method", "")).upper()
    if method not in {"GET", "POST"}:
        raise BridgeError(400, "invalid_request", "method must be GET or POST")

    path = body.get("path", body.get("url", ""))
    if path is None:
        path = ""
    if not isinstance(path, str):
        raise BridgeError(400, "invalid_request", "path must be a string")
    _validate_relative_path(path)

    params = _normalize_stringish_mapping(body.get("params", {}), "params")
    form = _normalize_stringish_mapping(body.get("form", body.get("payload", {})), "form")
    if method == "GET" and form:
        raise BridgeError(400, "invalid_request", "form/payload is only valid for POST")

    options = body.get("options", {}) or {}
    if not isinstance(options, dict):
        raise BridgeError(400, "invalid_request", "options must be an object")
    # Support the field names from the follow-up bead while keeping the documented
    # nested options shape.
    for legacy_name, option_name in {
        "ignoreExpire": "ignoreExpire",
        "noIndex": "noIndex",
        "noQuery": "noQuery",
        "rawResponse": "rawResponse",
        "fullResponse": "rawResponse",
    }.items():
        if legacy_name in body and option_name not in options:
            options[option_name] = body[legacy_name]

    normalized_options = {
        "ignoreExpire": _bool_option(options, "ignoreExpire"),
        "noIndex": _bool_option(options, "noIndex"),
        "noQuery": _bool_option(options, "noQuery"),
        "rawResponse": _bool_option(options, "rawResponse"),
    }

    return {
        "method": method,
        "path": path,
        "params": params,
        "form": form,
        "options": normalized_options,
    }


def _validate_relative_path(path: str) -> None:
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc or path.startswith("//"):
        raise BridgeError(400, "invalid_request", "path must be relative to the Ikariam host")
    if CONTROL_CHARS.search(path):
        raise BridgeError(400, "invalid_request", "path must not contain control characters")


def _normalize_stringish_mapping(value: Any, field: str) -> Dict[str, Optional[str]]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise BridgeError(400, "invalid_request", f"{field} must be an object")
    normalized: Dict[str, Optional[str]] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise BridgeError(400, "invalid_request", f"{field} keys must be strings")
        if CONTROL_CHARS.search(key):
            raise BridgeError(400, "invalid_request", f"{field} keys must not contain control characters")
        if item is None:
            normalized[key] = None
        elif isinstance(item, (str, int, float, bool)):
            normalized[key] = str(item)
        else:
            raise BridgeError(400, "invalid_request", f"{field}.{key} must be a string, number, boolean, or null")
    return normalized


def _bool_option(options: Dict[str, Any], name: str) -> bool:
    value = options.get(name, False)
    if not isinstance(value, bool):
        raise BridgeError(400, "invalid_request", f"options.{name} must be a boolean")
    return value


def _replace_action_request_for_get(session: Any, path: str, params: Dict[str, Optional[str]]) -> Tuple[str, Dict[str, Optional[str]]]:
    needs_token = actionRequest in path or any(value == actionRequest for value in params.values())
    if not needs_token:
        return path, params
    token = session._Session__token()
    replaced_params = {
        key: (token if value == actionRequest else value)
        for key, value in params.items()
    }
    return path.replace(actionRequest, token), replaced_params


def _safe_response_headers(headers: Any) -> Dict[str, str]:
    safe: Dict[str, str] = {}
    for key, value in dict(headers).items():
        lower = key.lower()
        if lower in HOP_BY_HOP_OR_SECRET_HEADERS or lower.startswith("proxy-"):
            continue
        safe[key] = value
    return safe


def _safe_call(fn: Any) -> Any:
    try:
        return fn()
    except Exception:
        return None


def _server_name(session: Any) -> Optional[str]:
    mundo = getattr(session, "mundo", None)
    servidor = getattr(session, "servidor", None)
    if mundo and servidor:
        return f"s{mundo}-{servidor}"
    return None


def _generate_token() -> str:
    token = os.environ.get("IKABOT_BRIDGE_TOKEN")
    if token:
        return token
    # 32 bytes gives 256 bits of entropy; token_urlsafe emits a bearer-safe string.
    return secrets.token_urlsafe(32)


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local Ikabot Session bridge")
    parser.add_argument("--host", default=os.environ.get("IKABOT_BRIDGE_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("IKABOT_BRIDGE_PORT", DEFAULT_PORT)))
    return parser.parse_args(argv)


def _validate_bind_host(host: str) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("The bridge may only bind to localhost (127.0.0.1, ::1, or localhost)")


def _prepare_session_storage() -> None:
    """Use the same .ikabot storage location as the classic CLI.

    The classic ikabot command changes into the user's home directory before
    creating Session(), so saved cookies live in ~/.ikabot. The bridge must do
    the same or it starts with an empty project-local .ikabot file and appears
    unauthenticated while the classic CLI is logged in.
    """
    home_key = "USERPROFILE" if os.name == "nt" else "HOME"
    home = os.getenv(home_key)
    if home:
        os.chdir(home)
    if not os.path.isfile(ikaFile):
        open(ikaFile, "w").close()
        os.chmod(ikaFile, 0o600)


def run(argv: Optional[list] = None) -> None:
    args = _parse_args(argv)
    _validate_bind_host(args.host)

    state = BridgeState(_generate_token())
    httpd = BridgeHTTPServer((args.host, args.port), BridgeRequestHandler, state)
    host, port = httpd.server_address[:2]
    if host == "0.0.0.0":
        raise SystemExit("Refusing to bind bridge to a non-loopback address")

    print(json.dumps({"event": "bridge-ready", "baseUrl": f"http://{host}:{port}", "token": state.token}), flush=True)
    _prepare_session_storage()
    state.start_session_thread()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
