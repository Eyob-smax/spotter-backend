from __future__ import annotations

import hashlib
import logging
import uuid

from django.core.cache import cache
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from planner.domain.risk import describe_rules
from planner.domain.sampling import ALLOWED_INTERVALS_MI
from planner.services import plans

from . import serializers as s

logger = logging.getLogger(__name__)

GEOCODE_TTL_S = 24 * 60 * 60
ERROR = OpenApiResponse(OpenApiTypes.OBJECT, description='Error envelope: {"error": {"code", "message", ...}}')


class ScopedAPIView(APIView):
    throttle_classes = [ScopedRateThrottle]


class HealthView(APIView):
    @extend_schema(summary="Liveness and cache check", responses={200: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT})
    def get(self, request):
        probe = f"health:{uuid.uuid4().hex}"
        try:
            cache.set(probe, 1, timeout=5)
            cache_ok = cache.get(probe) == 1
            cache.delete(probe)
        except Exception:  # noqa: BLE001 - report, don't crash the health endpoint
            logger.exception("Cache health check failed")
            cache_ok = False
        status = "ok" if cache_ok else "degraded"
        return Response({"status": status, "cache": "ok" if cache_ok else "error"}, status=200 if cache_ok else 503)


class MetaView(APIView):
    @extend_schema(summary="Risk rules, ranking order and input limits", responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        return Response(
            {
                **describe_rules(),
                "ranking": ["no_travel_mi", "severe_mi", "high_mi", "avg_risk", "duration_s"],
                "intervals_mi": list(ALLOWED_INTERVALS_MI),
                "default_interval_mi": 25,
                "load_lb": {"min": s.MIN_LOAD_LB, "max": s.MAX_LOAD_LB},
                "max_departure_days": s.MAX_DEPARTURE_DAYS,
                "heatmap_hours": plans.HEATMAP_HOURS,
            }
        )


class GeocodeSearchView(ScopedAPIView):
    throttle_scope = "geocode"

    @extend_schema(
        summary="Address / place autocomplete",
        parameters=[s.SearchQuerySerializer],
        responses={200: OpenApiTypes.OBJECT, 400: ERROR, 502: ERROR},
    )
    def get(self, request):
        query = s.SearchQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        q = query.validated_data["q"]
        bias = (query.validated_data["lat"], query.validated_data["lon"]) if "lat" in query.validated_data else None
        raw_key = f"{q.lower()}|{'-' if bias is None else f'{bias[0]:.1f},{bias[1]:.1f}'}"
        key = "geo:s:" + hashlib.sha1(raw_key.encode()).hexdigest()
        results = cache.get(key)
        if results is None:
            results = plans.tomtom_client().search(q, bias=bias)
            cache.set(key, results, timeout=GEOCODE_TTL_S)
        return Response({"results": results})


class ReverseGeocodeView(ScopedAPIView):
    throttle_scope = "geocode"

    @extend_schema(
        summary="Coordinates to a readable address",
        parameters=[s.ReverseQuerySerializer],
        responses={200: OpenApiTypes.OBJECT, 400: ERROR, 502: ERROR},
    )
    def get(self, request):
        query = s.ReverseQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        point = (query.validated_data["lat"], query.validated_data["lon"])
        key = f"geo:r:{point[0]:.4f},{point[1]:.4f}"
        result = cache.get(key)
        if result is None:
            result = plans.tomtom_client().reverse(point)
            cache.set(key, result, timeout=GEOCODE_TTL_S)
        return Response(result)


class RoutesView(ScopedAPIView):
    throttle_scope = "routes"

    @extend_schema(
        summary="Find 3 truck routes and create a trip plan",
        request=s.RouteRequestSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: ERROR, 422: ERROR, 502: ERROR, 504: ERROR},
    )
    def post(self, request):
        body = s.RouteRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        trip = plans.TripRequest(
            origin=dict(data["origin"]),
            destination=dict(data["destination"]),
            departure=data["departure_time"],
            load_lb=data["load_weight_lb"],
        )
        return Response(plans.create_plan(trip))


class AnalysisView(ScopedAPIView):
    throttle_scope = "analysis"

    @extend_schema(
        summary="Checkpoints, weather at ETA, risk and the recommendation",
        request=s.AnalysisRequestSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: ERROR, 404: ERROR, 502: ERROR},
    )
    def post(self, request, plan_id: str):
        body = s.AnalysisRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(plans.analyze(plan_id, int(body.validated_data["interval_mi"])))


class HeatmapView(ScopedAPIView):
    throttle_scope = "heatmap"

    @extend_schema(
        summary="Risk scores for the trip corridor, hourly for 48 h from departure",
        parameters=[OpenApiParameter("plan_id", str, OpenApiParameter.PATH)],
        responses={200: OpenApiTypes.OBJECT, 404: ERROR, 502: ERROR},
    )
    def get(self, request, plan_id: str):
        return Response(plans.heatmap(plan_id))
