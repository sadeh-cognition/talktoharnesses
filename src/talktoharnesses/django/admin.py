"""Django admin integration for trusted JWT issuance."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from django import forms
from django.contrib import admin
from django.contrib.admin.options import csrf_protect_m
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.template.response import TemplateResponse
from django.utils.cache import patch_cache_control

from talktoharnesses.django.auth import AuthenticationFailed, issue_token_sync
from talktoharnesses.django.models import ApiToken

UserModel = get_user_model()


class IssueTokenForm(forms.Form):
    """Select the active Django user that owns the client token."""

    user = forms.ModelChoiceField(queryset=UserModel._default_manager.none(), label="Client user")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        field = cast("forms.ModelChoiceField[Any]", self.fields["user"])
        field.queryset = UserModel._default_manager.filter(is_active=True)


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):  # pyright: ignore[reportMissingTypeArgument]
    """Issue replacement tokens without exposing stored token metadata."""

    issue_template = "admin/talktoharnesses/apitoken/issue_token.html"

    def has_view_permission(self, request: HttpRequest, obj: ApiToken | None = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: ApiToken | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: ApiToken | None = None) -> bool:
        return False

    @csrf_protect_m
    def add_view(
        self,
        request: HttpRequest,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        if not self.has_add_permission(request):
            raise PermissionDenied

        form = IssueTokenForm(request.POST if request.method == "POST" else None)
        token: str | None = None
        expires_at: datetime | None = None
        if request.method == "POST" and form.is_valid():
            try:
                user = form.cleaned_data["user"]
                with transaction.atomic():
                    issued = issue_token_sync(user)
                    self.log_addition(  # pyright: ignore[reportUnknownMemberType]
                        request,
                        ApiToken.objects.get(user=user),
                        f"Issued client JWT for user {user}.",
                    )
            except AuthenticationFailed:
                form.add_error("user", "A token cannot be issued for this user.")
            else:
                token = issued.token
                expires_at = issued.expires_at

        context: dict[str, Any] = dict(self.admin_site.each_context(request))
        context.update(
            {
                "opts": ApiToken._meta,
                "title": "Generate client JWT",
                "form": form,
                "token": token,
                "expires_at": expires_at,
            }
        )
        if extra_context:
            context.update(extra_context)
        response = TemplateResponse(request, self.issue_template, context)
        patch_cache_control(
            response,
            private=True,
            no_cache=True,
            no_store=True,
            must_revalidate=True,
        )
        return response
