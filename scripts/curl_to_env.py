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
    "UPSTREAM_STREAM_PATH",
    "UPSTREAM_RECAPTCHA_FIELD",
    "UPSTREAM_ACCEPT_LANGUAGE",
    "UPSTREAM_USER_AGENT",
    "UPSTREAM_COOKIE",
    "RECAPTCHA_PROVIDER",
    "RECAPTCHA_STATIC_TOKEN",
)

#: Oturum çerezi olma ihtimali olan adlar (uyarı üretmek için).
_SESSION_HINT = "auth"

#: Telemetri/analitik uç noktaları: yanlış isteğin cURL'ü kopyalanmış demektir.
TELEMETRY_HOSTS: tuple[str, ...] = (
    "datadoghq",
    "sentry.io",
    "google-analytics",
    "googletagmanager",
    "posthog",
    "segment.io",
    "amplitude",
    "mixpanel",
    "doubleclick",
    "facebook",
    "clarity.ms",
    "bugsnag",
    "newrelic",
    "intercom",
    "hotjar",
)

#: Varsayılan akış yolu (app/core/config.py ile aynı).
DEFAULT_STREAM_PATH = "/nextjs-api/stream/post-to-evaluation/{chat_id}"

#: URL yolunda sohbet kimliği olarak geçebilecek UUID biçimi.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

#: Farklı upstream sürümlerindeki model/sohbet endpoint izleri.
_EXPECTED_PATH_HINTS: tuple[str, ...] = (
    "post-to-evaluation",
    "stream",
    "completion",
    "chat",
    "generate",
    "message",
    "inference",
    "evaluation",
    "create-evaluation",
)


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


def extract_model_id(body: dict[str, Any]) -> str | None:
    """Farklı cURL sürümlerindeki model kimliği alanlarını bulur."""
    for field in ("modelAId", "modelId", "model_id", "model"):
        value = body.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def build_settings(parsed: dict[str, Any]) -> tuple[dict[str, str], list[str], str | None]:
    """cURL çıktısından `.env` çiftlerini, uyarıları ve model kimliğini üretir."""
    headers: dict[str, str] = parsed["headers"]
    body = load_body(parsed["body"])
    warnings: list[str] = []
    env: dict[str, str] = {}

    # --- alan adı
    url = parsed["url"]
    split = urlsplit(url) if url else None
    host = split.netloc if split else ""
    if host:
        env["TARGET_DOMAIN"] = host
    else:
        warnings.append("URL bulunamadı; TARGET_DOMAIN elle yazılmalı.")

    # Yanlış isteğin cURL'ü kopyalandığında sessizce telemetri sunucusuna
    # yönlenmemek için erken uyar.
    lowered_host = host.lower()
    if any(marker in lowered_host for marker in TELEMETRY_HOSTS):
        warnings.append(
            f"KRİTİK: '{host}' bir telemetri/analitik adresi; sohbet ucu değil. "
            "Network sekmesinde POST olan, gövdesinde model kimliği bulunan sohbet "
            "isteğine sağ tıklayıp cURL'ü yeniden kopyalayın."
        )
    elif split and not any(hint in split.path.lower() for hint in _EXPECTED_PATH_HINTS):
        warnings.append(
            f"URL yolu bilinen sohbet izlerinden hiçbirini içermiyor: {split.path or '/'}. "
            "İstek yine de model gövdesi içeriyorsa devam edilebilir; aksi halde yanlış "
            "Network isteği seçilmiş olabilir."
        )

    # --- akış yolu (sabit varsayım yerine cURL'deki gerçek yol)
    if split and split.path:
        template = _stream_path_template(split.path, body)
        if template:
            env["UPSTREAM_STREAM_PATH"] = template
            if template != DEFAULT_STREAM_PATH:
                warnings.append(
                    f"Akış yolu varsayılandan farklı, .env'e yazıldı: {template}"
                )
        else:
            warnings.append(
                f"URL yolundaki sohbet kimliği tanınamadı ({split.path}); "
                "UPSTREAM_STREAM_PATH elle ayarlanmalı."
            )

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
    model_id = extract_model_id(body)
    if not model_id:
        warnings.append(
            "Gövdede model kimliği bulunamadı (modelAId/modelId/model_id/model); "
            "config/models.yaml içindeki upstream_id değeri otomatik güncellenemez."
        )

    if not body and parsed["body"]:
        warnings.append("Gövde JSON olarak ayrıştırılamadı; cURL eksik kopyalanmış olabilir.")

    if not body:
        warnings.append(
            "KRİTİK: cURL gövdesi yok; bu bir model sohbet isteği değil veya cURL eksik kopyalanmış."
        )
    elif not model_id:
        warnings.append(
            "KRİTİK: Gövdede 'modelAId' yok; bu cURL model akış isteği olarak doğrulanamadı."
        )

    return env, warnings, model_id


