"""Django admin JWT issuance integration."""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client

from talktoharnesses.django.auth import AuthenticationFailed, authenticate_bearer_sync
from talktoharnesses.django.models import ApiToken


@pytest.fixture
def admin_user(db: Any) -> Any:
    User: Any = get_user_model()
    return User.objects.create_superuser(
        username="admin", email="admin@example.com", password="secret"
    )


@pytest.fixture
def admin_client(admin_user: Any) -> Client:
    client = Client()
    client.force_login(admin_user)
    return client


@pytest.mark.django_db
def test_admin_issues_one_time_token_for_active_client_user(
    admin_client: Client, admin_user: Any
) -> None:
    User: Any = get_user_model()
    user = User.objects.create_user(username="api-client", password=None)

    response = admin_client.post(
        "/admin/talktoharnesses/apitoken/add/",
        {"user": str(user.pk)},
    )

    assert response.status_code == 200
    token = response.context["token"]
    assert token
    assert response.context["expires_at"] is not None
    assert "no-store" in response.headers["Cache-Control"]
    assert authenticate_bearer_sync(f"Bearer {token}").pk == user.pk
    body = response.content.decode()
    assert token in body
    assert ApiToken.objects.get(user=user).jti_digest not in body

    log_entry = LogEntry.objects.get(
        user_id=admin_user.pk,
        object_id=str(user.pk),
        action_flag=ADDITION,
    )
    assert log_entry.change_message == f"Issued client JWT for user {user}."

    detail = admin_client.get(f"/admin/talktoharnesses/apitoken/{user.pk}/change/")
    assert detail.status_code == 403

    follow_up = admin_client.get("/admin/talktoharnesses/apitoken/add/")
    assert follow_up.status_code == 200
    assert follow_up.context["token"] is None
    assert token not in follow_up.content.decode()


@pytest.mark.django_db
def test_admin_resubmission_reissues_and_invalidates_previous_token(
    admin_client: Client,
) -> None:
    User: Any = get_user_model()
    user = User.objects.create_user(username="api-client", password=None)
    url = "/admin/talktoharnesses/apitoken/add/"

    first = admin_client.post(url, {"user": str(user.pk)}).context["token"]
    second = admin_client.post(url, {"user": str(user.pk)}).context["token"]

    assert first != second
    with pytest.raises(AuthenticationFailed):
        authenticate_bearer_sync(f"Bearer {first}")
    assert authenticate_bearer_sync(f"Bearer {second}").pk == user.pk
    assert ApiToken.objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_admin_requires_staff_user(db: Any) -> None:
    url = "/admin/talktoharnesses/apitoken/add/"
    anonymous_response = Client().get(url)

    User: Any = get_user_model()
    nonstaff = User.objects.create_user(username="nonstaff", password="secret")
    nonstaff_client = Client()
    nonstaff_client.force_login(nonstaff)
    nonstaff_response = nonstaff_client.get(url)

    assert anonymous_response.status_code == 302
    assert anonymous_response.headers["Location"].startswith("/admin/login/")
    assert nonstaff_response.status_code == 302
    assert nonstaff_response.headers["Location"].startswith("/admin/login/")


@pytest.mark.django_db
def test_admin_requires_add_permission(db: Any) -> None:
    User: Any = get_user_model()
    staff = User.objects.create_user(username="staff", password="secret", is_staff=True)
    client = Client()
    client.force_login(staff)

    response = client.get("/admin/talktoharnesses/apitoken/add/")

    assert response.status_code == 403

    staff.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="talktoharnesses",
            codename="add_apitoken",
        )
    )

    assert client.get("/admin/talktoharnesses/apitoken/add/").status_code == 200


@pytest.mark.django_db
def test_admin_rejects_post_without_csrf(admin_user: Any) -> None:
    User: Any = get_user_model()
    user = User.objects.create_user(username="api-client", password=None)
    client = Client(enforce_csrf_checks=True)
    client.force_login(admin_user)

    response = client.post(
        "/admin/talktoharnesses/apitoken/add/",
        {"user": str(user.pk)},
    )

    assert response.status_code == 403
    assert not ApiToken.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_admin_changelist_is_denied(admin_client: Client) -> None:
    response = admin_client.get("/admin/talktoharnesses/apitoken/")

    assert response.status_code == 403


@pytest.mark.django_db
def test_admin_form_excludes_inactive_users(admin_client: Client) -> None:
    User: Any = get_user_model()
    active = User.objects.create_user(username="active", password=None)
    inactive = User.objects.create_user(username="inactive", password=None, is_active=False)

    response = admin_client.get("/admin/talktoharnesses/apitoken/add/")

    users = set(response.context["form"].fields["user"].queryset)
    assert active in users
    assert inactive not in users
