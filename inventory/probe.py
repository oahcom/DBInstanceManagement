import socket
import time
from datetime import datetime, timezone

DEFAULT_TIMEOUT = 3.0


def check_tcp(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """探测 host:port 的 TCP 可达性。

    用 create_connection 而不是 connect_ex + 裸 socket，因为它自己处理 DNS 解析与
    地址族选择，并且一定会关闭 fd（裸 socket 忘关 = 连接泄漏）。
    """
    started = time.monotonic()
    reachable = False
    error = ""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            reachable = True
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"
    latency_ms = round((time.monotonic() - started) * 1000, 2)
    return {
        "host": host,
        "port": port,
        "reachable": reachable,
        "latency_ms": latency_ms,
        "error": error,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
