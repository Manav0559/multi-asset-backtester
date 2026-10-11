"""Child-process side of the BYOC sandbox.

Started ONLY by app.backtest.sandbox._run_worker as

    python -I -B -c <bootstrap> <backend_root>

with an explicit, minimal environment (nothing from the web process), a
throwaway working directory, and its own process group (the parent SIGKILLs
the group on wall-clock timeout).

Protocol: one pickled request dict on stdin (written by the trusted parent),
one JSON line on stdout. User code never sees the real stdout: `print` is
pointed at /dev/null before user code runs, and the reply is plain JSON so a
compromised child can at worst lie about its own weights.

Order matters: everything this process needs is imported BEFORE the rlimits
and the audit hook go on; the audit hook cannot be removed once installed.
"""
from __future__ import annotations

import json
import os
import pickle
import sys
import warnings

MAX_ERROR_CHARS = 2000

#: audit events (PEP 578) user code has no business triggering. Prefix match.
_DENIED_EVENT_PREFIXES = (
    "socket.", "subprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn",
    "os.fork", "os.forkpty", "os.kill", "os.killpg", "pty.", "ctypes.", "mmap.",
    "os.remove", "os.unlink", "os.rename", "os.replace", "os.rmdir", "os.mkdir",
    "os.chmod", "os.chown", "os.chflags", "os.link", "os.symlink", "os.truncate",
    "os.utime", "os.putenv", "os.unsetenv", "os.startfile", "shutil.",
    "urllib.", "http.client.", "ftplib.", "smtplib.", "poplib.", "imaplib.",
    "nntplib.", "telnetlib.", "webbrowser.", "sqlite3.", "pickle.find_class",
    "marshal.load", "builtins.input", "resource.setrlimit", "resource.prlimit",
    "sys.remote_exec",
)

_WRITE_FLAGS = (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC)


def _apply_rlimits(cpu_s: int, memory_mb: int) -> dict:
    """Best-effort kernel limits. Returns the soft limits actually set (macOS
    accepts but does not enforce RLIMIT_AS; Linux enforces all of these)."""
    import resource

    applied: dict[str, int] = {}

    def _set(name: str, soft: int, hard: int | None = None) -> None:
        lim = getattr(resource, name, None)
        if lim is None:
            return
        hard = soft if hard is None else hard
        try:
            _, cur_hard = resource.getrlimit(lim)
            if cur_hard != resource.RLIM_INFINITY:
                hard = min(hard, cur_hard)
                soft = min(soft, hard)
            resource.setrlimit(lim, (soft, hard))
            applied[name] = soft
        except (ValueError, OSError):
            pass

    _set("RLIMIT_CPU", cpu_s, cpu_s + 1)          # SIGXCPU at soft, SIGKILL at hard
    _set("RLIMIT_AS", memory_mb * 1024 * 1024)    # address space (memory)
    _set("RLIMIT_FSIZE", 0)                       # cannot grow any file
    _set("RLIMIT_CORE", 0)                        # no core dumps
    _set("RLIMIT_NPROC", 0)                       # no fork()/new processes
    return applied


def _allowed_read_roots(backend_root: str) -> tuple[str, ...]:
    """Directories the child may READ from: the Python installation and
    site-packages (lazy imports, tz data) plus the `app` package source.
    The backend root itself (where a .env could live) is NOT included."""
    backend_real = os.path.realpath(backend_root)
    candidates = [sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix,
                  os.path.join(backend_root, "app"), "/usr/share/zoneinfo"]
    candidates += [p for p in sys.path
                   if p and os.path.realpath(p) != backend_real]
    roots: set[str] = set()
    for p in candidates:
        for form in (os.path.abspath(p), os.path.realpath(p)):
            if form != backend_real and form != os.sep:
                roots.add(form.rstrip(os.sep))
    return tuple(sorted(roots))


