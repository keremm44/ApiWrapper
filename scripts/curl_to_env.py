#!/usr/bin/env python3
"""Tarayıcıdan kopyalanan cURL komutunu `.env` ve `models.yaml` ayarlarına çevirir.

Kullanım:
    python scripts/curl_to_env.py curl.txt
    python scripts/curl_to_env.py curl.txt --write
    type curl.txt | python scripts/curl_to_env.py -

Elle kopyala-yapıştır sırasında en sık yapılan hatalar (çerezin ortadan kesilmesi,
değerin alt satıra taşması, tırnakların yarım kalması) bu araçla tamamen ortadan
kalkar: cURL ne veriyorsa birebir o yazılır.

Çıkarılanlar:
  * ``TARGET_DOMAIN``        — URL'nin alan adı (şemasız)
  * ``UPSTREAM_COOKIE``      — ``cookie`` başlığının tamamı
  * ``UPSTREAM_ACCEPT_LANGUAGE``
  * ``UPSTREAM_USER_AGENT``
  * ``UPSTREAM_RECAPTCHA_FIELD`` — gövdedeki captcha alanının gerçek adı
  * ``RECAPTCHA_STATIC_TOKEN``   — gövdedeki captcha değeri
  * ``models.yaml`` için ``upstream_id`` (gövdedeki ``modelAId``)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.compare_curl import load_body, parse_curl  # noqa: E402

#: Gövdede captcha token'ını taşıyabilecek alan adları.
CAPTCHA_FIELDS: tuple[str, ...] = (
    "recaptchaV2Token",
    "recaptchaV3Token",
    "recaptchaToken",
    "captchaToken",
    "turnstileToken",
)

#: `.env` içine yazılacak anahtarların sırası.
ENV_ORDER: tuple[str, ...] = (
    "TARGET_DOMAIN",
    "UPSTREAM_RECAPTCHA_FIELD",
    "UPSTREAM_ACCEPT_LANGUAGE",
    "UPSTREAM_USER_AGENT",
    "UPSTREAM_COOKIE",
    "RECAPTCHA_PROVIDER",
    "RECAPTCHA_STATIC_TOKEN",
)

#: Oturum çerezi olma ihtimali olan adlar (uyarı üretmek için).
_SESSION_HINT = "auth"


def _mask(value: str, keep: int = 6) -> str:
    """Uzun gizli değerleri günlüğe basmadan önce kısaltır."""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]} ({len(value)} karakter)"


def extract_captcha(body: dict[str, Any]) -> tuple[str | None, str | None]:
    """Gövdeden captcha alan adını ve değerini bulur."""
    for field in CAPTCHA_FIELDS:
        value = body.get(field)
        if isinstance(value, str) and value.strip():
            return field, value.strip()
    # Bilinmeyen bir ad kullanılmışsa sezgisel olarak ara.
    for key, value in body.items():
        if "captcha" in key.lower() and isinstance(value, str) and value.strip():
            return key, value.strip()
    return None, None


def build_settings(parsed: dict[str, Any]) -> tuple[dict[str, str], list[str], str | None]:
    """cURL çıktısından `.env` çiftlerini, uyarıları ve model kimliğini üretir."""
    headers: dict[str, str] = parsed["headers"]
    body = load_body(parsed["body"])
    warnings: list[str] = []
    env: dict[str, str] = {}

    # --- alan adı
    url = parsed["url"]
    host = urlsplit(url).netloc if url else ""
    if host:
        env["TARGET_DOMAIN"] = host
    else:
        warnings.append("URL bulunamadı; TARGET_DOMAIN elle yazılmalı.")

    # --- başlıklar
    cookie = headers.get("cookie", "").strip()
    if cookie:
        env["UPSTREAM_COOKIE"] = cookie
        names = [p.split("=", 1)[0].strip() for p in cookie.split(";") if "=" in p]
        if not names:
            warnings.append(
                "cookie başlığında 'ad=değer' çifti yok. Muhtemelen değerin yalnızca "
                "bir parçası kopyalanmış; cURL'ü yeniden alın."
            )
        elif not any(_SESSION_HINT in n.lower() for n in names):
            warnings.append(
                f"Çerezler arasında oturum çerezi ('*auth*') görünmüyor: {names}. "
                "Siteye giriş yapmış durumdayken cURL almanız gerekir."
            )
    else:
        warnings.append("cookie başlığı yok; giriş yapmış bir istekten cURL alın.")

    if lang := headers.get("accept-language", "").strip():
        env["UPSTREAM_ACCEPT_LANGUAGE"] = lang
    if agent := headers.get("user-agent", "").strip():
        env["UPSTREAM_USER_AGENT"] = agent

    # --- captcha
    field, token = extract_captcha(body)
    if field and token:
        env["UPSTREAM_RECAPTCHA_FIELD"] = field
        env["RECAPTCHA_PROVIDER"] = "static"
        env["RECAPTCHA_STATIC_TOKEN"] = token
    else:
        warnings.append(
            "Gövdede captcha alanı bulunamadı. Hedef captcha istemiyorsa "
            "RECAPTCHA_PROVIDER=noop kullanın."
        )

    # --- model kimliği
    model_id = body.get("modelAId")
    if not isinstance(model_id, str) or not model_id.strip():
        model_id = None
        warnings.append(
            "Gövdede 'modelAId' yok; config/models.yaml içindeki upstream_id "
            "değerini elle güncellemeniz gerekir."
        )

    if not body and parsed["body"]:
        warnings.append("Gövde JSON olarak ayrıştırılamadı; cURL eksik kopyalanmış olabilir.")

    return env, warnings, model_id


def merge_env(path: Path, updates: dict[str, str]) -> str:
    """Var olan `.env` içeriğini koruyarak anahtarları günceller.

    Aynı anahtardan birden çok satır varsa ilki güncellenir, kalan kopyalar
    silinir; böylece mükerrer tanımların sessizce birbirini ezmesi önlenir.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    result: list[str] = []
    seen: set[str] = set()

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            result.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            result.append(f"{key}={remaining.pop(key)}")
            seen.add(key)
        elif key in seen:
            continue  # mükerrer satırı at
        else:
            result.append(line)

    if remaining:
        if result and result[-1].strip():
            result.append("")
        result.append("# scripts/curl_to_env.py tarafından eklendi")
        for key in ENV_ORDER:
            if key in remaining:
                result.append(f"{key}={remaining.pop(key)}")
        for key, value in remaining.items():
            result.append(f"{key}={value}")

    return "\n".join(result) + "\n"


