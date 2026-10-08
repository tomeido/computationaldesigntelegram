import socket
from http.client import HTTPConnection
from types import SimpleNamespace

import pytest

from compdesign_bot import mail_unsubscribe
from compdesign_bot.mailing import MailingStore


@pytest.fixture
def mailing_endpoint(tmp_path):
    settings = SimpleNamespace(
        database_path=tmp_path / "mail.db",
        mailing_unsubscribe_base_url="https://mail.example.com",
        mailing_unsubscribe_host="127.0.0.1",
        mailing_unsubscribe_port=0,
    )
    with MailingStore(settings.database_path) as store:
        store.subscribe("recipient@example.com", 1)
        token = store.db.execute("SELECT token FROM mailing_subscribers").fetchone()[0]
        with store.db:
            store.db.executemany(
                "INSERT INTO mailing_outbox(post_key,email,status,created_at) VALUES(?,?,?,?)",
                [(status, "recipient@example.com", status, "2026-10-08")
                 for status in ("queued", "failed", "sent")],
            )
    return settings, token


def request(server, method, path, *, body=None, headers=None):
    connection = HTTPConnection(*server.server_address, timeout=2)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def membership(settings):
    with MailingStore(settings.database_path) as store:
        active = store.db.execute("SELECT active FROM mailing_subscribers").fetchone()[0]
        statuses = dict(store.db.execute("SELECT post_key,status FROM mailing_outbox"))
        return active, statuses


def test_one_click_get_suppresses_pending_mail_without_exposing_identity(mailing_endpoint, capsys, caplog):
    settings, token = mailing_endpoint
    with mail_unsubscribe.unsubscribe_server(settings) as server:
        status, headers, body = request(server, "GET", "/unsubscribe/" + token)
    assert status == 200
    assert membership(settings) == (0, {"queued": "suppressed", "failed": "suppressed", "sent": "sent"})
    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "Set-Cookie" not in headers and "Location" not in headers
    captured = capsys.readouterr()
    public_output = body.decode() + captured.out + captured.err + caplog.text
    assert token not in public_output
    assert "recipient@example.com" not in public_output


def test_rfc8058_post_is_idempotent_without_cookie_or_login(mailing_endpoint):
    settings, token = mailing_endpoint
    with mail_unsubscribe.unsubscribe_server(settings) as server:
        for _ in range(2):
            status, headers, _body = request(
                server, "POST", "/unsubscribe/" + token,
                body=b"List-Unsubscribe=One-Click",
                headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
            )
            assert status == 200
            assert "Location" not in headers and "Set-Cookie" not in headers
    assert membership(settings)[0] == 0


def test_head_checks_link_without_cancelling_delivery(mailing_endpoint):
    settings, token = mailing_endpoint
    with mail_unsubscribe.unsubscribe_server(settings) as server:
        status, _headers, body = request(server, "HEAD", "/unsubscribe/" + token)
    assert status == 200 and body == b""
    assert membership(settings) == (1, {"queued": "queued", "failed": "failed", "sent": "sent"})


@pytest.mark.parametrize("path", [
    "/", "/unsubscribe/short", "/unsubscribe/" + "A" * 32,
    "/unsubscribe/" + "A" * 31 + ".", "/unsubscribe/" + "A" * 33,
])
def test_invalid_or_unknown_tokens_have_no_side_effect(mailing_endpoint, path):
    settings, _token = mailing_endpoint
    with mail_unsubscribe.unsubscribe_server(settings) as server:
        assert request(server, "GET", path)[0] == 404
    assert membership(settings)[0] == 1


@pytest.mark.parametrize("body,headers,status", [
    (b"List-Unsubscribe=One-Click", {"Content-Type": "text/plain"}, 415),
    (b"List-Unsubscribe=Wrong", {"Content-Type": "application/x-www-form-urlencoded"}, 400),
    (b"List-Unsubscribe=One-Click&other=value", {"Content-Type": "application/x-www-form-urlencoded"}, 400),
    (b"List-Unsubscribe=One-Click&List-Unsubscribe=One-Click",
     {"Content-Type": "application/x-www-form-urlencoded"}, 400),
    (b"\xff", {"Content-Type": "application/x-www-form-urlencoded"}, 400),
    (b"x" * 4097, {"Content-Type": "application/x-www-form-urlencoded"}, 413),
    (b"List-Unsubscribe=One-Click", {"Content-Type": "application/x-www-form-urlencoded",
                                      "Content-Length": "-1"}, 400),
    (b"List-Unsubscribe=One-Click", {"Content-Type": "application/x-www-form-urlencoded",
                                      "Transfer-Encoding": "chunked"}, 400),
])
def test_bad_post_is_rejected_without_unsubscribing(mailing_endpoint, body, headers, status):
    settings, token = mailing_endpoint
    with mail_unsubscribe.unsubscribe_server(settings) as server:
        assert request(server, "POST", "/unsubscribe/" + token, body=body, headers=headers)[0] == status
    assert membership(settings)[0] == 1


def multipart_body(*fields):
    body = b""
    for name, value in fields:
        body += (
            b"--mail-boundary\r\nContent-Disposition: form-data; name=\"" + name
            + b"\"\r\n\r\n" + value + b"\r\n"
        )
    return body + b"--mail-boundary--\r\n"


