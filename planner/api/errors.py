"""Uniform error envelope: {"error": {"code", "message", ...}} for every failure."""

from __future__ import annotations

import logging

from django.http import JsonResponse
from rest_framework import exceptions as drf
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from planner.exceptions import PlannerError

logger = logging.getLogger(__name__)


def _flatten(detail, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(detail, dict):
        for key, value in detail.items():
            name = prefix if key == "non_field_errors" else (f"{prefix}.{key}" if prefix else str(key))
            out.update(_flatten(value, name))
    elif isinstance(detail, list):
        for item in detail:
            for key, value in _flatten(item, prefix).items():
                out.setdefault(key, value)
    else:
        out[prefix or "non_field_errors"] = str(detail)
    return out


def error_body(code: str, message: str, **extra) -> dict:
    return {"error": {"code": code, "message": message, **extra}}


def exception_handler(exc, context):
    if isinstance(exc, PlannerError):
        if exc.status >= 500:
            logger.warning("%s: %s", exc.code, exc.message)
        return Response({"error": exc.to_dict()}, status=exc.status)

    if isinstance(exc, drf.ValidationError):
        fields = _flatten(exc.detail)
        first = next(iter(fields.values()), "Invalid request.")
        return Response(error_body("validation_error", first, fields=fields), status=400)
    if isinstance(exc, drf.Throttled):
        wait = int(exc.wait or 1)
        resp = Response(
            error_body("rate_limited", f"Too many requests. Try again in {wait} s.", retry_after=wait), status=429
        )
        resp["Retry-After"] = str(wait)
        return resp

    response = drf_exception_handler(exc, context)
    if response is not None:
        code = {
            drf.NotFound: "not_found",
            drf.MethodNotAllowed: "method_not_allowed",
            drf.ParseError: "bad_request",
            drf.UnsupportedMediaType: "unsupported_media_type",
            drf.NotAcceptable: "not_acceptable",
        }.get(type(exc), "bad_request")
        message = str(getattr(exc, "detail", "")) or "Bad request."
        response.data = error_body(code, message)
        return response

    logger.exception("Unhandled error")
    return Response(error_body("internal_error", "Something went wrong. Please try again."), status=500)


def json_404(request, exception=None):
    return JsonResponse(error_body("not_found", "Not found."), status=404)


def json_500(request):
    return JsonResponse(error_body("internal_error", "Something went wrong. Please try again."), status=500)


def json_400(request, exception=None):
    return JsonResponse(error_body("bad_request", "Bad request."), status=400)


def json_403(request, exception=None):
    return JsonResponse(error_body("forbidden", "Forbidden."), status=403)
