"""同一 hook 端口被多个微信监听时，按已建立连接的服务端 PID 安全校验。

Windows 会把连向共享 ``127.0.0.1:30001`` 的不同 TCP 连接分给不同 Weixin.exe。连接建立后，
系统 TCP 表能给出该连接服务端的真实 PID。本模块在发送任何 HTTP 字节之前核对 PID：落到未选
微信的连接立即关闭，只有落到目标 PID 的连接才承载请求。部分微信版本可能把新连接长期交给
同一监听者；此时保持失败关闭并等待后续重试，不通过重开用户已登录的微信来改变分流。
"""
from __future__ import annotations

import socket
import time
from typing import Callable, Iterable, Optional

import httpx
import psutil


def server_pid_for_connection(
    upstream_port: int,
    source_port: int,
    *,
    connections: Optional[Callable[[], Iterable]] = None,
) -> int:
    """返回这条 loopback 连接服务端的 PID；查不清返回 0（失败关闭）。"""
    try:
        rows = (connections or (lambda: psutil.net_connections(kind="tcp")))()
    except Exception:  # noqa: BLE001 - 权限/瞬态失败都不能猜 PID
        return 0
    for row in rows:
        try:
            if not row.pid or not row.laddr or not row.raddr:
                continue
            if int(row.laddr.port) == int(upstream_port) \
                    and int(row.raddr.port) == int(source_port) \
                    and str(row.status).upper() == "ESTABLISHED":
                return int(row.pid)
        except Exception:  # noqa: BLE001
            continue
    return 0


def selected_pid_still_listens(
    pid: int,
    port: int,
    *,
    port_for_pid=None,
    owners_for_port=None,
) -> bool:
    """共享端口模式的归属闸：目标仍是监听者即可；逐连接 PID 闸负责排除其他监听者。"""
    from widget.wechat.ports import listening_pids_for_port, listening_port_for_pid

    try:
        actual = int((port_for_pid or listening_port_for_pid)(int(pid)) or 0)
        owners = list((owners_for_port or listening_pids_for_port)(int(port)) or [])
        return actual == int(port) and int(pid) in {int(value) for value in owners}
    except Exception:  # noqa: BLE001
        return False


def pid_accepts_connections(
    pid: int,
    port: int,
    *,
    attempts: int = 6,
    socket_factory=None,
    server_pid_fn=None,
    sleep_fn=None,
) -> bool:
    """只做 TCP 握手并核对服务端 PID；不向任一微信发送 HTTP 字节。"""
    connect = socket_factory or socket.create_connection
    identify = server_pid_fn or server_pid_for_connection
    nap = sleep_fn or time.sleep
    for _ in range(max(1, int(attempts))):
        sock = None
        try:
            sock = connect(("127.0.0.1", int(port)), 1.0)
            source_port = int(sock.getsockname()[1])
            owner = 0
            for _probe in range(8):
                owner = int(identify(int(port), source_port) or 0)
                if owner:
                    break
                nap(0.005)
            if owner == int(pid):
                return True
        except Exception:  # noqa: BLE001 - 探测失败就是不可达，绝不能猜
            pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    return False


