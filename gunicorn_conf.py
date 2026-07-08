"""Gunicorn config for production serving — this is step 2 of the scale path (multiple app workers).

Run it with:   gunicorn -c gunicorn_conf.py app.main:app

Why gunicorn + uvicorn workers: FastAPI is ASGI. One uvicorn process uses ~one CPU core (Python's
GIL). Gunicorn supervises several uvicorn worker processes so the app uses every core on the box and
survives a worker crash (it respawns). The app is stateless (identity is in the JWT), so these
workers are interchangeable and you can also run many *containers* of this behind a load balancer
(see docker-compose.prod.yml + nginx.conf) to scale across machines.

Connection budget (important when going wide): each worker holds up to
(db_pool_size + db_max_overflow) Postgres connections. Total at peak ~= workers x that. Keep it under
Postgres `max_connections` (the prod compose raises it to 200) or put pgbouncer in front.
"""
import multiprocessing
import os

# bind / sockets. Platforms like Render/Heroku/Fly inject the port to listen on via $PORT, so honour
# it: explicit BIND wins, else bind 0.0.0.0:$PORT when the platform set one, else the local default.
_port = os.getenv("PORT")
bind = os.getenv("BIND") or (f"0.0.0.0:{_port}" if _port else "0.0.0.0:8000")

# Worker processes. One uvicorn worker per core is the right default for an async server; each worker
# is a single event loop, and FastAPI runs the sync DB endpoints in a per-worker threadpool, so one
# worker still serves many concurrent requests. In containers, CPU count can read the host's cores,
# so SET WEB_CONCURRENCY EXPLICITLY in production (e.g. 4) rather than trusting the default.
workers = int(os.getenv("WEB_CONCURRENCY", multiprocessing.cpu_count()))
worker_class = "uvicorn.workers.UvicornWorker"

# Recycle workers periodically so any slow memory leak can't accumulate over days of uptime. The
# jitter staggers recycles so they don't all happen at once.
max_requests = int(os.getenv("MAX_REQUESTS", "10000"))
max_requests_jitter = int(os.getenv("MAX_REQUESTS_JITTER", "1000"))

# Timeouts. `timeout` kills a worker wedged on a single request (a runaway query). `graceful_timeout`
# is how long a worker gets to finish in-flight requests on shutdown/reload. `keepalive` holds idle
# client connections briefly so a load balancer can reuse them.
timeout = int(os.getenv("TIMEOUT", "60"))
graceful_timeout = int(os.getenv("GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.getenv("KEEPALIVE", "5"))

# Logs to stdout/stderr so the container/platform log driver captures them.
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info")

# Preload the app so workers share import work (copy-on-write memory) and start faster.
preload_app = os.getenv("PRELOAD", "true").lower() in ("1", "true", "yes")


def post_fork(server, worker):
    """CRITICAL with preload_app: the master imports app.db and creates the SQLAlchemy engine/pool
    before forking. A forked child inherits the parent's live socket file descriptors — if two
    workers use the same inherited DB connection you get corrupted/SSL-broken connections. Disposing
    the engine here drops any inherited connections so each worker lazily opens its OWN, cleanly."""
    try:
        from app.db import engine
        engine.dispose()
    except Exception:  # pragma: no cover - never let a hook crash a worker
        pass
