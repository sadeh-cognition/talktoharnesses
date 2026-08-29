"""URL configuration: the split API owns the whole tree."""

from django.urls import path

from tth_opencode.api import api

urlpatterns = [
    path("", api.urls),
]
