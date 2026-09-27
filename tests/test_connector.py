import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import qq_mail_connector as bridge


SETTINGS = {"QQ_MAIL_ADDRESS": "test@qq.com", "QQ_MAIL_AUTH_CODE": "fake", "CONNECTOR_TOKEN": "test-token"}


class FakeIMAP:
    def __init__(self):
        self.closed = False
        self.calls = []

    def uid(self, command, *args):
        self.calls.append((command, args))
        if command == "search":
            return "OK", [b"10 11"]
        uid, item = args
        if item == "(RFC822.SIZE)":
            return "OK", [b"1 (UID 11 RFC822.SIZE 75)"]
        raw = (b"From: Sender <sender@example.com>\r\nTo: test@qq.com\r\n"
               b"Subject: Project update\r\nDate: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
               b"Content-Type: text/plain; charset=utf-8\r\n\r\nHello")
        return "OK", [(b"1 FETCH", raw)]

    def logout(self):
        self.closed = True


class FakeSMTP:
    def __init__(self, *args, **kwargs):
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def login(self, address, code):
        pass

    def send_message(self, message):
        self.sent.append(message)


class ConnectorTests(unittest.TestCase):
    def test_list_and_read_do_not_mutate_imap_state(self):
        instances = []

        def make(_settings):
            imap = FakeIMAP()
            instances.append(imap)
            return imap

        with patch.object(bridge, "imap_connect", side_effect=make):
            results = bridge.list_messages(SETTINGS, 1, "project")
            detail = bridge.get_message(SETTINGS, "11")
        self.assertEqual(results[0]["uid"], "11")
        self.assertEqual(detail["body"], "Hello")
        self.assertTrue(all(item.closed for item in instances))
        self.assertTrue(all("BODY.PEEK" in str(call) or call[0] == "search" or "RFC822.SIZE" in str(call)
                            for item in instances for call in item.calls))

    def test_send_requires_opt_in_and_confirmation_and_blocks_header_injection(self):
        data = {"to": ["person@example.com"], "subject": "Hello", "body": "Text", "confirm_send": True}
        with patch.dict("os.environ", {"QQ_MAIL_ALLOW_SEND": "false"}):
            with self.assertRaises(PermissionError):
                bridge.send_message(SETTINGS, data)
        with patch.dict("os.environ", {"QQ_MAIL_ALLOW_SEND": "true"}):
            with self.assertRaises(ValueError):
                bridge.send_message(SETTINGS, {**data, "confirm_send": False})
            with self.assertRaises(ValueError):
                bridge.send_message(SETTINGS, {**data, "to": ["x@example.com\nBcc:bad@example.com"]})
            with patch.object(bridge.smtplib, "SMTP_SSL", FakeSMTP):
                self.assertEqual(bridge.send_message(SETTINGS, data)["sent"], True)

    def test_http_requires_token_and_never_echoes_it(self):
        bridge.Handler.settings = SETTINGS
        server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
            conn.request("GET", "/health")
            response = conn.getresponse()
            self.assertEqual(response.status, 401)
            self.assertNotIn(b"test-token", response.read())
            conn.request("GET", "/health", headers={"Authorization": "Bearer test-token"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"status": "ok"})
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