def _stream_path_template(path: str, body: dict[str, Any]) -> str | None:
    """URL yolundaki sohbet kimliğini ``{chat_id}`` yer tutucusuna çevirir.

    Upstream yolu sürümlenebildiği için (ör. ``/nextjs-api/stream/...`` yerine
    ``/api/stream/...``) sabit varsayım HTTP 404 üretir. Yol cURL'den alınır ve
    yalnızca kimlik kısmı parametreleştirilir.
    """
    chat_id = body.get("id")
    # Yalnızca tam bir yol segmentiyle eşleşmeli: kısa bir "id" değeri yolun
    # ortasındaki harflere denk gelip yolu bozabilir.
    if isinstance(chat_id, str) and chat_id:
        segments = path.split("/")
        if chat_id in segments:
            return "/".join("{chat_id}" if seg == chat_id else seg for seg in segments)
    match = _UUID_RE.search(path)
    if match:
        return path[: match.start()] + "{chat_id}" + path[match.end() :]

    # Bazı sürümlerde chat_id URL'ye değil JSON gövdesine gider; bu durumda
    # endpoint yolunu olduğu gibi korumak gerekir.
    if isinstance(chat_id, str) and chat_id and any(
        hint in path.lower() for hint in _EXPECTED_PATH_HINTS
    ):
        return path
    return None


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


def update_models_yaml(path: Path, upstream_id: str, model_name: str | None = None) -> bool:
    """Model eşleşmesini güvenli biçimde ekler veya günceller.

    Birden fazla model varken ilk kaydı sessizce değiştirmek yanlış model
    eşleşmesine yol açar. Bu durumda yeni model, varsayılan yerel adı olarak
    upstream kimliğiyle eklenir; ``model_name`` verilirse o ad kullanılır.
    """
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if model_name:
        id_match = re.search(
            rf"(^\s*- id:\s*){re.escape(model_name)}\s*$", text, re.MULTILINE
        )
        if id_match:
            start = id_match.end()
            next_model = re.search(r"^\s*- id:\s*", text[start:], re.MULTILINE)
            end = start + next_model.start() if next_model else len(text)
            block = text[start:end]
            new_block, count = re.subn(
                r"(^\s*upstream_id:\s*).*$",
                lambda m: f"{m.group(1)}{upstream_id}",
                block,
                count=1,
                flags=re.MULTILINE,
            )
            if count:
                path.write_text(text[:start] + new_block + text[end:], encoding="utf-8")
                return True

    if re.search(rf"^\s*upstream_id:\s*{re.escape(upstream_id)}\s*$", text, re.MULTILINE):
        return False

    name = model_name or upstream_id
    suffix = "" if text.endswith("\n") else "\n"
    addition = (
        f"{suffix}\n  - id: {name}\n"
        f"    upstream_id: {upstream_id}\n"
        "    owned_by: upstream\n"
        "    description: cURL ile eklenen model\n"
    )
    path.write_text(text + addition, encoding="utf-8")
    return True


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
        "--model-name",
        help=(
            "cURL'deki modeli bağlamak için yerel model adı; verilmezse yeni model "
            "upstream_id adıyla eklenir"
        ),
    )
    parser.add_argument(
        "--show-secrets", action="store_true", help="Değerleri maskelemeden yazdır"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Kritik uyarılara rağmen yazmayı sürdür",
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
        print(f"  (models.yaml) yerel ad={args.model_name or model_id}")

    if warnings:
        print("\n=== Uyarılar ===")
        for item in warnings:
            print(f"  ! {item}")

    if not args.write:
        print("\nYazmak için --write ekleyin.")
        return 0

    critical = [w for w in warnings if w.startswith("KRİTİK")]
    if critical and not args.force:
        print(
            "\nYazma iptal edildi: yanlış isteğin cURL'ü kopyalanmış görünüyor.\n"
            "Doğru cURL'ü alıp tekrar deneyin ya da yine de yazmak için --force ekleyin.",
            file=sys.stderr,
        )
        return 3

    env_path = Path(args.env_file)
    env_path.write_text(merge_env(env_path, env), encoding="utf-8")
    print(f"\n{env_path} güncellendi ({len(env)} anahtar).")

    if model_id:
        models_path = Path(args.models_file)
        if update_models_yaml(models_path, model_id, args.model_name):
            print(
                f"{models_path} güncellendi "
                f"({args.model_name or model_id} -> {model_id})."
            )
        else:
            print(f"{models_path} zaten bu upstream_id eşleşmesini içeriyor: {model_id}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