class PidRoutedTransport(httpx.BaseTransport):
    """把每个 HTTP 请求放到已由 Windows 证明属于 ``target_pid`` 的 TCP 连接上。"""

    def __init__(
        self,
        *,
        target_pid: int,
        upstream_port: int,
        upstream_host: str = "127.0.0.1",
        timeout: float = 4.0,
        query_timeout: float = 20.0,
        attempts: int = 24,
        socket_factory=None,
        server_pid_fn=None,
        sleep_fn=None,
    ) -> None:
        self.target_pid = int(target_pid)
        self.upstream_port = int(upstream_port)
        self.upstream_host = str(upstream_host)
        self.timeout = float(timeout)
        self.query_timeout = max(self.timeout, float(query_timeout))
        self.attempts = max(1, int(attempts))
        self._socket_factory = socket_factory or socket.create_connection
        self._server_pid_fn = server_pid_fn or server_pid_for_connection
        self._sleep = sleep_fn or time.sleep

    def _connect_to_target(self, request: httpx.Request):
        last_error: Exception | None = None
        for _ in range(self.attempts):
            sock = None
            try:
                sock = self._socket_factory(
                    (self.upstream_host, self.upstream_port), self.timeout,
                )
                sock.settimeout(self.timeout)
                source_port = int(sock.getsockname()[1])
                owner = 0
                # TCP 握手完成与系统连接表入行之间可能差几个毫秒；只等 PID 元数据，不发请求。
                for _probe in range(8):
                    owner = int(self._server_pid_fn(self.upstream_port, source_port) or 0)
                    if owner:
                        break
                    self._sleep(0.005)
                if owner == self.target_pid:
                    return sock
                sock.close()
            except Exception as exc:  # noqa: BLE001 - 未发送字节前可安全换一条连接
                last_error = exc
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
        message = (
            f"无法把端口 {self.upstream_port} 的连接路由到所选微信 pid={self.target_pid}；"
            "本次请求未发送到任何其他微信"
        )
        if last_error:
            message += f"（{last_error}）"
        raise httpx.ConnectError(message, request=request)

    @staticmethod
    def _encode_request(request: httpx.Request) -> bytes:
        body = request.read()
        path = request.url.raw_path or b"/"
        lines = [request.method.encode("ascii") + b" " + path + b" HTTP/1.1"]
        skipped = {b"host", b"connection", b"content-length", b"transfer-encoding"}
        lines.append(f"Host: 127.0.0.1:{request.url.port or 30001}".encode("ascii"))
        for key, value in request.headers.raw:
            if key.lower() not in skipped:
                lines.append(key + b": " + value)
        lines.extend((f"Content-Length: {len(body)}".encode("ascii"), b"Connection: close"))
        return b"\r\n".join(lines) + b"\r\n\r\n" + body

    @staticmethod
    def _read_response(sock, request: httpx.Request) -> httpx.Response:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
        if b"\r\n\r\n" not in data:
            raise httpx.RemoteProtocolError("微信 hook 返回了不完整的 HTTP 响应", request=request)
        head, body = bytes(data).split(b"\r\n\r\n", 1)
        lines = head.split(b"\r\n")
        try:
            status = int(lines[0].split(b" ", 2)[1])
        except (IndexError, ValueError) as exc:
            raise httpx.RemoteProtocolError("微信 hook HTTP 状态行无效", request=request) from exc
        headers: list[tuple[bytes, bytes]] = []
        content_length: int | None = None
        for line in lines[1:]:
            if b":" not in line:
                continue
            key, value = line.split(b":", 1)
            value = value.strip()
            headers.append((key, value))
            if key.lower() == b"content-length":
                try:
                    content_length = int(value)
                except ValueError:
                    content_length = None
        if content_length is not None:
            while len(body) < content_length:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                body += chunk
            body = body[:content_length]
        else:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                body += chunk
        return httpx.Response(status, headers=headers, content=body, request=request)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        sock = self._connect_to_target(request)
        try:
            # PID 已在连接保持期间确认；从这里开始才允许发送业务 HTTP 字节。
            # 普通 profile/send 请求继续快速失败；QueryDB 冷启动会在微信进程内扫描并注册
            # 数据库句柄，真实账号上常超过 4 秒。这里仅放宽数据库响应等待，不放宽连接
            # 路由，也不自动重试已经发出的请求。
            if request.url.path.startswith("/QueryDB/"):
                sock.settimeout(self.query_timeout)
            else:
                timeout_config = request.extensions.get("timeout") or {}
                read_timeout = timeout_config.get("read")
                effective_timeout = self.timeout
                if isinstance(read_timeout, (int, float)):
                    effective_timeout = min(effective_timeout, float(read_timeout))
                sock.settimeout(effective_timeout)
            sock.sendall(self._encode_request(request))
            return self._read_response(sock, request)
        except httpx.HTTPError:
            raise
        except Exception as exc:
            # 请求一旦发出就绝不自动重试，避免 SendTextMsg 在响应丢失时重复发送。
            raise httpx.TransportError(str(exc), request=request) from exc
        finally:
            sock.close()


def build_pid_routed_client(
    pid: int,
    port: int,
    *,
    timeout: float = 4.0,
    query_timeout: float = 20.0,
) -> httpx.Client:
    transport = PidRoutedTransport(
        target_pid=pid,
        upstream_port=port,
        timeout=timeout,
        query_timeout=query_timeout,
    )
    return httpx.Client(
        base_url=f"http://127.0.0.1:{int(port)}",
        timeout=timeout,
        transport=transport,
        trust_env=False,
    )
