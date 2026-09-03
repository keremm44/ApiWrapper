"""Tarayıcı taklidi HTTP başlıklarının üretimi.

cURL analizindeki başlıklar birebir korunur; Chrome'un gönderdiği
`sec-*` başlıkları da eklenerek istek gerçekçi hale getirilir.
"""

from __future__ import annotations

import re

from app.core.config import Settings

_CHROME_VERSION_RE = re.compile(r"Chrome/(\d+)")


def chrome_major_version(user_agent: str) -> str:
    match = _CHROME_VERSION_RE.search(user_agent)
    return match.group(1) if match else "152"


def platform_from_ua(user_agent: str) -> str:
    ua = user_agent.lower()
    if "windows" in ua:
        return '"Windows"'
    if "macintosh" in ua or "mac os x" in ua:
        return '"macOS"'
    if "android" in ua:
        return '"Android"'
    if "linux" in ua:
        return '"Linux"'
    return '"Unknown"'


def sec_ch_ua(user_agent: str) -> str:
    version = chrome_major_version(user_agent)
    return (
        f'"Chromium";v="{version}", "Google Chrome";v="{version}", '
        '"Not?A_Brand";v="24"'
    )


def build_stream_headers(settings: Settings, chat_id: str) -> dict[str, str]:
    """Stream uç noktası için tam başlık kümesi."""
    ua = settings.upstream_user_agent
    headers: dict[str, str] = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "text/plain;charset=UTF-8",
        "origin": settings.origin,
        "referer": settings.referer_url(chat_id),
        "user-agent": ua,
        "sec-ch-ua": sec_ch_ua(ua),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform_from_ua(ua),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "priority": "u=1, i",
    }
    if settings.upstream_cookie:
        headers["cookie"] = settings.upstream_cookie
    headers.update(settings.parsed_extra_headers())
    return headers


def build_page_headers(settings: Settings) -> dict[str, str]:
    """Oturum ısıtma (HTML sayfa) istekleri için başlıklar."""
    ua = settings.upstream_user_agent
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": ua,
        "sec-ch-ua": sec_ch_ua(ua),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform_from_ua(ua),
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "upgrade-insecure-requests": "1",
    }
    if settings.upstream_cookie:
        headers["cookie"] = settings.upstream_cookie
    headers.update(settings.parsed_extra_headers())
    return headers
