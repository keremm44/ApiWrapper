"""`scripts/curl_to_env.py` için testler.

Bu araç, kullanıcının cURL komutunu elle kopyalarken yaptığı hataları
(çerezin ortadan kesilmesi, değerin alt satıra taşması, mükerrer anahtarlar)
ortadan kaldırmak için yazıldı; testler hem doğru ayrıştırmayı hem de bozuk
girdilerde üretilen uyarıları doğrular.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from scripts.compare_curl import parse_curl
from scripts.curl_to_env import (
    build_settings,
    extract_captcha,
    merge_env,
    update_models_yaml,
)


def _jwt() -> str:
    def seg(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    head = seg({"alg": "HS256", "typ": "JWT"})
    payload = seg({"sub": "user-1", "exp": int(time.time()) + 3600})
    return f"{head}.{payload}.sig"


def _session_cookie(token: str) -> str:
    session = {"access_token": token, "refresh_token": "r" * 40}
    blob = base64.b64encode(json.dumps(session).encode()).decode()
    return (
        f"__cf_bm=abc; _ga=GA1.1.9; lmarena-auth-prod-v1.0=base64-{blob}; "
        "cf_clearance=CFxyz; sidebar_state=true"
    )


def _curl(cookie: str, body: dict) -> str:
    return (
        "curl 'https://example-llm.ai/nextjs-api/stream/post-to-evaluation/c1' \\\n"
        "  -H 'accept: */*' \\\n"
        "  -H 'accept-language: tr-TR,tr;q=0.9' \\\n"
        "  -H 'user-agent: Mozilla/5.0 Chrome/128.0.0.0' \\\n"
        f"  -H 'cookie: {cookie}' \\\n"
        f"  --data-raw '{json.dumps(body)}'"
    )


@pytest.fixture()
def sample() -> dict:
    token = _jwt()
    body = {
        "id": "c1",
        "modelAId": "019f19f2-41f1-7c6d-9891-48d02fd9952c",
        "userMessage": {"content": "Merhaba dünya"},
        "modality": "chat",
        "recaptchaV2Token": "0cAFcWeA6_" + "T" * 500,
    }
    return {"token": token, "body": body, "cookie": _session_cookie(token)}


def test_extracts_every_setting_from_curl(sample):
    env, warnings, model_id = build_settings(parse_curl(_curl(sample["cookie"], sample["body"])))

    assert env["TARGET_DOMAIN"] == "example-llm.ai"
    assert env["UPSTREAM_COOKIE"] == sample["cookie"]
    assert env["UPSTREAM_RECAPTCHA_FIELD"] == "recaptchaV2Token"
    assert env["RECAPTCHA_STATIC_TOKEN"] == sample["body"]["recaptchaV2Token"]
    assert env["RECAPTCHA_PROVIDER"] == "static"
    assert env["UPSTREAM_ACCEPT_LANGUAGE"] == "tr-TR,tr;q=0.9"
    assert model_id == "019f19f2-41f1-7c6d-9891-48d02fd9952c"
    assert warnings == []


def test_extracted_cookie_is_single_line(sample):
    """`.env` satır tabanlıdır; değer içinde satır sonu olursa ayar bozulur."""
    env, _, _ = build_settings(parse_curl(_curl(sample["cookie"], sample["body"])))
    assert "\n" not in env["UPSTREAM_COOKIE"]
    assert "\n" not in env["RECAPTCHA_STATIC_TOKEN"]


def test_warns_when_cookie_is_a_broken_fragment():
    """Kullanıcının çerezi ortadan kopyalaması en sık görülen hata."""
    fragment = "mdvb2dsZXVzZXJjb250ZW50LmNvbS9hL0FDZzhvY0pGMlFZ"
    curl = f"curl 'https://x.ai/api' -H 'cookie: {fragment}' --data-raw '{{}}'"
    _, warnings, _ = build_settings(parse_curl(curl))
    assert any("ad=değer" in w for w in warnings)


def test_warns_when_session_cookie_missing():
    """Yalnızca analitik çerezleri varsa kullanıcı giriş yapmamıştır."""
    curl = "curl 'https://x.ai/api' -H 'cookie: _ga=GA1.1.1; sidebar_state=true' --data-raw '{}'"
    _, warnings, _ = build_settings(parse_curl(curl))
    assert any("auth" in w for w in warnings)


@pytest.mark.parametrize(
    "field", ["recaptchaV2Token", "recaptchaV3Token", "turnstileToken"]
)
def test_detects_captcha_field_name(field: str):
    found, value = extract_captcha({field: "tok-" + "x" * 40})
    assert found == field
    assert value.startswith("tok-")


def test_merge_env_updates_in_place_and_keeps_others(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "API_KEYS=sk-keep\nTARGET_DOMAIN=localhost\n# yorum\nLOG_LEVEL=info\n",
        encoding="utf-8",
    )
    result = merge_env(path, {"TARGET_DOMAIN": "example.com", "UPSTREAM_COOKIE": "a=1"})

    assert "API_KEYS=sk-keep" in result
    assert "LOG_LEVEL=info" in result
    assert "# yorum" in result
    assert "TARGET_DOMAIN=example.com" in result
    assert "TARGET_DOMAIN=localhost" not in result
    assert result.count("TARGET_DOMAIN=") == 1


def test_merge_env_removes_duplicate_keys(tmp_path):
    """Elle düzenlenen dosyalarda aynı anahtar birden çok kez bulunabilir."""
    path = tmp_path / ".env"
    path.write_text("UPSTREAM_COOKIE=old1\nAPI_KEYS=k\nUPSTREAM_COOKIE=old2\n", encoding="utf-8")
    result = merge_env(path, {"UPSTREAM_COOKIE": "new"})

    assert result.count("UPSTREAM_COOKIE=") == 1
    assert "UPSTREAM_COOKIE=new" in result
    assert "old1" not in result and "old2" not in result


def test_merge_env_creates_file_when_absent(tmp_path):
    path = tmp_path / ".env"
    result = merge_env(path, {"TARGET_DOMAIN": "example.com"})
    assert "TARGET_DOMAIN=example.com" in result


def test_update_models_yaml_replaces_first_upstream_id(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(
        "models:\n  - id: a\n    upstream_id: PLACEHOLDER\n  - id: b\n    upstream_id: OTHER\n",
        encoding="utf-8",
    )
    assert update_models_yaml(path, "real-id-1") is True
    text = path.read_text(encoding="utf-8")
    assert "upstream_id: real-id-1" in text
    assert "upstream_id: OTHER" in text


def test_update_models_yaml_missing_file_is_reported(tmp_path):
    assert update_models_yaml(tmp_path / "yok.yaml", "x") is False


def test_windows_cmd_curl_format_is_parsed():
    """Windows 'Copy as cURL (cmd)' çıktısı ^ ile kaçış kullanır."""
    curl = 'curl ^"https://x.ai/api^" ^\n  -H ^"cookie: a-auth-token=xyz123456789^"'
    env, _, _ = build_settings(parse_curl(curl))
    assert env["TARGET_DOMAIN"] == "x.ai"
    assert "a-auth-token=xyz123456789" in env["UPSTREAM_COOKIE"]


# ------------------------- yanlış isteğin cURL'ü kopyalandığında
def test_telemetry_host_is_flagged_as_critical():
    """Datadog/analitik isteğinin cURL'ü kopyalanırsa TARGET_DOMAIN bozulur.

    Gerçek bir raporda base_url 'browser-intake-us3-datadoghq.com' olmuştu;
    istekler LLM yerine log toplayıcıya gidiyordu.
    """
    curl = (
        "curl 'https://browser-intake-us3-datadoghq.com/api/v2/rum' "
        "-H 'cookie: a-auth-token=xyz1234567890abc' --data-raw '{}'"
    )
    _, warnings, _ = build_settings(parse_curl(curl))
    assert any(w.startswith("KRİTİK") and "telemetri" in w for w in warnings)


@pytest.mark.parametrize(
    "host",
    ["browser-intake-us3-datadoghq.com", "sentry.io", "www.google-analytics.com"],
)
def test_known_telemetry_hosts_are_rejected(host: str):
    curl = f"curl 'https://{host}/collect' -H 'cookie: a=b' --data-raw '{{}}'"
    _, warnings, _ = build_settings(parse_curl(curl))
    assert any(w.startswith("KRİTİK") for w in warnings)


def test_unexpected_path_is_warned_but_not_critical():
    """Doğru alan adı ama yanlış uç: uyar, ama yazmayı engelleme."""
    curl = "curl 'https://example-llm.ai/api/session' -H 'cookie: a-auth=b' --data-raw '{}'"
    _, warnings, _ = build_settings(parse_curl(curl))
    assert any("post-to-evaluation" in w for w in warnings)
    assert not any(w.startswith("KRİTİK") for w in warnings)


def test_correct_endpoint_produces_no_path_or_host_warning(sample):
    curl = _curl(sample["cookie"], sample["body"])
    _, warnings, _ = build_settings(parse_curl(curl))
    assert not any("post-to-evaluation" in w for w in warnings)
    assert not any(w.startswith("KRİTİK") for w in warnings)