def _install_audit_hook(read_roots: tuple[str, ...]) -> None:
    prefixes = tuple(r + os.sep for r in read_roots)

    def _open_allowed(path, mode, flags) -> bool:
        if isinstance(mode, str) and any(c in mode for c in "wax+"):
            return False
        if isinstance(flags, int) and flags & _WRITE_FLAGS:
            return False
        if isinstance(path, int):            # re-wrapping an already-open fd
            return True
        try:
            p = os.path.abspath(os.fsdecode(path))
        except Exception:  # noqa: BLE001
            return False
        return p in read_roots or p.startswith(prefixes)

    def hook(event: str, args: tuple) -> None:
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            if not _open_allowed(path, mode, flags):
                raise PermissionError(f"sandbox: file access to {path!r} is not permitted")
        elif event.startswith(_DENIED_EVENT_PREFIXES):
            raise PermissionError(f"sandbox: '{event}' is not permitted")

    sys.addaudithook(hook)


def _probe(applied: dict) -> dict:
    """What a strategy would run into. Never executes user code."""
    import resource
    import socket
    import subprocess

    def _try(fn) -> str:
        try:
            fn()
            return "allowed"
        except PermissionError:
            return "blocked"
        except Exception as exc:  # noqa: BLE001
            return f"error:{type(exc).__name__}"

    def _read_outside():
        with open("/etc/hosts", "rb") as fh:
            fh.read(1)

    def _write_cwd():
        with open("probe.txt", "w") as fh:
            fh.write("x")

    def _socket():
        socket.create_connection(("127.0.0.1", 9), timeout=1).close()

    def _subprocess():
        subprocess.run(["/bin/true"], check=False)  # noqa: S603

    limits = {}
    for name in ("RLIMIT_CPU", "RLIMIT_AS", "RLIMIT_FSIZE", "RLIMIT_NPROC"):
        lim = getattr(resource, name, None)
        if lim is not None:
            limits[name] = resource.getrlimit(lim)[0]
    return {
        "ok": True,
        "env": sorted(os.environ),
        "rlimits_applied": applied,
        "rlimits": limits,
        "checks": {
            "read_outside_python": _try(_read_outside),
            "write_file": _try(_write_cwd),
            "network": _try(_socket),
            "subprocess": _try(_subprocess),
        },
    }


def main() -> None:
    reply_stream = sys.stdout.buffer
    request = pickle.load(sys.stdin.buffer)      # trusted: written by the parent
    backend_root = sys.argv[1]

    # User print() goes nowhere; the reply channel stays private to us.
    sys.stdout = open(os.devnull, "w")  # noqa: SIM115 — lives for the process
    warnings.simplefilter("ignore")

    # Import everything user code can reach BEFORE the guards go on.
    from app.backtest import sandbox

    op = request.get("op")
    limits = request.get("limits") or {}
    applied = _apply_rlimits(int(limits.get("cpu_s", 60)), int(limits.get("memory_mb", 2048)))
    _install_audit_hook(_allowed_read_roots(backend_root))

    try:
        if op == "check":
            strategy = sandbox.build_custom_strategy(request["source"], request.get("params"))
            reply = {"ok": True, "class_name": type(strategy).__name__}
        elif op == "run":
            strategy = sandbox.build_custom_strategy(request["source"], request.get("params"))
            weights = sandbox.run_custom_strategy(strategy, request["data"])
            reply = {"ok": True, "class_name": type(strategy).__name__,
                     "weights": [float(w) for w in weights.to_numpy(dtype=float)]}
        elif op == "probe":
            reply = _probe(applied)
        else:
            reply = {"ok": False, "error": f"unknown sandbox op {op!r}"}
    except sandbox.SandboxError as exc:
        reply = {"ok": False, "error": str(exc)[:MAX_ERROR_CHARS]}
    except MemoryError:
        reply = {"ok": False, "error": "strategy exceeded the sandbox memory limit"}
    except Exception as exc:  # noqa: BLE001
        reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]}

    reply_stream.write(json.dumps(reply).encode() + b"\n")
    reply_stream.flush()
