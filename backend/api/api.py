#!/usr/bin/env python3
"""C++ Studio HTTP API. All submitted C++ runs in disposable Docker containers."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
MAX_BODY = 2 * 1024 * 1024
RUNNER_IMAGE = os.environ.get("RUNNER_IMAGE", "cpp-studio-runner:latest")
JOBS_DIR = Path(os.environ.get("JOBS_DIR", "/jobs")).resolve()
JOBS_HOST_DIR_RAW = os.environ.get("JOBS_HOST_DIR", "").strip()
JOBS_HOST_DIR = Path(JOBS_HOST_DIR_RAW).resolve() if JOBS_HOST_DIR_RAW else Path("/")
ALLOWED_ORIGINS = {x.strip().rstrip("/") for x in os.environ.get("CPP_STUDIO_ALLOWED_ORIGINS", "").split(",") if x.strip()}
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "8"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
RUNNER_RUNTIME = os.environ.get("RUNNER_RUNTIME", "")
rate_lock = threading.Lock()
rate_events: dict[str, deque[float]] = defaultdict(deque)
job_slots = threading.BoundedSemaphore(MAX_CONCURRENT)

JOBS_DIR.mkdir(parents=True, exist_ok=True)
if not JOBS_HOST_DIR.is_absolute() or str(JOBS_HOST_DIR) == "/":
    raise RuntimeError("JOBS_HOST_DIR phải là đường dẫn tuyệt đối riêng trên host, không được là '/'.")


def json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def client_ip(handler: BaseHTTPRequestHandler) -> str:
    if TRUST_PROXY:
        forwarded = handler.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded[:64]
    return handler.client_address[0]


def rate_limited(ip: str) -> bool:
    now = time.monotonic()
    with rate_lock:
        events = rate_events[ip]
        while events and now - events[0] > 60:
            events.popleft()
        if len(events) >= RATE_LIMIT:
            return True
        events.append(now)
        return False


def docker_image_ready() -> bool:
    try:
        result = subprocess.run(["docker", "image", "inspect", RUNNER_IMAGE], capture_output=True, text=True, timeout=4)
        return result.returncode == 0
    except Exception:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "CPPStudioAPI/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print("[API]", self.address_string(), fmt % args, flush=True)

    def origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        return not origin or origin.rstrip("/") in ALLOWED_ORIGINS

    def send_json(self, status: int, payload: dict) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        origin = self.headers.get("Origin")
        if origin and origin.rstrip("/") in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        if not self.origin_allowed():
            self.send_json(403, {"ok": False, "message": "Origin không được phép."})
            return
        self.send_response(204)
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:
        if not self.origin_allowed():
            self.send_json(403, {"ok": False, "message": "Origin không được phép."})
            return
        if self.path != "/api/health":
            self.send_json(404, {"ok": False, "message": "API không tồn tại."})
            return
        ready = docker_image_ready()
        self.send_json(200, {
            "ok": True,
            "ready": ready,
            "compiler": "GNU G++",
            "debugger": "GDB",
            "sandbox": "disposable Docker container",
            "message": "Sẵn sàng." if ready else "Backend đã chạy nhưng chưa tìm thấy runner image.",
        })

    def do_POST(self) -> None:
        if self.path not in ("/api/run", "/api/trace"):
            self.send_json(404, {"ok": False, "message": "API không tồn tại."})
            return
        if not self.origin_allowed():
            self.send_json(403, {"ok": False, "message": "Origin không được phép."})
            return
        if self.headers.get_content_type() != "application/json":
            self.send_json(415, {"ok": False, "message": "Yêu cầu phải dùng application/json."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("Mã nguồn gửi lên trống hoặc vượt quá giới hạn 2 MB.")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            source = str(data.get("source", ""))
            stdin = str(data.get("stdin", ""))
            standard = str(data.get("standard", "c++20"))
            if not source.strip():
                raise ValueError("Chưa có mã C++ để biên dịch.")
            if standard not in {"c++11", "c++14", "c++17", "c++20", "c++23"}:
                standard = "c++20"
            if len(stdin) > 256 * 1024:
                raise ValueError("Dữ liệu đầu vào vượt quá 256 KB.")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.send_json(400, {"ok": False, "message": str(exc)})
            return

        ip = client_ip(self)
        if rate_limited(ip):
            self.send_json(429, {"ok": False, "message": "Bạn đã gửi quá nhiều yêu cầu. Vui lòng đợi một phút rồi thử lại."})
            return
        if not job_slots.acquire(blocking=False):
            self.send_json(503, {"ok": False, "message": "Máy chủ đang bận. Vui lòng thử lại sau ít phút."})
            return

        job_id = uuid.uuid4().hex
        job_dir = JOBS_DIR / job_id
        host_job_dir = JOBS_HOST_DIR / job_id
        container_name = "cppstudio-" + job_id[:16]
        try:
            job_dir.mkdir(mode=0o777)
            os.chmod(job_dir, 0o777)
            (job_dir / "main.cpp").write_text(source, encoding="utf-8")
            (job_dir / "stdin.txt").write_text(stdin, encoding="utf-8")
            os.chmod(job_dir / "main.cpp", 0o644)
            os.chmod(job_dir / "stdin.txt", 0o644)

            command = [
                "docker", "run", "--rm", "--name", container_name,
                "--network", "none",
                "--memory", "512m", "--memory-swap", "512m", "--cpus", "1.0",
                "--pids-limit", "64", "--read-only",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
                "--cap-drop", "ALL", "--cap-add", "SYS_PTRACE",
                "--security-opt", "no-new-privileges",
                "--ulimit", "nofile=128:128",
                "--user", "10001:10001", "--workdir", "/work",
                "--env", "HOME=/tmp", "--env", "TMPDIR=/tmp", "--env", "LANG=C.UTF-8",
                "--mount", f"type=bind,source={host_job_dir},target=/work",
            ]
            if RUNNER_RUNTIME:
                command.extend(["--runtime", RUNNER_RUNTIME])
            mode = "trace" if self.path == "/api/trace" else "run"
            command.extend([
                RUNNER_IMAGE, "/opt/cppstudio/worker.py",
                "/work/main.cpp", "/work/stdin.txt", standard, mode,
            ])
            timeout = 58 if mode == "trace" else 16
            try:
                result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "kill", container_name], capture_output=True, timeout=3)
                subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=3)
                self.send_json(200, {"ok": False, "message": "Tác vụ vượt quá thời gian cho phép và đã bị dừng.", "steps": [], "stdout": ""})
                return

            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "Runner không trả về kết quả.")[-4000:]
                self.send_json(503, {"ok": False, "message": "Không thể chạy môi trường G++/GDB: " + detail, "steps": []})
                return
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                self.send_json(502, {"ok": False, "message": "Runner trả về dữ liệu không hợp lệ.", "debug_output": (result.stdout or "")[-2000:]})
                return
            self.send_json(200, payload)
        except Exception as exc:
            self.send_json(500, {"ok": False, "message": "Lỗi backend: " + str(exc), "steps": []})
        finally:
            try:
                shutil.rmtree(job_dir, ignore_errors=True)
            finally:
                job_slots.release()


if __name__ == "__main__":
    print(f"C++ Studio API listening on {HOST}:{PORT}", flush=True)
    print(f"Allowed browser origins: {', '.join(sorted(ALLOWED_ORIGINS)) or '(none)'}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
