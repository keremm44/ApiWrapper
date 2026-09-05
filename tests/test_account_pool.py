"""Hesap havuzu testleri: yükleme, seçim, AIMD, hesap geçişi ve admin ucu."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.account import Account, AccountPool, load_accounts
from app.services.session_manager import SessionManager
from tests.conftest import TEST_API_KEY, UPSTREAM_DOMAIN, ai_stream, make_settings

STREAM_URL = f"https://{UPSTREAM_DOMAIN}/nextjs-api/stream/post-to-evaluation/"

WINDOW = 1200.0
COOLDOWN = 1200.0


def two_account_settings(**overrides):
    base = {
        "target_domain": UPSTREAM_DOMAIN,
        "upstream_cookie": "COOKIE-1",
        "recaptcha_static_token": "RT-1",
        "upstream_account_name": "bir",
        "upstream_cookie_2": "COOKIE-2",
        "recaptcha_static_token_2": "RT-2",
        "upstream_account_2_name": "iki",
        "account_quota_window_seconds": WINDOW,
        "account_cooldown_seconds": COOLDOWN,
    }
    base.update(overrides)
    return make_settings(**base)


@pytest.fixture
def pool_client():
    app = create_app(two_account_settings(retry_max_attempts=1))
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        yield test_client


def chat_body() -> dict:
    return {"model": "test-model", "messages": [{"role": "user", "content": "selam"}]}


def route_by_cookie(router: respx.Router, responses: dict[str, httpx.Response]):
    """Cookie değerine göre yanıt döndürür; beklenmeyen cookie testi patlatır."""

    def handler(request: httpx.Request) -> httpx.Response:
        cookie = request.headers.get("cookie", "")
        assert cookie in responses, f"beklenmeyen cookie: {cookie!r}"
        return responses[cookie]

    return router.post(url__startswith=STREAM_URL).mock(side_effect=handler)


# ----------------------------------------------------------------- yükleme
def test_loads_both_accounts_from_env():
    accounts = load_accounts(two_account_settings())
    assert [(a.slot, a.label) for a in accounts] == [(1, "bir"), (2, "iki")]
    assert accounts[1].cookie == "COOKIE-2"
    assert accounts[1].recaptcha_token == "RT-2"


def test_unconfigured_slots_are_skipped():
    settings = two_account_settings(upstream_cookie_2="", recaptcha_static_token_2="")
    accounts = load_accounts(settings)
    assert [a.slot for a in accounts] == [1]


def test_single_account_setup_yields_one_entry():
    accounts = load_accounts(
        make_settings(target_domain=UPSTREAM_DOMAIN, upstream_cookie="C1")
    )
    assert [a.label for a in accounts] == ["hesap-1"]


def test_effective_settings_does_not_mutate_the_original():
    settings = two_account_settings()
    account = load_accounts(settings)[1]
    derived = account.effective_settings(settings)
    assert derived.upstream_cookie == "COOKIE-2"
    assert derived.recaptcha_static_token == "RT-2"
    assert settings.upstream_cookie == "COOKIE-1"
    assert settings.recaptcha_static_token == "RT-1"


def test_effective_settings_recomputes_urls_from_account_domain():
    settings = two_account_settings()
    account = load_accounts(settings)[0]
    derived = Account(
        slot=9, name="x", cookie="c", target_domain="baska.example.com"
    ).effective_settings(settings)
    assert derived.stream_url("CID").startswith("https://baska.example.com/")
    assert account.slot == 1  # orijinal değişmedi


# ------------------------------------------------------------------ seçim
def test_pick_prefers_the_least_used_account():
    pool = AccountPool(two_account_settings())
    assert pool.pick().label == "bir"
    pool.record_message(1)
    assert pool.pick().label == "iki"


def test_pick_respects_the_budget_as_a_preference():
    pool = AccountPool(two_account_settings(account_msg_budget=2))
    pool.record_message(1)
    pool.record_message(1)
    # 1. hesap bütçeyi doldurdu ama isteği engellemez; sadece sıra değişir.
    assert pool.pick().label == "iki"
    assert pool.effective_budget(1) == 2


def test_single_account_is_never_blocked_by_budget():
    pool = AccountPool(
        make_settings(
            target_domain=UPSTREAM_DOMAIN, upstream_cookie="C1", account_msg_budget=1
        )
    )
    for _ in range(5):
        pool.record_message(1)
    assert pool.pick().slot == 1


# --------------------------------------------------------------- cooldown
def test_cooled_account_is_skipped_and_returns_after_cooldown():
    pool = AccountPool(two_account_settings())
    now = 1_000.0
    pool.record_message(1, now=now)
    pool.report_quota(1, now=now)

    assert pool.cooldown_remaining(1, now=now) == pytest.approx(COOLDOWN)
    assert pool.pick(now=now).label == "iki"
    # Cooldown bitince 1. hesap yeniden adaydır; seçim en az kullanılandan yana
    # olduğu için penceresi boş olan 2. hesap tercih edilmeye devam eder.
    assert pool.cooldown_remaining(1, now=now + COOLDOWN + 1) == 0.0
    pool.record_message(2, now=now + COOLDOWN + 1)
    pool.record_message(2, now=now + COOLDOWN + 1)
    assert pool.pick(now=now + COOLDOWN + 1).label == "bir"


def test_pick_returns_none_when_every_account_is_cooling():
    pool = AccountPool(two_account_settings())
    pool.report_quota(1, now=1_000.0)
    pool.report_quota(2, now=1_000.0)
    assert pool.pick(now=1_000.0) is None
    assert pool.next_cooldown_end(now=1_000.0) == pytest.approx(COOLDOWN)


# ------------------------------------------------------------------- AIMD
def test_quota_hit_lowers_the_learned_limit():
    """N. mesajda kilitlendiysek gerçek sınır en fazla N-1'dir."""
    pool = AccountPool(two_account_settings(account_msg_budget=15))
    for _ in range(5):
        pool.record_message(1)
    pool.report_quota(1)
    assert pool.state(1).learned_limit == 4
    assert pool.effective_budget(1) == 4


