from datetime import UTC, timedelta

from django.utils import timezone
from rest_framework import serializers

from planner.domain.geo import haversine_m
from planner.domain.sampling import ALLOWED_INTERVALS_MI

MIN_LOAD_LB = 1_000
MAX_LOAD_LB = 120_000
MAX_DEPARTURE_DAYS = 7
PAST_GRACE = timedelta(minutes=15)
MIN_TRIP_M = 500


class PointSerializer(serializers.Serializer):
    lat = serializers.FloatField(min_value=-90, max_value=90)
    lon = serializers.FloatField(min_value=-180, max_value=180)
    label = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")


class RouteRequestSerializer(serializers.Serializer):
    origin = PointSerializer()
    destination = PointSerializer()
    departure_time = serializers.DateTimeField(
        help_text="ISO 8601. Naive values are read as UTC. Up to 15 minutes in the past is treated as now."
    )
    load_weight_lb = serializers.IntegerField(min_value=MIN_LOAD_LB, max_value=MAX_LOAD_LB)

    def validate_departure_time(self, value):
        if timezone.is_naive(value):
            value = value.replace(tzinfo=UTC)
        value = value.astimezone(UTC)
        now = timezone.now()
        if value < now - PAST_GRACE:
            raise serializers.ValidationError("Departure time is in the past.")
        if value > now + timedelta(days=MAX_DEPARTURE_DAYS):
            raise serializers.ValidationError(
                f"Departure must be within {MAX_DEPARTURE_DAYS} days; forecasts beyond that aren't reliable."
            )
        return max(value, now).replace(second=0, microsecond=0)

    def validate(self, attrs):
        o, d = attrs["origin"], attrs["destination"]
        if haversine_m((o["lat"], o["lon"]), (d["lat"], d["lon"])) < MIN_TRIP_M:
            raise serializers.ValidationError({"destination": "Destination must be at least 500 m from the origin."})
        return attrs


class AnalysisRequestSerializer(serializers.Serializer):
    interval_mi = serializers.ChoiceField(choices=ALLOWED_INTERVALS_MI, default=25)


class SearchQuerySerializer(serializers.Serializer):
    q = serializers.CharField(min_length=2, max_length=120, trim_whitespace=True)
    lat = serializers.FloatField(min_value=-90, max_value=90, required=False)
    lon = serializers.FloatField(min_value=-180, max_value=180, required=False)

    def validate(self, attrs):
        if ("lat" in attrs) != ("lon" in attrs):
            raise serializers.ValidationError({"lat": "Provide both lat and lon, or neither."})
        return attrs


class ReverseQuerySerializer(serializers.Serializer):
    lat = serializers.FloatField(min_value=-90, max_value=90)
    lon = serializers.FloatField(min_value=-180, max_value=180)