def test_rfc8058_multipart_one_click_post(mailing_endpoint):
    settings, token = mailing_endpoint
    with mail_unsubscribe.unsubscribe_server(settings) as server:
        status, _headers, _body = request(
            server, "POST", "/unsubscribe/" + token,
            body=multipart_body((b"List-Unsubscribe", b"One-Click")),
            headers={"Content-Type": "multipart/form-data; boundary=mail-boundary"},
        )
    assert status == 200
    assert membership(settings)[0] == 0


@pytest.mark.parametrize("body,content_type", [
    (multipart_body((b"List-Unsubscribe", b"One-Click"), (b"List-Unsubscribe", b"One-Click")),
     "multipart/form-data; boundary=mail-boundary"),
    (multipart_body((b"List-Unsubscribe", b"One-Click"), (b"other", b"value")),
     "multipart/form-data; boundary=mail-boundary"),
    (multipart_body((b"List-Unsubscribe", b"Wrong")), "multipart/form-data; boundary=mail-boundary"),
    (multipart_body((b"List-Unsubscribe", b"One-Click")), "multipart/form-data; boundary=wrong-boundary"),
    (multipart_body((b"List-Unsubscribe", b"One-Click")), "multipart/form-data"),
    (b"--mail-boundary\r\nContent-Disposition: form-data; name=\"List-Unsubscribe\"\r\n\r\nOne-Click",
     "multipart/form-data; boundary=mail-boundary"),
])
def test_invalid_multipart_has_no_side_effect(mailing_endpoint, body, content_type):
    settings, token = mailing_endpoint
    with mail_unsubscribe.unsubscribe_server(settings) as server:
        assert request(
            server, "POST", "/unsubscribe/" + token,
            body=body, headers={"Content-Type": content_type},
        )[0] == 400
    assert membership(settings)[0] == 1


@pytest.mark.parametrize("method", ["DELETE", "PUT", "PATCH", "OPTIONS", "UNKNOWN"])
def test_other_methods_return_405_without_mutating(mailing_endpoint, method):
    settings, token = mailing_endpoint
    with mail_unsubscribe.unsubscribe_server(settings) as server:
        status, headers, body = request(server, method, "/unsubscribe/" + token)
    assert status == 405
    assert headers["Allow"] == "GET, HEAD, POST"
    assert token.encode() not in body
    assert membership(settings)[0] == 1


def test_public_base_url_prefix_is_part_of_the_route(mailing_endpoint):
    settings, token = mailing_endpoint
    settings.mailing_unsubscribe_base_url = "https://mail.example.com/briefing/"
    with mail_unsubscribe.unsubscribe_server(settings) as server:
        assert request(server, "GET", "/unsubscribe/" + token)[0] == 404
        assert membership(settings)[0] == 1
        assert request(server, "GET", "/briefing/unsubscribe/" + token)[0] == 200
    assert membership(settings)[0] == 0


def test_request_connections_and_server_socket_are_closed(mailing_endpoint, monkeypatch):
    settings, token = mailing_endpoint
    closed = []

    class TrackingStore(MailingStore):
        def close(self):
            closed.append(self)
            super().close()

    monkeypatch.setattr(mail_unsubscribe, "MailingStore", TrackingStore)
    with mail_unsubscribe.unsubscribe_server(settings) as server:
        address = server.server_address
        assert len(closed) == 1  # Startup initialization has released its connection.
        assert request(server, "HEAD", "/unsubscribe/" + token)[0] == 200
        assert len(closed) == 1  # HEAD uses a read-only connection without a schema write.
        assert request(server, "GET", "/unsubscribe/" + token)[0] == 200
        assert len(closed) == 2
    assert server.socket.fileno() == -1
    with pytest.raises(OSError):
        socket.create_connection(address, timeout=0.2)


def test_stalled_request_times_out_without_blocking_the_next_request(mailing_endpoint, monkeypatch):
    settings, token = mailing_endpoint
    monkeypatch.setattr(mail_unsubscribe, "_REQUEST_TIMEOUT_SECONDS", 0.1)
    with mail_unsubscribe.unsubscribe_server(settings) as server:
        connection = socket.create_connection(server.server_address, timeout=1)
        try:
            connection.sendall(
                ("POST /unsubscribe/" + token + " HTTP/1.1\r\nHost: localhost\r\n"
                 "Content-Type: application/x-www-form-urlencoded\r\nContent-Length: 25\r\n\r\nL").encode(),
            )
            assert request(server, "HEAD", "/unsubscribe/" + token)[0] == 200
        finally:
            connection.close()
    assert membership(settings)[0] == 1


def test_blank_public_url_does_not_open_a_server(mailing_endpoint, monkeypatch):
    settings, _token = mailing_endpoint
    settings.mailing_unsubscribe_base_url = ""

    def unexpected_server(*_args, **_kwargs):
        raise AssertionError("Disabled unsubscribe endpoint should not bind a socket")

    monkeypatch.setattr(mail_unsubscribe, "_UnsubscribeHTTPServer", unexpected_server)
    with mail_unsubscribe.unsubscribe_server(settings) as server:
        assert server is None
