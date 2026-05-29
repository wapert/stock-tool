"""
gunicorn_config.py — Pre-warms Shioaji in every worker after fork.
Each gunicorn worker process has its own module-level singletons.
The post_fork hook fires a background thread to log in immediately,
so by the time the first user request arrives the connection is ready.
"""
import threading

workers = 4   # Oracle 6GB RAM — 4 workers (~380MB each = ~1.5GB total)
bind    = "127.0.0.1:5050"
timeout = 300  # allow Gemini video analysis (can take 60-120s)


def post_fork(server, worker):
    """Called in each worker process right after it is forked."""
    def _prewarm():
        try:
            from shioaji_data import prewarm
            ok = prewarm()
            server.log.info(f"[worker {worker.pid}] Shioaji {'connected' if ok else 'skipped'}")
        except Exception as e:
            server.log.warning(f"[worker {worker.pid}] Shioaji prewarm: {e}")
    threading.Thread(target=_prewarm, daemon=True).start()
