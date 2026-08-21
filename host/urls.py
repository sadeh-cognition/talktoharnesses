from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("talktoharnesses.django.api.urls")),
]
urlpatterns += staticfiles_urlpatterns()
