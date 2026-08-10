"""Production entry point for the reader, run by the TranslateBooksReader task.

The scheduled task runs with "Run whether user is logged on or not", so there
is no console: every ``print()`` and every traceback in the app would otherwise
go to a handle that does not exist. ``logs/web_ui.log`` is the only window into
a running service — the logging setup here is load-bearing, not a nicety.

Dev still uses ``python -m web_ui.app`` (set ``BOOKS_DEBUG=1`` for auto-reload).
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Must precede the web_ui import: the task has no "Start in" directory, so the
# cwd is whatever Windows hands us (typically C:\Windows\system32), and
# `import web_ui.app` would fail outright.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Loopback only — `tailscale serve` is the single door in. Flip to "0.0.0.0"
# for a one-off LAN fallback if the tunnel is misbehaving.
HOST = "127.0.0.1"
PORT = 5000

# Not arbitrary: waitress is thread-per-connection from a fixed pool, and the
# batch-translate SSE stream holds its thread from batch_start to
# batch_complete. Waitress's default of 4 would let two dashboard tabs plus a
# phone starve the pool and make the server look hung.
THREADS = 16

LOG_PATH = REPO_ROOT / "logs" / "web_ui.log"


class _LogStream:
    """File-like shim so bare ``print()`` calls land in the log file.

    Resolves ``handler.stream`` on every write rather than capturing it, so
    output keeps flowing after the handler rotates the file out from under us.
    """

    def __init__(self, handler: logging.Handler) -> None:
        self._handler = handler

    def write(self, text: str) -> int:
        stream = self._handler.stream
        stream.write(text)
        stream.flush()
        return len(text)

    def flush(self) -> None:
        self._handler.stream.flush()

    def isatty(self) -> bool:
        return False


def _setup_logging() -> logging.Handler:
    """Attach a rotating file handler to the root logger."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_PATH, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("waitress").setLevel(logging.INFO)
    return handler


def main() -> None:
    handler = _setup_logging()

    # web_ui/app.py prints its banner and a handful of diagnostics to stdout.
    stream = _LogStream(handler)
    sys.stdout = stream
    sys.stderr = stream

    from flask.logging import default_handler

    from web_ui.app import app, _print_access_urls

    # Flask's default handler writes to the WSGI error stream, which is now the
    # same file the root handler writes to; dropping it avoids doubled lines.
    app.logger.removeHandler(default_handler)
    app.logger.setLevel(logging.INFO)

    from waitress import serve

    log = logging.getLogger("serve")
    log.info("Starting waitress on %s:%s (threads=%s)", HOST, PORT, THREADS)
    _print_access_urls(PORT, host=HOST)
    serve(app, host=HOST, port=PORT, threads=THREADS, ident="translate-books")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A non-zero exit is what makes Task Scheduler restart us; the log line
        # is what makes the restart explicable afterwards.
        logging.getLogger("serve").exception("Server exited with an unhandled exception")
        raise
