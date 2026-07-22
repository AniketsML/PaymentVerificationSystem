#!/usr/bin/env python
"""
ONE COMMAND TO RUN THE WHOLE SYSTEM.

    python run.py                # start everything that's configured
    python run.py --check        # preflight only: tell me what's wrong, start nothing
    python run.py --port 8010    # use another port
    python run.py --no-browser   # don't open the browser

What it starts:
    web console + API      always
    worker pool            always (in-process, WORKER_COUNT threads)
    automated ingestion    only when SOURCE_MODE is configured (otherwise inert)

Why this exists: every failure people actually hit (Postgres down, port busy, no .env,
missing deps) used to surface as a stack trace mid-boot. Here each one is checked FIRST
and reported as a plain sentence with the fix. Nothing starts until the checks pass.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# ── tiny console helpers (no deps — this must run before deps are verified) ────
# Windows consoles default to cp1252, which cannot encode ✓/✗/─ and would crash the
# launcher on its own first line. Force UTF-8 where possible, and fall back to ASCII
# markers where it isn't — the launcher must never die trying to print.
try:
    sys.stdout.reconfigure(encoding="utf-8")        # type: ignore[attr-defined]
except Exception:
    pass


def _printable(sample: str) -> bool:
    try:
        sample.encode(sys.stdout.encoding or "ascii")
        return True
    except Exception:
        return False


_UNI = _printable("✓─")
_C = sys.stdout.isatty()
def _c(code, s): return f"\033[{code}m{s}\033[0m" if _C else s
def bold(s):     return _c("1", s)
def dim(s):      return _c("2", s)
def green(s):    return _c("32", s)
def red(s):      return _c("31", s)
def yellow(s):   return _c("33", s)
def blue(s):     return _c("34", s)

RULE = "─" if _UNI else "-"
OK = green("✓" if _UNI else "[ok]")
WARN = yellow("!" if _UNI else "[!]")
BAD = red("✗" if _UNI else "[x]")
_problems: list[str] = []
_warnings: list[str] = []


def check(label: str, ok: bool, detail: str = "", fix: str = "", fatal: bool = True) -> bool:
    mark = OK if ok else (BAD if fatal else WARN)
    line = f"  {mark} {label}"
    if detail:
        line += dim(f" — {detail}")
    print(line)
    if not ok:
        msg = f"{label}: {detail}" + (f"\n      fix: {fix}" if fix else "")
        (_problems if fatal else _warnings).append(msg)
    return ok


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) != 0


def first_free_port(start: int, tries: int = 20) -> int | None:
    for p in range(start, start + tries):
        if port_free(p):
            return p
    return None


# ── preflight ─────────────────────────────────────────────────────────────────
def preflight(port: int) -> tuple[bool, int]:
    print(bold("\n  Preflight\n"))

    # 1. python version
    v = sys.version_info
    check("Python 3.11+", v >= (3, 11), f"found {v.major}.{v.minor}.{v.micro}",
          "install Python 3.11 or newer")

    # 2. dependencies
    missing = []
    for mod, pkg in (("flask", "flask"), ("waitress", "waitress"), ("psycopg", "psycopg[binary]"),
                     ("psycopg_pool", "psycopg_pool"), ("PIL", "Pillow"), ("pandas", "pandas"),
                     ("numpy", "numpy"), ("requests", "requests"), ("dateutil", "python-dateutil")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    deps_ok = check("Dependencies installed", not missing,
                    "missing: " + ", ".join(missing) if missing else "all present",
                    "pip install -r requirements.txt   (or use run.bat / run.sh)")
    if not deps_ok:
        return False, port

    # 3. .env
    env_path = os.path.join(BASE, ".env")
    if not os.path.exists(env_path):
        example = os.path.join(BASE, ".env.example")
        if os.path.exists(example):
            import shutil
            shutil.copyfile(example, env_path)
            check(".env file", False, "was missing — created from .env.example",
                  "open .env and fill in DATABASE_URL, MEDHA_API_KEY, PV_AUTH_USER/PASS",
                  fatal=False)
        else:
            check(".env file", False, "not found", "create .env (see .env.example)", fatal=False)
    else:
        check(".env file", True, "loaded")

    from config import settings   # safe: reads env only, no DB

    # 4. database
    db_ok, db_err = False, ""
    try:
        from db import pg
        with pg.pool().connection() as c:
            c.execute("SELECT 1")
        db_ok = True
    except Exception as e:  # noqa: BLE001
        db_err = f"{type(e).__name__}: {str(e).splitlines()[0][:120]}"
    safe_url = settings.DATABASE_URL.split("@")[-1] if "@" in settings.DATABASE_URL else settings.DATABASE_URL
    check("PostgreSQL reachable", db_ok, safe_url if db_ok else db_err,
          "start Postgres and check DATABASE_URL in .env")

    # 5. schema
    if db_ok:
        try:
            from db import pg
            pg.init_schema()
            check("Database schema ready", True, "tables + migrations applied")
        except Exception as e:  # noqa: BLE001
            check("Database schema ready", False, f"{type(e).__name__}: {e}",
                  "check the DB user has CREATE rights")

    # 6. port
    if not port_free(port):
        alt = first_free_port(port + 1)
        if alt:
            check(f"Port {port} available", False, f"busy — will use {alt} instead",
                  f"free port {port}, or run with --port {port}", fatal=False)
            port = alt
        else:
            check(f"Port {port} available", False, "busy, and no free port nearby",
                  "stop whatever is using it, or pass --port")
    else:
        check(f"Port {port} available", True)

    # 7. model endpoint (warn only — the console runs, verification needs it)
    from config import runtime
    try:
        mc = runtime.model_config()
        has_model = bool(mc.get("url"))
        check("Vision model endpoint", has_model,
              f"{mc.get('model')} @ {mc.get('url')}" if has_model else "no MEDHA_API_URL",
              "set MEDHA_API_URL / MEDHA_API_KEY in .env (or edit it in the console)",
              fatal=False)
    except Exception:
        check("Vision model endpoint", False, "could not read config", fatal=False)

    # 8. auth (warn only)
    auth_on = bool(settings.AUTH_USER and settings.AUTH_PASS)
    check("Console login", auth_on,
          f"enabled for {settings.AUTH_USER}" if auth_on else "DISABLED — console is open to anyone",
          "set PV_AUTH_USER and PV_AUTH_PASS in .env", fatal=False)
    if auth_on and len(settings.AUTH_PASS) < 12:
        _warnings.append(f"Console password is only {len(settings.AUTH_PASS)} characters — "
                         "use 16+ random characters in production")

    return not _problems, port


def banner(port: int, ingest_on: bool) -> None:
    from config import settings
    from config import runtime
    mc = runtime.model_config()
    url = f"http://localhost:{port}"
    line = RULE * 58
    print("\n" + blue(line))
    print("  " + bold("Payment Verification Console") + dim("  ·  running"))
    print(blue(line))
    print(f"  {bold('Open')}       {green(url)}")
    if settings.AUTH_USER:
        print(f"  {bold('Login')}      {settings.AUTH_USER}")
    print(f"  {bold('Workers')}    {settings.WORKER_COUNT} threads")
    print(f"  {bold('Model')}      {mc.get('model')} @ {dim(mc.get('url') or 'not set')}")
    print(f"  {bold('Ingestion')}  " + (green(f"ON · source '{settings.SOURCE_NAME}' "
                                              f"every {settings.INGEST_INTERVAL}s")
                                        if ingest_on else dim("off (SOURCE_MODE not set)")))
    print(blue(line))
    print(dim("  Ctrl+C to stop\n"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the Payment Verification System.")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--check", action="store_true", help="run preflight checks only")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser")
    ap.add_argument("--no-ingest", action="store_true", help="don't start the ingester")
    args = ap.parse_args()

    print(bold("\n  Payment Verification System"))
    ok, port = preflight(args.port)

    if _warnings:
        print(yellow("\n  Warnings"))
        for w in _warnings:
            print(f"    {WARN} {w}")
    if not ok:
        print(red("\n  Cannot start — fix these first:\n"))
        for p in _problems:
            print(f"    {BAD} {p}")
        print()
        return 1
    if args.check:
        print(green("\n  All checks passed. (--check: not starting)\n"))
        return 0

    os.environ["PORT"] = str(port)          # so anything reading env agrees with us

    from config import settings
    ingest_on = bool(settings.SOURCE_MODE) and not args.no_ingest

    # importing the app boots schema + the in-process worker pool
    from app.server import app

    if ingest_on:
        import threading
        from ingest import ingester
        threading.Thread(target=ingester.run, kwargs={"source": settings.SOURCE_NAME},
                         daemon=True, name="ingester").start()

    banner(port, ingest_on)

    if not args.no_browser:
        import threading
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}")).start()

    from waitress import serve
    try:
        serve(app, host=args.host, port=port, threads=settings.WEB_THREADS, _quiet=True)
    except KeyboardInterrupt:
        pass
    print(dim("\n  Stopped.\n"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