def test_budget_grows_only_after_several_clean_windows():
    """Tavan, bütçe kadar mesaj kilit olmadan geçildikten sonra ve en fazla
    pencere başına bir kez artar (her mesajda değil)."""
    pool = AccountPool(
        two_account_settings(account_msg_budget=3, account_budget_growth_streak=2)
    )
    for _ in range(5):
        pool.record_message(1)
    assert pool.state(1).learned_limit == 0  # henüz ilk temiz pencere tamamlanmadı
    assert pool.state(1).clean_windows == 1

    pool.record_message(1)  # 6 = 3 * (1 + 1) -> ikinci temiz pencere
    assert pool.state(1).learned_limit == 4

    pool.record_message(1)  # aynı pencerede ikinci kez artmaz
    assert pool.state(1).learned_limit == 4


def test_budget_does_not_grow_on_every_message():
    """En kötü durumda (bütçe 1) bile tavan her mesajda değil, her 3 mesajda bir artar."""
    pool = AccountPool(
        two_account_settings(account_msg_budget=1, account_budget_growth_streak=3)
    )
    for _ in range(4):
        pool.record_message(1)
    assert pool.effective_budget(1) == 2  # 4 mesaj -> tek artış

    for _ in range(6):
        pool.record_message(1)
    # 10 mesaj sonunda tavan 10 değil, ~4 olmalı (her 3 mesajda +1).
    assert pool.effective_budget(1) <= 4


def test_quota_hit_resets_the_growth_streak():
    pool = AccountPool(
        two_account_settings(account_msg_budget=2, account_budget_growth_streak=3)
    )
    for _ in range(2):
        pool.record_message(1)
    assert pool.state(1).clean_windows == 1
    pool.report_quota(1)
    assert pool.state(1).clean_windows == 0

# ------------------------------------------------------- sohbet hesaba bağlı
@pytest.mark.asyncio
async def test_each_account_gets_its_own_upstream_chat():
    manager = SessionManager(make_settings(session_reuse=True))
    first = await manager.acquire(None, "cli_1", "bir")
    second = await manager.acquire(None, "cli_1", "iki")
    again = await manager.acquire(None, "cli_1", "bir")
    assert first.chat_id != second.chat_id
    assert first.chat_id == again.chat_id


# ------------------------------------------------- uçtan uca: hesap geçişi
@respx.mock
def test_quota_on_first_account_switches_to_the_second(pool_client):
    route = route_by_cookie(
        respx,
        {
            "COOKIE-1": httpx.Response(
                429, content=b'{"error":"upstream limit reached"}'
            ),
            "COOKIE-2": httpx.Response(200, content=ai_stream("ikinci hesap")),
        },
    )
    response = pool_client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "ikinci hesap"
    assert route.call_count == 2

    body = json.loads(route.calls[1].request.content.decode())
    assert body["recaptchaV3Token"] == "RT-2"  # hesabın kendi captcha token'ı


