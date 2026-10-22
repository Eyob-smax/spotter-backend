"""Gunicorn settings. Threads suit this I/O-bound app (it mostly waits on TomTom/Open-Meteo)."""

import os

bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
workers = int(os.getenv("WEB_CONCURRENCY", "2"))
worker_class = "gthread"
threads = int(os.getenv("GUNICORN_THREADS", "4"))
# Cross-country truck routing with alternatives can take ~20 s upstream.
timeout = 90
graceful_timeout = 30
keepalive = 5
max_requests = 1000
max_requests_jitter = 100
accesslog = "-"
errorlog = "-"
forwarded_allow_ips = "*"
