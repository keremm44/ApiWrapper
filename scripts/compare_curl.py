"""Tarayıcıdan alınan gerçek cURL ile ApiWrapper'ın ürettiği isteği karşılaştırır.

Kullanım:
    # cURL'ü bir dosyaya kaydedin (Windows ^ kaçışları veya bash \\ kaçışları olabilir)
    python scripts/compare_curl.py request.txt

Çıktı: eksik/fazla/farklı başlıklar ve gövde alanları — böylece upstream'e
gönderdiğimiz isteğin tarayıcınınkiyle örtüşüp örtüşmediğini görürsünüz.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import Settings  # noqa: E402
from app.upstream.headers import build_stream_headers  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[0m",
)

#: Karşılaştırmada yok sayılacak başlıklar (bağlantıya/istemciye özgü).
IGNORED_HEADERS = {"content-length", "host", "connection", "accept-encoding"}


def normalize_curl(raw: str) -> str:
    """Windows (`^`) ve bash (`\\`) satır devamı kaçışlarını temizler."""
    text = raw.strip()
    # Windows cmd: ^" -> "  ve satır sonundaki ^
    text = re.sub(r"\^\s*\n", " ", text)
    text = text.replace('^"', '"').replace("^%", "%").replace("^^", "^")
    # bash: satır sonu \
    text = re.sub(r"\\\s*\n", " ", text)
    # PowerShell: satır sonu `
    text = re.sub(r"`\s*\n", " ", text)
    return " ".join(text.split())


def _split_top_level(text: str) -> list[tuple[str, str]]:
    """`-H "..."`, `-b "..."`, `--data-raw "..."` çiftlerini sırayla çıkarır.

    `shlex` bitişik tırnaklı parçaları birleştirip JSON gövdesini bozduğu için
    tırnak eşleştirmesi elle yapılır.
    """
    pairs: list[tuple[str, str]] = []
    pattern = re.compile(
        r"(--data-raw|--data-binary|--data|--header|--url|--cookie|-H|-d|-b)\s+"
    )
    pos = 0
    while True:
        match = pattern.search(text, pos)
        if not match:
            break
        flag = match.group(1)
        start = match.end()
        if start < len(text) and text[start] in "\"'":
            quote = text[start]
            end = start + 1
            buf: list[str] = []
            while end < len(text):
                if text[end] == "\\" and end + 1 < len(text):
                    buf.append(text[end + 1])
                    end += 2
                    continue
                if text[end] == quote:
                    # Bitişik aynı tırnak: JSON içi tırnak olabilir, ileriye bak.
                    nxt = end + 1
                    while nxt < len(text) and text[nxt] == " ":
                        nxt += 1
                    if nxt >= len(text) or text[nxt] in "-\n" or text[nxt:nxt + 2] == "--":
                        break
                    buf.append(text[end])
                    end += 1
                    continue
                buf.append(text[end])
                end += 1
            value = "".join(buf)
            pos = end + 1
        else:
            end = text.find(" ", start)
            end = len(text) if end == -1 else end
            value = text[start:end]
            pos = end
        pairs.append((flag, value))
    return pairs


def parse_curl(raw: str) -> dict[str, Any]:
    """cURL komutundan url, başlıklar, çerezler ve gövdeyi çıkarır."""
    text = normalize_curl(raw)

    url = ""
    headers: dict[str, str] = {}
    cookie = ""
    body = ""

    for flag, value in _split_top_level(text):
        if flag in ("-H", "--header"):
            name, _, header_value = value.partition(":")
            headers[name.strip().lower()] = header_value.strip()
        elif flag in ("-b", "--cookie"):
            cookie = value
        elif flag in ("--data-raw", "--data", "-d", "--data-binary"):
            body = value
        elif flag == "--url":
            url = value

    if not url:
        found = re.search(r"https?://[^\s\"']+", text)
        if found:
            url = found.group(0)
    if cookie:
        headers["cookie"] = cookie
    return {"url": url, "headers": headers, "body": body}


def load_body(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    reference = parse_curl(Path(sys.argv[1]).read_text(encoding="utf-8"))
    ref_headers = reference["headers"]
    ref_body = load_body(reference["body"])

    settings = Settings()
    chat_id = ref_body.get("id") or "CHAT_ID"
    ours = build_stream_headers(settings, chat_id)
    ours = {k.lower(): v for k, v in ours.items()}

    print(f"\n=== Başlık karşılaştırması {DIM}(referans = tarayıcı cURL'ü){RESET} ===\n")

    names = sorted((set(ref_headers) | set(ours)) - IGNORED_HEADERS)
    problems = 0
    for name in names:
        theirs = ref_headers.get(name)
        mine = ours.get(name)
        if theirs is None:
            print(f"{YELLOW}  FAZLA   {RESET}{name}: {DIM}{str(mine)[:70]}{RESET}")
            problems += 1
        elif mine is None:
            print(f"{RED}  EKSİK   {RESET}{name}: {DIM}{str(theirs)[:70]}{RESET}")
            problems += 1
        elif theirs.strip() == str(mine).strip():
            print(f"{GREEN}  EŞLEŞTİ {RESET}{name}")
        else:
            print(f"{YELLOW}  FARKLI  {RESET}{name}")
            print(f"{DIM}      tarayıcı: {theirs[:90]}{RESET}")
            print(f"{DIM}      bizim   : {str(mine)[:90]}{RESET}")
            problems += 1

    if ref_body:
        print(f"\n=== Gövde alanları {DIM}(--data-raw){RESET} ===\n")
        expected_fields = set(ref_body)
        our_fields = {
            "id",
            "modelAId",
            "userMessageId",
            "modelAMessageId",
            "userMessage",
            "modality",
            settings.upstream_recaptcha_field,
        }
        for field in sorted(expected_fields | our_fields):
            in_ref, in_ours = field in expected_fields, field in our_fields
            if in_ref and in_ours:
                print(f"{GREEN}  EŞLEŞTİ {RESET}{field}")
            elif in_ref:
                print(f"{RED}  EKSİK   {RESET}{field}  <-- gövdemizde yok!")
                problems += 1
            else:
                print(f"{YELLOW}  FAZLA   {RESET}{field}  <-- tarayıcı göndermiyor")
                problems += 1

        captcha_fields = [f for f in expected_fields if "recaptcha" in f.lower()]
        if captcha_fields and captcha_fields[0] != settings.upstream_recaptcha_field:
            print(
                f"\n{RED}  !! UPSTREAM_RECAPTCHA_FIELD={settings.upstream_recaptcha_field} "
                f"ancak tarayıcı '{captcha_fields[0]}' gönderiyor.{RESET}"
            )
            print(f"     .env içine yazın: UPSTREAM_RECAPTCHA_FIELD={captcha_fields[0]}")

    print(
        f"\n=== {GREEN if problems == 0 else YELLOW}{problems} fark bulundu{RESET} ===\n"
        f"{DIM}Not: 'FAZLA' işaretli sec-* başlıkları genelde zararsızdır; "
        f"'EKSİK' olanlar ve gövde farkları önemlidir.{RESET}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
