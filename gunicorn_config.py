"""
gunicorn_config.py — Pre-warms Shioaji in every worker after fork.
Each gunicorn worker process has its own module-level singletons.
The post_fork hook fires a background thread to log in immediately,
so by the time the first user request arrives the connection is ready.
"""
import threading

workers = 1   # single worker: all caches shared, ThreadPoolExecutor handles internal parallelism
bind    = "127.0.0.1:5050"
timeout = 120


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
