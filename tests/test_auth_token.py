"""Cookie'den access_token ayıklama ve Authorization başlığı testleri."""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import quote

import pytest

from app.upstream.auth import (
    decode_jwt_claims,
    extract_access_token,
    is_token_expired,
    looks_like_jwt,
    merge_chunked_cookies,
    parse_cookie_header,
    resolve_bearer_token,
    token_seconds_remaining,
)
from app.upstream.headers import build_authorization, build_stream_headers
from tests.conftest import make_settings


def make_jwt(payload: dict | None = None) -> str:
    """İmzası önemsiz, biçimsel olarak geçerli bir JWT üretir."""

    def seg(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = seg({"alg": "HS256", "typ": "JWT"})
    body = seg(payload or {"sub": "user-1", "exp": int(time.time()) + 3600})
    return f"{header}.{body}.c2lnbmF0dXJlLXBsYWNlaG9sZGVy"


TOKEN = make_jwt()


# ------------------------------------------------------------------ parsing
def test_parse_cookie_header_basic():
    cookies = parse_cookie_header("a=1; b=2;  c = 3 ")
    assert cookies == {"a": "1", "b": "2", "c": "3"}


def test_parse_cookie_handles_equals_in_value():
    cookies = parse_cookie_header("t=abc==; other=x")
    assert cookies["t"] == "abc=="


def test_parse_cookie_ignores_junk():
    assert parse_cookie_header("") == {}
    assert parse_cookie_header("novalue; =orphan; ok=1") == {"ok": "1"}


def test_first_occurrence_wins():
    assert parse_cookie_header("k=first; k=second")["k"] == "first"


def test_merge_chunked_cookies():
    merged = merge_chunked_cookies({"sb.1": "world", "sb.0": "hello", "x": "y"})
    assert merged["sb"] == "helloworld"
    assert merged["x"] == "y"


def test_merge_does_not_override_existing_base():
    merged = merge_chunked_cookies({"sb": "whole", "sb.0": "part"})
    assert merged["sb"] == "whole"


# --------------------------------------------------------------- extraction
def test_extract_plain_access_token():
    assert extract_access_token(f"access_token={TOKEN}; other=1") == TOKEN


def test_extract_url_encoded_token():
    assert extract_access_token(f"access_token={quote(TOKEN)}") == TOKEN


def test_extract_from_json_cookie():
    value = json.dumps({"access_token": TOKEN, "refresh_token": "r-123"})
    assert extract_access_token(f"sb-auth-token={quote(value)}") == TOKEN


def test_extract_from_json_array_cookie():
    value = json.dumps([TOKEN, "refresh-token", None, None])
    assert extract_access_token(f"sb-auth-token={quote(value)}") == TOKEN


def test_extract_from_base64_json_cookie():
    payload = json.dumps({"access_token": TOKEN, "token_type": "bearer"}).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    assert extract_access_token(f"sb-xyz-auth-token=base64-{encoded}") == TOKEN


def test_extract_from_chunked_base64_cookie():
    payload = json.dumps({"access_token": TOKEN}).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    mid = len(encoded) // 2
    header = (
        f"sb-auth-token.0=base64-{encoded[:mid]}; sb-auth-token.1={encoded[mid:]}"
    )
    assert extract_access_token(header) == TOKEN


def test_extract_from_nested_session_object():
    value = json.dumps({"currentSession": {"access_token": TOKEN}, "expiresAt": 1})
    assert extract_access_token(f"supabase.auth.token={quote(value)}") == TOKEN


def test_prefers_named_cookie_when_specified():
    other = make_jwt({"sub": "other"})
    header = f"decoy_token={other}; my_session={TOKEN}"
    assert extract_access_token(header, cookie_names=["my_session"]) == TOKEN


def test_partial_name_match_for_configured_name():
    header = f"app-my_session-v2={TOKEN}"
    assert extract_access_token(header, cookie_names=["my_session"]) == TOKEN


def test_falls_back_to_any_jwt_in_cookies():
    assert extract_access_token(f"weird_name_xyz={TOKEN}") == TOKEN


def test_returns_none_without_token():
    assert extract_access_token("theme=dark; lang=tr") is None
    assert extract_access_token("") is None


def test_short_values_are_not_tokens():
    assert extract_access_token("access_token=abc") is None


def test_jwt_preferred_over_opaque_value():
    header = f"auth_token=short-opaque-but-long-enough; session_token={TOKEN}"
    assert extract_access_token(header) == TOKEN


def test_opaque_token_accepted_when_no_jwt():
    opaque = "a" * 40
    assert extract_access_token(f"access_token={opaque}") == opaque


# ---------------------------------------------------------------------- jwt
def test_looks_like_jwt():
    assert looks_like_jwt(TOKEN)
    assert not looks_like_jwt("not.a")
    assert not looks_like_jwt("")


def test_decode_claims_and_expiry():
    exp = int(time.time()) + 120
    token = make_jwt({"sub": "u", "exp": exp})
    assert decode_jwt_claims(token)["sub"] == "u"
    remaining = token_seconds_remaining(token)
    assert remaining is not None and 100 < remaining <= 120
    assert not is_token_expired(token)


def test_expired_token_detected():
    token = make_jwt({"exp": int(time.time()) - 60})
    assert is_token_expired(token)
    assert not is_token_expired(token, leeway=-120)


def test_token_without_exp_is_not_expired():
    token = make_jwt({"sub": "no-exp"})
    assert token_seconds_remaining(token) is None
    assert not is_token_expired(token)


def test_malformed_jwt_claims_are_empty():
    assert decode_jwt_claims("aaa.!!!notbase64!!!.ccc") == {}


# ------------------------------------------------------------------ resolve
def test_explicit_token_wins_over_cookie():
    other = make_jwt({"sub": "cookie"})
    resolved = resolve_bearer_token(
        explicit_token=TOKEN, cookie_header=f"access_token={other}"
    )
    assert resolved == TOKEN


def test_bearer_prefix_stripped_from_explicit_token():
    assert resolve_bearer_token(explicit_token=f"Bearer {TOKEN}") == TOKEN


# ------------------------------------------------------------------ headers
def test_authorization_header_built_from_cookie():
    settings = make_settings(upstream_cookie=f"access_token={TOKEN}; theme=dark",
                             upstream_auth_from_cookie=True)
    headers = build_stream_headers(settings, "chat-1")
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["cookie"] == f"access_token={TOKEN}; theme=dark"


def test_authorization_absent_when_no_token():
    settings = make_settings(upstream_cookie="theme=dark", upstream_auth_from_cookie=True)
    assert "authorization" not in build_stream_headers(settings, "chat-1")


def test_authorization_disabled_by_flag():
    settings = make_settings(
        upstream_cookie=f"access_token={TOKEN}", upstream_auth_from_cookie=False
    )
    assert build_authorization(settings) is None


def test_explicit_access_token_setting_used():
    settings = make_settings(upstream_access_token=TOKEN)
    assert build_stream_headers(settings, "c")["authorization"] == f"Bearer {TOKEN}"


def test_custom_auth_scheme():
    settings = make_settings(upstream_access_token=TOKEN, upstream_auth_scheme="Token")
    assert build_authorization(settings) == f"Token {TOKEN}"


def test_empty_scheme_sends_raw_token():
    settings = make_settings(upstream_access_token=TOKEN, upstream_auth_scheme="")
    assert build_authorization(settings) == TOKEN


def test_extra_headers_can_override_authorization():
    settings = make_settings(
        upstream_access_token=TOKEN,
        upstream_extra_headers="authorization=Bearer manual-override",
    )
    headers = build_stream_headers(settings, "c")
    assert headers["authorization"] == "Bearer manual-override"


def test_configured_cookie_name_setting_is_honoured():
    decoy = make_jwt({"sub": "decoy"})
    settings = make_settings(
        upstream_cookie=f"tracking_jwt={decoy}; my_app_session={TOKEN}",
        upstream_token_cookie_names=["my_app_session"],
        upstream_auth_from_cookie=True,
    )
    assert build_authorization(settings) == f"Bearer {TOKEN}"


def test_expired_token_still_sent_with_warning(caplog):
    expired = make_jwt({"exp": int(time.time()) - 30})
    settings = make_settings(upstream_access_token=expired)
    assert build_authorization(settings) == f"Bearer {expired}"


@pytest.mark.parametrize(
    "cookie",
    [
        "access_token={t}",
        "sb-auth-token=%7B%22access_token%22%3A%22{t}%22%7D",
        "session=abc; access-token={t}; other=1",
    ],
)
def test_common_cookie_shapes(cookie):
    header = cookie.format(t=TOKEN)
    assert extract_access_token(header) == TOKEN


# --------------------------------------- gerçek hedef cURL'ünden gelen senaryolar
ARENA_COOKIE = (
    "arena-auth-prod-v1.0=base64-{blob}; cf_clearance=abcdefghijklmnop1234567890"
)


def _arena_cookie(token: str) -> str:
    blob = (
        base64.urlsafe_b64encode(json.dumps({"access_token": token}).encode())
        .decode()
        .rstrip("=")
    )
    return ARENA_COOKIE.format(blob=blob)


def test_arena_auth_cookie_is_recognised():
    assert extract_access_token(_arena_cookie(TOKEN)) == TOKEN


def test_cf_clearance_is_never_mistaken_for_token():
    """Cloudflare çerezi uzun ve opak; token sanılmamalı."""
    header = "cf_clearance=" + "Z" * 80 + "; theme=dark"
    assert extract_access_token(header) is None


def test_cf_clearance_ignored_even_with_token_in_name():
    header = "cf_clearance=" + "Q" * 60 + "; __cf_bm=" + "R" * 60
    assert extract_access_token(header) is None


def test_arena_cookie_preferred_over_cloudflare():
    header = _arena_cookie(TOKEN)
    assert "cf_clearance" in header
    assert extract_access_token(header) == TOKEN