@respx.mock
def test_no_switch_when_all_accounts_are_locked(pool_client):
    route = route_by_cookie(
        respx,
        {
            "COOKIE-1": httpx.Response(429, content=b'{"error":"upstream limit reached"}'),
            "COOKIE-2": httpx.Response(429, content=b'{"error":"upstream limit reached"}'),
        },
    )
    response = pool_client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "upstream_quota_reached"
    assert route.call_count == 2  # iki hesap denendi, fazlası değil

    # Sonraki istek hiç upstream'e gitmez: iki hesap da dinlenmede.
    second = pool_client.post("/v1/chat/completions", json=chat_body())
    assert second.status_code == 429
    assert "temporarily locked" in second.json()["error"]["message"]
    assert route.call_count == 2


@respx.mock
def test_switch_limit_is_honoured(pool_client):
    """account_max_switches=2 ile en fazla 3 hesap denenir (burada 2 hesap var)."""
    route = route_by_cookie(
        respx,
        {
            "COOKIE-1": httpx.Response(429, content=b'{"error":"upstream limit reached"}'),
            "COOKIE-2": httpx.Response(429, content=b'{"error":"upstream limit reached"}'),
        },
    )
    pool_client.post("/v1/chat/completions", json=chat_body())
    assert route.call_count == 2


@respx.mock
def test_each_account_uses_its_own_cookie_and_token(pool_client):
    route = route_by_cookie(
        respx,
        {
            "COOKIE-1": httpx.Response(200, content=ai_stream("bir")),
            "COOKIE-2": httpx.Response(200, content=ai_stream("iki")),
        },
    )
    for _ in range(4):
        assert pool_client.post("/v1/chat/completions", json=chat_body()).status_code == 200

    cookies = [call.request.headers.get("cookie") for call in route.calls]
    tokens = [
        json.loads(call.request.content.decode())["recaptchaV3Token"]
        for call in route.calls
    ]
    assert set(cookies) == {"COOKIE-1", "COOKIE-2"}
    assert set(tokens) == {"RT-1", "RT-2"}
    # Cookie ile token eşleşmeli (A hesabının token'ı B'ye gitmemeli).
    pairs = set(zip(cookies, tokens, strict=True))
    assert pairs == {("COOKIE-1", "RT-1"), ("COOKIE-2", "RT-2")}


@respx.mock
@respx.mock
def test_each_account_gets_a_separate_upstream_chat():
    settings = two_account_settings(session_reuse=True)
    client = TestClient(create_app(settings))
    route = route_by_cookie(
        respx,
        {
            "COOKIE-1": httpx.Response(200, content=ai_stream("bir")),
            "COOKIE-2": httpx.Response(200, content=ai_stream("iki")),
        },
    )
    with client as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        for _ in range(4):
            test_client.post("/v1/chat/completions", json=chat_body())

    per_cookie: dict[str, set[str]] = {}
    for call in route.calls:
        cookie = call.request.headers.get("cookie")
        per_cookie.setdefault(cookie, set()).add(call.request.url.path)
    assert len(per_cookie) == 2
    assert all(len(paths) == 1 for paths in per_cookie.values())
    assert settings.session_reuse is True
    # İki hesap farklı upstream sohbetlerine yazmış olmalı.
    all_paths = {p for paths in per_cookie.values() for p in paths}
    assert len(all_paths) == 2


# ------------------------------------------------------------------ admin
def test_admin_accounts_lists_pool_without_secrets(pool_client):
    body = pool_client.get("/v1/admin/accounts").json()
    assert body["configured_accounts"] == 2
    names = {a["name"] for a in body["accounts"]}
    assert names == {"bir", "iki"}
    text = json.dumps(body)
    assert "COOKIE-1" not in text and "RT-1" not in text
    for account in body["accounts"]:
        assert account["has_cookie"] is True
        assert account["messages_in_window"] == 0


def test_admin_account_cooldown_reset(pool_client):
    pool = pool_client.app.state.accounts
    pool.report_quota(1)
    assert pool.cooldown_remaining(1) > 0

    response = pool_client.post("/v1/admin/accounts/1/reset")
    assert response.status_code == 200
    assert pool.cooldown_remaining(1) == 0.0

    missing = pool_client.post("/v1/admin/accounts/99/reset")
    assert missing.status_code == 400


def test_metrics_include_account_switches(pool_client):
    pool = pool_client.app.state.accounts
    pool.report_quota(1)
    text = pool_client.get("/metrics").text
    assert "apiwrapper_account_switches_total" in text
    assert "apiwrapper_account_cooldown" in text
