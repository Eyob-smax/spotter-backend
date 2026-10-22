from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path("api/", include("planner.api.urls")),
    path("api/schema", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]

handler404 = "planner.api.errors.json_404"
handler500 = "planner.api.errors.json_500"
handler400 = "planner.api.errors.json_400"
handler403 = "planner.api.errors.json_403"
