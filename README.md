# ApiWrapper

Harici bir Web LLM servisini **OpenAI uyumlu** bir REST API'ye dönüştüren, üretime hazır
FastAPI servisi. OpenAI SDK, LangChain, Open WebUI, Cursor, Continue gibi tüm
OpenAI-compatible istemcilerle çalışır.

```
İstemci ──(OpenAI şeması)──▶ ApiWrapper ──(text/plain, tarayıcı taklidi)──▶ Upstream Web LLM
        ◀──(SSE chat.completion.chunk)──          ◀──(AI SDK data-stream)──
```

Mimari ayrıntılar için: **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)**

---

## Özellikler

| Alan | Ayrıntı |
|---|---|
| Uç noktalar | `POST /v1/chat/completions` (stream + non-stream), `POST /v1/completions`, `GET /v1/models`, `GET /v1/models/{id}`, `GET /health`, `GET /metrics`, `/v1/admin/*` |
| Akış | Vercel AI SDK data-stream (`0:`, `f:`, `e:`, `d:`, `3:` …) → OpenAI SSE; UTF-8 sınır güvenli; `[DONE]` garantili |
| Güvenlik | Sabit zamanlı API key doğrulama, log maskeleme, gövde/mesaj/prompt limitleri, SSRF'e kapalı sabit hedef |
| Dayanıklılık | Retry + full jitter, `Retry-After` desteği, devre kesici, eşzamanlılık tavanı, idle timeout, istemci kopması iptali |
| reCAPTCHA v3 | `static` (varsayılan) · `noop` · `browser` (Playwright) · `external` — TTL cache + tek-uçuş kilidi |
| Gözlemlenebilirlik | structlog JSON, `X-Request-ID`, Prometheus metrikleri (TTFT dahil) |
| Uyumluluk | `stop` dizileri (yerel kesme), `stream_options.include_usage`, alias'lı model kayıt defteri, multimodal içerik parçaları |

---

## Hızlı Başlangıç

```bash
git clone <repo> && cd ApiWrapper
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,tokens]"

cp .env.example .env      # TARGET_DOMAIN, API_KEYS, RECAPTCHA_STATIC_TOKEN doldurun
$EDITOR config/models.yaml # upstream_id değerlerini gerçek modelAId'lerle değiştirin

make run                  # http://localhost:8000/docs
```

### Zorunlu yapılandırma

| Değişken | Açıklama |
|---|---|
| `TARGET_DOMAIN` | Hedef servis domaini, **şemasız** (örn. `example-llm.com`) |
| `API_KEYS` | Yerel istemcilerin kullanacağı anahtarlar (virgülle ayrılmış) |
| `RECAPTCHA_STATIC_TOKEN` | `static` sağlayıcı için tarayıcıdan alınan v3 token'ı |
| `config/models.yaml` | `id` ↔ `upstream_id` (`modelAId`) eşlemesi |

> Hedef domain ve model kimlikleri **kodda gömülü değildir**; yalnızca `.env` ve
> `config/models.yaml` üzerinden okunur.

---

## Kullanım

**cURL — streaming**

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-local-dev-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "stream": true,
    "stream_options": {"include_usage": true},
    "messages": [{"role": "user", "content": "Merhaba, kendini tanıt."}]
  }'
```

**OpenAI Python SDK**

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="sk-local-dev-key")

stream = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "FastAPI'yi üç cümlede anlat."}],
    stream=True,
)
for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

**Uzantı:** `conversation_id` alanı ile (ve `SESSION_STATELESS=false` iken) aynı upstream
`chat_id` yeniden kullanılır.

---

## reCAPTCHA sağlayıcıları

```bash
RECAPTCHA_PROVIDER=static     # RECAPTCHA_STATIC_TOKEN (~2 dk ömürlü, hızlı başlangıç)
RECAPTCHA_PROVIDER=noop       # upstream doğrulamıyorsa / testler
RECAPTCHA_PROVIDER=browser    # Playwright: RECAPTCHA_SITE_KEY gerekir
RECAPTCHA_PROVIDER=external   # RECAPTCHA_EXTERNAL_URL + API key (2captcha uyumlu)
```

`browser` için: `pip install ".[browser]" && playwright install chromium`.
Upstream token'ı reddederse (403) cache otomatik geçersizleşir ve istek **bir kez** yeniden denenir.

---

## İşletim

```bash
make test     # 68 test: parser, adapters, çekirdek, uçtan uca API
python scripts/test_client.py  # canlı servise karşı 14 kontrol
make lint     # ruff
make docker   # docker compose up --build
```

- `GET /health` → devre kesici durumu, model sayısı, oturum sayısı
- `GET /metrics` → Prometheus (`apiwrapper_time_to_first_token_seconds` dahil)
- `POST /v1/admin/breaker/reset`, `/v1/admin/recaptcha/invalidate`, `/v1/admin/sessions/clear`
- `GET /v1/admin/config` → hassas olmayan etkin yapılandırma

### Ayar politikaları

`UNSUPPORTED_PARAMS` upstream'in desteklemediği (`temperature`, `max_tokens`, `tools` …)
parametrelerin davranışını belirler:

| Değer | Davranış |
|---|---|
| `ignore` (varsayılan) | Sessizce yok sayılır |
| `hint` | Prompt'a doğal dil ipucu olarak eklenir |
| `error` | 400 ile reddedilir |

---

## Yasal Uyarı

Bu araç yalnızca eğitim ve birlikte çalışabilirlik amaçlıdır. Hedef servisin
kullanım koşullarına (ToS), oran sınırlarına ve yürürlükteki mevzuata uyum
tamamen kullanıcının sorumluluğundadır.

## Teslimat Raporu

Tam teknik teslimat özeti (klasör ağacı, kritik dosya kodları, konfigürasyon,
çalıştırma/test talimatları, edge-case ve risk analizi):
**[`docs/DELIVERY_REPORT.md`](docs/DELIVERY_REPORT.md)**
