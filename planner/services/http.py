"""Shared HTTP session for third-party APIs: connection pooling and bounded retries."""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "spotter-route/1.0 (+https://github.com/)"


def build_session(pool_size: int = 16, retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504)) -> requests.Session:
    retry = Retry(
        total=2,
        connect=2,
        read=1,
        status=2,
        backoff_factor=0.4,
        status_forcelist=retry_statuses,
        allowed_methods=frozenset({"GET"}),
        # Upstreams can ask for minute-long waits; we'd rather fail fast and let the user retry.
        respect_retry_after_header=False,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=pool_size)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers["User-Agent"] = USER_AGENT
    return session


_sessions: dict[str, requests.Session] = {}


def get_session(name: str = "default") -> requests.Session:
    """One pooled session per upstream. The weather session doesn't retry 429: its limit is per minute."""
    if name not in _sessions:
        statuses = (500, 502, 503, 504) if name == "weather" else (429, 500, 502, 503, 504)
        _sessions[name] = build_session(retry_statuses=statuses)
    return _sessions[name]
