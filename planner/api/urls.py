from django.urls import path, re_path

from . import views

PLAN_ID = r"(?P<plan_id>pl_[0-9a-f]{20})"

urlpatterns = [
    path("health", views.HealthView.as_view(), name="health"),
    path("meta", views.MetaView.as_view(), name="meta"),
    path("geocode/search", views.GeocodeSearchView.as_view(), name="geocode-search"),
    path("geocode/reverse", views.ReverseGeocodeView.as_view(), name="geocode-reverse"),
    path("routes", views.RoutesView.as_view(), name="routes"),
    re_path(rf"^plans/{PLAN_ID}/analysis$", views.AnalysisView.as_view(), name="plan-analysis"),
    re_path(rf"^plans/{PLAN_ID}/heatmap$", views.HeatmapView.as_view(), name="plan-heatmap"),
]
