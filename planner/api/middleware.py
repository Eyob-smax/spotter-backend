"""Request-id propagation: every log line and error response carries the id of the request."""

import contextvars
import logging
import uuid

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def current_request_id() -> str:
    return _request_id.get()


class RequestIdMiddleware:
    header = "HTTP_X_REQUEST_ID"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get(self.header, "")
        request_id = incoming if 0 < len(incoming) <= 64 and incoming.isprintable() else uuid.uuid4().hex[:12]
        token = _request_id.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            _request_id.reset(token)
        response["X-Request-ID"] = request_id
        return response


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
        return True
