"""Auth: every /v1/* route needs the bearer token except /v1/health and docs."""

from __future__ import annotations

import pytest

from app.auth import token_is_valid
from tests.conftest import TEST_TOKEN


def test_token_comparison():
    assert token_is_valid(TEST_TOKEN) is True
    assert token_is_valid(TEST_TOKEN + "x") is False
    assert token_is_valid(TEST_TOKEN[:-1]) is False  # a prefix must not pass
    assert token_is_valid("") is False
    assert token_is_valid(None) is False


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/messages"),
        ("POST", "/v1/messages/bulk"),
        ("GET", "/v1/messages"),
        ("GET", "/v1/messages/abc"),
        ("DELETE", "/v1/messages/abc"),
        ("GET", "/v1/inbox"),
        ("GET", "/v1/conversations"),
        ("GET", "/v1/contacts/a@b.com"),
        ("POST", "/v1/consents"),
        ("GET", "/v1/suppressions"),
        ("POST", "/v1/suppressions"),
        ("DELETE", "/v1/suppressions/a@b.com"),
        ("GET", "/v1/stats"),
    ],
)
def test_routes_require_a_token(client, method, path):
    resp = client.request(method, path, headers={"Authorization": ""}, json={})
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "unauthorized"
    # The refusal must not leak the expected token or any config.
    assert TEST_TOKEN not in resp.text


def test_wrong_scheme_is_rejected(client):
    resp = client.get("/v1/messages", headers={"Authorization": f"Basic {TEST_TOKEN}"})
    assert resp.status_code == 401


@pytest.mark.parametrize("path", ["/v1/health", "/health", "/docs", "/openapi.json"])
def test_open_routes_need_no_token(client, path):
    resp = client.get(path, headers={"Authorization": ""})
    assert resp.status_code == 200


def test_health_body_carries_no_secret(client):
    resp = client.get("/v1/health", headers={"Authorization": ""})
    assert resp.status_code == 200
    assert TEST_TOKEN not in resp.text
    assert "unit-test-key" not in resp.text
