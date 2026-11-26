#!/usr/bin/env python3
"""One-shot worker. Runs only inside a disposable, network-isolated container."""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
from pathlib import Path

import cppstudio_core as core

MAX_OUTPUT_CHARS = 120_000
MAX_TRACE_STEPS = 300


def clip(value: object) -> str:
    text = value if isinstance(value, str) else str(value or "")
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + "\n[Đã cắt bớt đầu ra vì vượt giới hạn.]"
    return text


def emit(payload: dict, exit_code: int = 0) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")
    raise SystemExit(exit_code)


def main() -> None:
    if len(sys.argv) != 5:
        emit({"ok": False, "message": "Tham số worker không hợp lệ."}, 2)

    source_path = Path(sys.argv[1]).resolve()
    stdin_path = Path(sys.argv[2]).resolve()
    standard = sys.argv[3]
    mode = sys.argv[4]
    if standard not in {"c++11", "c++14", "c++17", "c++20", "c++23"}:
        emit({"ok": False, "message": "Chuẩn C++ không được hỗ trợ."}, 2)
    if mode not in {"run", "trace"}:
        emit({"ok": False, "message": "Chế độ chạy không hợp lệ."}, 2)

    source = source_path.read_text(encoding="utf-8", errors="replace")
    stdin = stdin_path.read_text(encoding="utf-8", errors="replace") if stdin_path.exists() else ""
    workdir = str(source_path.parent)
    compiled, error = core.compile_source(source, standard, workdir, debug=(mode == "trace"))
    if error:
        emit({
            "ok": False,
            "message": error,
            "compiler_output": clip((compiled or {}).get("compiler_output", "")),
            "stdout": "",
            "stderr": "",
            "steps": [],
        })

    if mode == "run":
        try:
            result = subprocess.run(
                [compiled["exe"]], input=stdin, text=True, capture_output=True,
                encoding="utf-8", errors="replace", timeout=core.RUN_TIMEOUT,
                cwd=workdir, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp", "LANG": "C.UTF-8"},
            )
            emit({
                "ok": result.returncode == 0,
                "message": "G++ biên dịch và chạy xong." if result.returncode == 0 else "Chương trình kết thúc với lỗi.",
                "compiler_output": clip(compiled["compiler_output"]),
                "stdout": clip(result.stdout),
                "stderr": clip(result.stderr),
                "exit_code": result.returncode,
            })
        except subprocess.TimeoutExpired as exc:
            emit({
                "ok": False,
                "message": "Chương trình chạy quá thời gian.",
                "compiler_output": clip(compiled["compiler_output"]),
                "stdout": clip(exc.stdout or ""),
                "stderr": clip(exc.stderr or ""),
                "exit_code": None,
            })

    gdb = shutil.which("gdb")
    if not gdb:
        emit({"ok": False, "message": "Môi trường chạy chưa có GDB.", "compiler_output": clip(compiled["compiler_output"]), "steps": []}, 2)
    try:
        tracer = core.NativeTracer(gdb, compiled["exe"], compiled["source"], str(stdin_path), max_steps=MAX_TRACE_STEPS)
        trace = tracer.run()
        emit({
            "ok": bool(trace.get("steps")),
            "compiler_output": clip(compiled["compiler_output"]),
            "stderr": "",
            "exit_code": trace.get("exit_code"),
            "steps": trace.get("steps", []),
            "stdout": clip(trace.get("stdout", "")),
            "message": trace.get("message", "GDB đã thu thập trạng thái thực thi."),
            "debug_output": clip(trace.get("debug_output", "")),
        })
    except Exception as exc:
        emit({
            "ok": False,
            "message": "G++ đã biên dịch nhưng GDB không thể tạo mô phỏng: " + str(exc),
            "compiler_output": clip(compiled["compiler_output"]),
            "steps": [],
            "stdout": "",
        })


if __name__ == "__main__":
    main()