def update_models_yaml(path: Path, upstream_id: str) -> bool:
    """`models.yaml` içindeki ilk `upstream_id` değerini günceller."""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r"(^\s*upstream_id:\s*).*$",
        lambda m: f"{m.group(1)}{upstream_id}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count and new_text != text:
        path.write_text(new_text, encoding="utf-8")
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="cURL komutunu .env ayarlarına çevirir.",
    )
    parser.add_argument("curl_file", help="cURL komutunu içeren dosya ('-' = stdin)")
    parser.add_argument(
        "--write", action="store_true", help=".env ve models.yaml dosyalarını güncelle"
    )
    parser.add_argument("--env-file", default=".env", help="Hedef .env yolu")
    parser.add_argument(
        "--models-file", default="config/models.yaml", help="Hedef models.yaml yolu"
    )
    parser.add_argument(
        "--show-secrets", action="store_true", help="Değerleri maskelemeden yazdır"
    )
    args = parser.parse_args()

    raw = sys.stdin.read() if args.curl_file == "-" else Path(args.curl_file).read_text(
        encoding="utf-8", errors="replace"
    )
    if not raw.strip():
        print("HATA: cURL girdisi boş.", file=sys.stderr)
        return 2

    parsed = parse_curl(raw)
    env, warnings, model_id = build_settings(parsed)

    if not env:
        print("HATA: cURL'den hiçbir ayar çıkarılamadı.", file=sys.stderr)
        return 2

    print("=== Çıkarılan ayarlar ===")
    for key in ENV_ORDER:
        if key not in env:
            continue
        value = env[key]
        secret = key in {"UPSTREAM_COOKIE", "RECAPTCHA_STATIC_TOKEN"}
        shown = value if args.show_secrets or not secret else _mask(value)
        print(f"  {key}={shown}")
    if model_id:
        print(f"  (models.yaml) upstream_id={model_id}")

    if warnings:
        print("\n=== Uyarılar ===")
        for item in warnings:
            print(f"  ! {item}")

    if not args.write:
        print("\nYazmak için --write ekleyin.")
        return 0

    env_path = Path(args.env_file)
    env_path.write_text(merge_env(env_path, env), encoding="utf-8")
    print(f"\n{env_path} güncellendi ({len(env)} anahtar).")

    if model_id:
        models_path = Path(args.models_file)
        if update_models_yaml(models_path, model_id):
            print(f"{models_path} güncellendi (upstream_id={model_id}).")
        else:
            print(
                f"UYARI: {models_path} güncellenemedi; upstream_id={model_id} "
                "değerini elle yazın."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
