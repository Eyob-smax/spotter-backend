"""Domain-level errors. The API layer renders them as {"error": {...}} with the given status."""

from __future__ import annotations


class PlannerError(Exception):
    code = "internal_error"
    status = 500
    default_message = "Something went wrong."

    def __init__(self, message: str | None = None, *, field: str | None = None, **extra):
        super().__init__(message or self.default_message)
        self.message = message or self.default_message
        self.field = field
        self.extra = extra

    def to_dict(self) -> dict:
        body = {"code": self.code, "message": self.message}
        if self.field:
            body["field"] = self.field
        body.update(self.extra)
        return body


class PointNotRoutable(PlannerError):
    code = "point_not_routable"
    status = 422
    default_message = "No truck-accessible road near this point."


class NoRouteFound(PlannerError):
    code = "no_route_found"
    status = 422
    default_message = "No truck route exists between these points."


class PlanExpired(PlannerError):
    code = "plan_expired"
    status = 404
    default_message = "This trip plan has expired. Please search again."


class UpstreamTimeout(PlannerError):
    code = "upstream_timeout"
    status = 504
    default_message = "A data provider took too long to respond. Please try again."


class UpstreamUnavailable(PlannerError):
    code = "upstream_unavailable"
    status = 502
    default_message = "A data provider is unavailable right now. Please try again."
