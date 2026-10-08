"""Small, private-token HTTP endpoint for one-click mailing unsubscription."""

import logging
import re
import sqlite3
from contextlib import closing, contextmanager
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit

from .mailing import MailingStore

_REQUEST_TIMEOUT_SECONDS = 3
_MAX_BODY_BYTES = 4096
_TOKEN = r"[A-Za-z0-9_-]{32}"
_SUCCESS = (
    "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
    "<title>Unsubscribed</title><h1>Unsubscribed</h1>"
    "<p>Your address has been removed from future mailing deliveries.</p>"
    "<p lang=\"ko\">수신 거부가 완료되었습니다. 앞으로 메일 발송 대상에서 제외됩니다.</p>"
    "</html>"
).encode("utf-8")
_ERROR = b"<!doctype html><html><title>Request failed</title><p>Request failed.</p></html>"


def _valid_one_click_body(body, content_type):
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "application/x-www-form-urlencoded":
        fields = parse_qs(body.decode("ascii"), strict_parsing=True, max_num_fields=1)
        return fields == {"List-Unsubscribe": ["One-Click"]}
    if any(char in content_type for char in "\r\n"):
        return False
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body,
    )
    boundary = message.get_boundary()
    if (
        not boundary or len(boundary) > 70 or message.defects
        or message["Content-Type"].defects or not message.is_multipart()
    ):
        return False
    parts = list(message.iter_parts())
    if len(parts) != 1:
        return False
    part = parts[0]
    return (
        not part.defects and not part.is_multipart()
        and len(part.get_all("Content-Disposition", [])) == 1
        and not part["Content-Disposition"].defects
        and part.get_content_disposition() == "form-data"
        and part.get_param("name", header="Content-Disposition") == "List-Unsubscribe"
        and part.get_filename() is None and not part.get("Content-Transfer-Encoding")
        and part.get_payload(decode=True) == b"One-Click"
    )


class _UnsubscribeHTTPServer(HTTPServer):
    # One request worker and a short socket timeout bound slow-client resource use.
    request_queue_size = 4

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(_REQUEST_TIMEOUT_SECONDS)
        return connection, address

    def handle_error(self, _request, _client_address):
        # A traceback or request URL could expose a recipient's bearer token.
        logging.getLogger(__name__).warning("Unsubscribe request failed.")


def _handler(settings):
    prefix = urlsplit(settings.mailing_unsubscribe_base_url).path.rstrip("/")
    route = re.compile(re.escape(prefix + "/unsubscribe/") + _TOKEN)

    class Handler(BaseHTTPRequestHandler):
        server_version = "MailUnsubscribe"
        sys_version = ""

        def log_message(self, _format, *_args):
            # Default access/error logs include the secret token in the URL.
            pass

        def _respond(self, status, body=_ERROR, *, allow=False):
            self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            )
            if allow:
                self.send_header("Allow", "GET, HEAD, POST")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def send_error(self, code, message=None, explain=None):
            # Malformed requests and unsupported methods must never echo URLs.
            self._respond(405 if code == 501 else code, allow=code == 501)

        def _token(self):
            if not route.fullmatch(self.path):
                self._respond(404)
                return None
            return self.path.rsplit("/", 1)[1]

        def _unsubscribe(self, token, *, mutate):
            try:
                if mutate:
                    with MailingStore(settings.database_path) as store:
                        found = store.unsubscribe_token(token)
                else:
                    database_url = Path(settings.database_path).resolve().as_uri() + "?mode=ro"
                    with closing(sqlite3.connect(database_url, uri=True, timeout=3)) as db:
                        found = db.execute(
                            "SELECT 1 FROM mailing_subscribers WHERE token=?", (token,),
                        ).fetchone() is not None
            except sqlite3.Error:
                self._respond(503)
                return
            if found:
                self._respond(200, _SUCCESS)
            else:
                self._respond(404)

        def do_GET(self):
            token = self._token()
            if token:
                self._unsubscribe(token, mutate=True)

        def do_HEAD(self):
            token = self._token()
            if token:
                self._unsubscribe(token, mutate=False)

        def do_POST(self):
            token = self._token()
            if not token:
                return
            lengths = self.headers.get_all("Content-Length", [])
            if (
                self.headers.get("Transfer-Encoding") or len(lengths) != 1
                or not lengths[0].isascii() or not lengths[0].isdigit()
            ):
                self._respond(400)
                return
            if len(lengths[0]) > len(str(_MAX_BODY_BYTES)):
                self._respond(413)
                return
            length = int(lengths[0])
            if length > _MAX_BODY_BYTES:
                self._respond(413)
                return
            if len(self.headers.get_all("Content-Type", [])) != 1:
                self._respond(400)
                return
            if self.headers.get_content_type() not in {
                "application/x-www-form-urlencoded", "multipart/form-data",
            }:
                self._respond(415)
                return
            try:
                body = self.rfile.read(length)
                if len(body) != length:
                    self._respond(400)
                    return
                valid = _valid_one_click_body(body, self.headers["Content-Type"])
            except (UnicodeError, ValueError):
                self._respond(400)
                return
            if not valid:
                self._respond(400)
                return
            self._unsubscribe(token, mutate=True)

    return Handler


@contextmanager
def unsubscribe_server(settings):
    """Run one bounded HTTP worker while the bot runs; disabled without a public URL."""
    if not settings.mailing_unsubscribe_base_url:
        yield None
        return
    # Initialize/migrate once at startup so HEAD can use a strictly read-only connection.
    with MailingStore(settings.database_path):
        pass
    server = _UnsubscribeHTTPServer(
        (settings.mailing_unsubscribe_host, settings.mailing_unsubscribe_port), _handler(settings),
    )
    thread = Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.1},
        name="mail-unsubscribe", daemon=True,
    )
    try:
        thread.start()
    except BaseException:
        server.server_close()
        raise
    try:
        yield server
    finally:
        try:
            server.shutdown()
        finally:
            server.server_close()
            thread.join(timeout=_REQUEST_TIMEOUT_SECONDS + 1)
