from __future__ import annotations

import unittest

import httpx

from widget.wechat.pid_routing import (
    PidRoutedTransport,
    pid_accepts_connections,
    selected_pid_still_listens,
)


class _FakeSocket:
    def __init__(self, source_port: int, response: bytes):
        self.source_port = source_port
        self.response = response
        self.sent: list[bytes] = []
        self.closed = False
        self.timeouts: list[float] = []

    def getsockname(self):
        return "127.0.0.1", self.source_port

    def getpeername(self):
        return "127.0.0.1", 30001

    def settimeout(self, timeout):
        self.timeouts.append(float(timeout))

    def sendall(self, data: bytes):
        self.sent.append(bytes(data))

    def recv(self, _size: int) -> bytes:
        data, self.response = self.response, b""
        return data

    def close(self):
        self.closed = True


class PidRoutedTransportTests(unittest.TestCase):
    _OK = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 17\r\n"
        b"Content-Type: application/json\r\n\r\n{\"wxid\":\"target\"}"
    )

    def test_wrong_process_receives_no_http_bytes_and_target_handles_request(self) -> None:
        wrong = _FakeSocket(51001, self._OK)
        target = _FakeSocket(51002, self._OK)
        sockets = iter((wrong, target))
        transport = PidRoutedTransport(
            target_pid=22,
            upstream_port=30001,
            socket_factory=lambda *_args, **_kwargs: next(sockets),
            server_pid_fn=lambda _upstream, source: {51001: 11, 51002: 22}[source],
            attempts=2,
        )

        response = transport.handle_request(
            httpx.Request("POST", "http://127.0.0.1:30001/GetSelfProfile", json={})
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("target", response.json()["wxid"])
        self.assertEqual([], wrong.sent)
        self.assertTrue(wrong.closed)
        self.assertTrue(target.sent)

    def test_fails_closed_when_no_connection_reaches_selected_process(self) -> None:
        made = [_FakeSocket(51001, self._OK), _FakeSocket(51002, self._OK)]
        sockets = list(made)
        transport = PidRoutedTransport(
            target_pid=22,
            upstream_port=30001,
            socket_factory=lambda *_args, **_kwargs: sockets.pop(0),
            server_pid_fn=lambda _upstream, _source: 11,
            attempts=2,
        )

        with self.assertRaises(httpx.ConnectError):
            transport.handle_request(
                httpx.Request("POST", "http://127.0.0.1:30001/QueryDB/execute", json={})
            )

        self.assertTrue(all(not sock.sent for sock in made))

    def test_database_requests_get_a_longer_read_timeout_than_normal_hook_calls(self) -> None:
        profile_socket = _FakeSocket(51001, self._OK)
        database_socket = _FakeSocket(51002, self._OK)
        sockets = iter((profile_socket, database_socket))
        transport = PidRoutedTransport(
            target_pid=22,
            upstream_port=30001,
            timeout=4.0,
            query_timeout=20.0,
            socket_factory=lambda *_args, **_kwargs: next(sockets),
            server_pid_fn=lambda _upstream, _source: 22,
        )

        transport.handle_request(
            httpx.Request("POST", "http://127.0.0.1:30001/GetSelfProfile", json={})
        )
        transport.handle_request(
            httpx.Request("POST", "http://127.0.0.1:30001/QueryDB/GetAllDBName", json={})
        )

        self.assertEqual(4.0, profile_socket.timeouts[-1])
        self.assertEqual(20.0, database_socket.timeouts[-1])

    def test_normal_hook_request_honors_shorter_per_request_timeout(self) -> None:
        profile_socket = _FakeSocket(
            43001, b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}",
        )
        transport = PidRoutedTransport(
            target_pid=11,
            upstream_port=30001,
            timeout=4.0,
            socket_factory=lambda *_args: profile_socket,
            server_pid_fn=lambda _port, _source: 11,
        )
        request = httpx.Request(
            "POST",
            "http://127.0.0.1:30001/GetSelfProfile",
            json={},
            extensions={"timeout": {"connect": 0.25, "read": 0.25,
                                      "write": 0.25, "pool": 0.25}},
        )

        transport.handle_request(request)

        self.assertEqual(0.25, profile_socket.timeouts[-1])

    def test_shared_listener_is_healthy_while_selected_pid_is_still_present(self) -> None:
        self.assertTrue(selected_pid_still_listens(
            22, 30001,
            port_for_pid=lambda _pid: 30001,
            owners_for_port=lambda _port: [11, 22],
        ))
        self.assertFalse(selected_pid_still_listens(
            22, 30001,
            port_for_pid=lambda _pid: 30001,
            owners_for_port=lambda _port: [11],
        ))

    def test_probe_sends_no_http_bytes_while_checking_listener_pid(self) -> None:
        wrong = _FakeSocket(51001, self._OK)
        target = _FakeSocket(51002, self._OK)
        sockets = iter((wrong, target))

        reachable = pid_accepts_connections(
            22, 30001,
            attempts=2,
            socket_factory=lambda *_args, **_kwargs: next(sockets),
            server_pid_fn=lambda _upstream, source: {51001: 11, 51002: 22}[source],
            sleep_fn=lambda _seconds: None,
        )

        self.assertTrue(reachable)
        self.assertEqual([], wrong.sent)
        self.assertEqual([], target.sent)
        self.assertTrue(wrong.closed)
        self.assertTrue(target.closed)


if __name__ == "__main__":
    unittest.main()
