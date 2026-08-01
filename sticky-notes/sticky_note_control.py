"""External control socket for the sticky-note widget.

A small loopback TCP server that lets other processes signal the running widget
— most importantly "refresh now" so an external journal edit (e.g. from
`sticky-note-add`) shows up immediately instead of waiting for the next
auto-refresh tick.

Why a socket (vs a Unix signal or a macOS distributed notification): the wire
protocol is a plain line of JSON, so it's trivially extensible to new commands
and portable to other OSes — only the widget-side handler is AppKit-specific.

Protocol — one JSON object per line, request and response both newline-framed:

    request :  {"cmd": "refresh", "scope": "journal"}\n
    response:  {"ok": true}\n
    request :  {"cmd": "ping"}\n
    response:  {"ok": true, "pong": true}\n

`scope` on a refresh is "journal" (cheap: reload the local journal and
re-render, no GUS query — the add-task case) or "gus" (full re-query). Unknown
commands get {"ok": false, "error": "..."}.

Security: the server binds 127.0.0.1 ONLY, so it is never reachable off the
machine. Any local process can still connect, so do not add commands here that
perform privileged actions without adding authentication first.

Threading: the server runs its accept loop on a daemon thread. The handler
callback therefore runs OFF the main thread — a widget handler MUST marshal any
AppKit/UI work back to the main thread itself (see sticky_note.py).
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Callable

# Framing / safety limits. Commands are tiny one-liners; cap the read so a
# misbehaving or hostile local client can't make us buffer unboundedly.
_MAX_LINE_BYTES = 64 * 1024
_ACCEPT_BACKLOG = 8
_CLIENT_TIMEOUT = 5.0  # seconds; a stalled client must not pin a connection open

HOST = "127.0.0.1"


class ControlServer:
    """Loopback TCP server dispatching one-line JSON commands to a handler.

    The socket is bound eagerly in __init__ so a port collision surfaces to the
    caller immediately (it can print and exit). serve_forever() runs the accept
    loop; start_thread() runs it on a daemon thread.
    """

    def __init__(self, port: int, handler: Callable[[dict], dict]):
        """Bind 127.0.0.1:<port>. Raises OSError if the port is unavailable.

        `handler` receives the parsed command dict and returns a JSON-able dict
        response. It runs on the accept-loop thread, not the main thread.
        """
        self._handler = handler
        self._port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Avoid a TIME_WAIT bind failure on a quick relaunch of the widget.
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((HOST, port))  # OSError (EADDRINUSE) propagates to caller
        self._sock.listen(_ACCEPT_BACKLOG)

    @property
    def port(self) -> int:
        return self._port

    def start_thread(self) -> threading.Thread:
        """Run the accept loop on a daemon thread and return it."""
        t = threading.Thread(
            target=self.serve_forever, name="sticky-note-control", daemon=True
        )
        t.start()
        return t

    def serve_forever(self) -> None:
        """Accept and service connections until the socket is closed.

        A per-connection error is logged and swallowed so one bad client never
        kills the listener (runtime robustness: the socket is an enhancement,
        never a reason to take the widget down).
        """
        import sys

        while True:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return  # socket closed -> stop serving
            try:
                self._handle_conn(conn)
            except Exception as exc:  # noqa: BLE001 - never let one client kill us
                print(f"[sticky-note] control connection error: {exc}", file=sys.stderr)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle_conn(self, conn: socket.socket) -> None:
        conn.settimeout(_CLIENT_TIMEOUT)
        data = self._recv_line(conn)
        if data is None:
            self._send(conn, {"ok": False, "error": "no command"})
            return
        try:
            obj = json.loads(data.decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError("command must be a JSON object")
        except (ValueError, UnicodeDecodeError) as exc:
            self._send(conn, {"ok": False, "error": f"bad request: {exc}"})
            return

        if obj.get("cmd") == "ping":
            self._send(conn, {"ok": True, "pong": True})
            return

        try:
            resp = self._handler(obj)
        except Exception as exc:  # noqa: BLE001
            resp = {"ok": False, "error": f"handler error: {exc}"}
        if not isinstance(resp, dict):
            resp = {"ok": True}
        self._send(conn, resp)

    @staticmethod
    def _recv_line(conn: socket.socket) -> "bytes | None":
        """Read up to the first newline (or EOF), capped at _MAX_LINE_BYTES."""
        chunks = []
        total = 0
        while total < _MAX_LINE_BYTES:
            chunk = conn.recv(4096)
            if not chunk:
                break
            nl = chunk.find(b"\n")
            if nl != -1:
                chunks.append(chunk[:nl])
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        return data if data else None

    @staticmethod
    def _send(conn: socket.socket, obj: dict) -> None:
        try:
            conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        except OSError:
            pass  # client gone; nothing to do

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def send_command(port: int, obj: dict, timeout: float = 5.0) -> dict:
    """Connect to 127.0.0.1:<port>, send one JSON command, return the response.

    Raises ConnectionRefusedError if no widget is listening (i.e. it isn't
    running), or OSError/socket.timeout on other transport failures. Callers
    that treat the signal as best-effort should catch OSError.
    """
    with socket.create_connection((HOST, port), timeout=timeout) as conn:
        conn.settimeout(timeout)
        conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf and len(buf) < _MAX_LINE_BYTES:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
    line = buf.split(b"\n", 1)[0]
    if not line:
        return {"ok": False, "error": "empty response"}
    try:
        return json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return {"ok": False, "error": f"bad response: {exc}"}
