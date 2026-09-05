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
| Upstream auth | Cookie'den `access_token` ayıklama (JSON/base64/parçalı) → `Authorization: Bearer`; `/v1/admin/auth` teşhisi |
| Güvenlik | Sabit zamanlı API key doğrulama, log maskeleme, gövde/mesaj/prompt limitleri, SSRF'e kapalı sabit hedef |
| Dayanıklılık | Retry + full jitter, `Retry-After` desteği, devre kesici, eşzamanlılık tavanı, idle timeout, istemci kopması iptali |
| Kota tespiti | Hedefin "limit reached" tarzı mesajı işaret tabanlı yakalanır (HTTP gövdesi · akış hata olayı · opsiyonel düz metin taraması) → `429 upstream_quota_reached` + `Retry-After`, **retry edilmez** |
| Hesap havuzu | Çoklu çerez/token; kayan pencere sayacı, kilitte otomatik hesap geçişi, AIMD ile öğrenilen bütçe, hesap başına captcha token'ı |
| Sohbet yönetimi | `SESSION_REUSE=true` ile istemci başına tek upstream sohbeti; `SESSION_ROTATE_AFTER_MESSAGES`/`_SECONDS` eşiğinde yeni sohbete geçilir (Continue gibi `conversation_id` gönderemeyen istemciler için) |
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
> `config/models.yaml` üzerinden okunur. `UPSTREAM_STREAM_PATH` varsayılanı eski bir
> uçtur — tarayıcı cURL'ünden alınmazsa upstream **HTTP 404** döner.

---

## Upstream Kimlik Doğrulama (`Authorization: Bearer`)

Hedef servis yalnızca `Cookie` göndermeyi yeterli bulmaz; `Authorization: Bearer <access_token>`
başlığını da zorunlu kılar (aksi halde **401**). ApiWrapper token'ı `UPSTREAM_COOKIE`
içinden **otomatik ayıklar** ve her upstream isteğine ekler.

```bash
# Tarayıcıdan kopyalanan ham Cookie başlığı yeterlidir:
UPSTREAM_COOKIE=theme=dark; sb-abc-auth-token=base64-eyJhY2Nlc3NfdG9rZW4iOi...; other=1
```

Desteklenen çerez biçimleri (`app/upstream/auth.py`):

| Biçim | Örnek |
|---|---|
| Düz değer | `access_token=eyJhbGciOi...` |
| URL-encoded | `access_token=eyJ...%3D%3D` |
| JSON nesnesi | `sb-auth-token={"access_token":"eyJ...","refresh_token":"..."}` |
| JSON dizisi | `sb-auth-token=["eyJ...","refresh",null,null]` |
| base64-JSON | `sb-auth-token=base64-eyJhY2Nlc3NfdG9rZW4iOi...` |
| Parçalı çerez | `sb-auth-token.0=base64-eyJ...` + `sb-auth-token.1=...` |

### İlgili ayarlar

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `UPSTREAM_AUTH_FROM_COOKIE` | `true` | Cookie'den otomatik ayıklamayı aç/kapa |
| `UPSTREAM_ACCESS_TOKEN` | — | Token'ı doğrudan ver (en yüksek öncelik) |
| `UPSTREAM_TOKEN_COOKIE_NAMES` | — | Token'ı taşıyan çerez adları (virgülle); boşsa sezgisel arama |
| `UPSTREAM_AUTH_SCHEME` | `Bearer` | Boş bırakılırsa token ham gönderilir |
| `UPSTREAM_WARN_ON_EXPIRED_TOKEN` | `true` | JWT `exp` dolmuşsa logda uyar |

> `UPSTREAM_EXTRA_HEADERS` en son uygulanır; `authorization=...` yazarak
> otomatik üretilen başlığı bilinçli olarak ezebilirsiniz.

### Teşhis

Token'ın doğru ayıklanıp ayıklanmadığını **token'ı ifşa etmeden** kontrol edin:

```bash
curl -s -H "Authorization: Bearer sk-local-dev-key" \
  http://localhost:8000/v1/admin/auth | jq
```

```json
{
  "authorization_header_will_be_sent": true,
  "source": "cookie",
  "cookie_names_present": ["other", "sb-abc-auth-token", "theme"],
  "token": {"found": true, "masked": "eyJ***yz", "is_jwt": true,
            "expires_in_seconds": 3567, "expired": false}
}
```

Upstream yine de 401 dönerse yanıt artık genel bir hata değil, eyleme dönük olur:
`upstream_unauthorized` + "Refresh UPSTREAM_COOKIE...". Süresi dolmuş bir JWT
gönderilmeden önce log uyarısı düşer (`access_token_expired`, kaç saniye önce dolduğu ile).


---

## Kendi cURL'ünüze Göre Ayarlama (önemli)

Hedef servisler ayrıntılarda farklılaşır. Tarayıcıdan aldığınız isteği bir dosyaya
kaydedip **karşılaştırma aracını** çalıştırın; hangi başlık/gövde alanının uyuşmadığını
size satır satır söyler (Windows `^`, bash `\` ve PowerShell `` ` `` kaçışlarını anlar):

```bash
# DevTools > Network > sağ tık > Copy as cURL  -> request.txt
python scripts/compare_curl.py request.txt
```

```text
=== Gövde alanları (--data-raw) ===
  EŞLEŞTİ id
  EKSİK   recaptchaV2Token  <-- gövdemizde yok!
  FAZLA   recaptchaV3Token  <-- tarayıcı göndermiyor

  !! UPSTREAM_RECAPTCHA_FIELD=recaptchaV3Token ancak tarayıcı 'recaptchaV2Token' gönderiyor.
     .env içine yazın: UPSTREAM_RECAPTCHA_FIELD=recaptchaV2Token
```

### Sık karşılaşılan üç fark

| Belirti | Ayar |
|---|---|
| Gövdede `recaptchaV2Token` var, biz `recaptchaV3Token` yolluyoruz | `UPSTREAM_RECAPTCHA_FIELD=recaptchaV2Token` |
| `accept-language` farklı (örn. `tr-TR,...`) | `UPSTREAM_ACCEPT_LANGUAGE=tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7` |
| cURL'de `authorization` başlığı **yok** | `UPSTREAM_AUTH_FROM_COOKIE=false` (varsayılan) |

> **Cloudflare:** İsteğinizde `cf_clearance` çerezi varsa onu da `UPSTREAM_COOKIE`
> içine **olduğu gibi** ekleyin. `cf_clearance` yalnızca onu üreten IP + User-Agent
> ikilisiyle geçerlidir; bu yüzden `UPSTREAM_USER_AGENT` tarayıcınızla birebir aynı
> olmalı ve sunucu tarayıcıyla aynı çıkıştan (IP) istek atmalıdır — aksi halde
> Cloudflare yeniden doğrulama ister. `cf_clearance` asla `access_token` sanılmaz.


### Windows / PowerShell

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\windows\run.ps1                       # servisi baslat (pencere 1)
.\scripts\windows\chat.ps1 "Merhaba"            # mesaj gonder  (pencere 2)
.\scripts\windows\chat.ps1 -Interactive         # surekli sohbet
.\scripts\windows\chat.ps1 -Diagnose            # saglik + kimlik teshisi
```

Ayrıntı: [`scripts/windows/README.md`](scripts/windows/README.md)


---

## Kullanım

**cURL — streaming**

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-local-dev-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6-max-preview",
    "stream": true,
    "stream_options": {"include_usage": true},
    "messages": [{"role": "user", "content": "Merhaba, kendini tanıt."}]
  }'
```

**OpenAI Python SDK**

`base_url` `/v1` ile veya onsuz verilebilir; her iki biçim de aynı uçlara gider.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="sk-local-dev-key")

stream = client.chat.completions.create(
    model="qwen3.6-max-preview",
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

## 404 / yanlış uç

İki farklı 404 vardır; karıştırmayın:

| Kaynak | Belirti | Çözüm |
|---|---|---|
| Yerel API | `Unknown endpoint … /chat/completions` | `base_url` olarak `http://localhost:8000` veya `http://localhost:8000/v1` kullanın (ikisi de çalışır). Bilinmeyen model `model_not_found` döner. |
| Upstream | `upstream_not_found` / logda `HTTP 404` | `UPSTREAM_STREAM_PATH` tarayıcıdaki gerçek yolla uyuşmuyor. `python scripts/curl_to_env.py request.txt --write` çalıştırın. Yeni uçlar genelde `/nextjs-api/stream/create-evaluation` kullanır ve `chat_id`'yi URL'ye koymaz. |

`python scripts/compare_curl.py request.txt` artık **URL yolunu** da karşılaştırır; buradaki `FARKLI` satırı 404'ün doğrudan nedenidir.

## Birden çok hesabı cURL ile kaydetme

`curl_to_env.py` her çalıştırmada **bir hesap yuvasına** yazar. cURL'leri sırayla atın:
1. cURL soneksiz anahtarlara (`UPSTREAM_COOKIE`, `RECAPTCHA_STATIC_TOKEN`, …),
2. cURL `_2` soneksli anahtarlara (`UPSTREAM_COOKIE_2`, `RECAPTCHA_STATIC_TOKEN_2`, …).
Hiçbir hesap diğerini ezmez; `models.yaml` yalnızca 1. hesapta güncellenir (model
kimlikleri hesap başına değildir).

```bash
python scripts/curl_to_env.py hesap1.txt --write     # -> 1. yuva
python scripts/curl_to_env.py hesap2.txt --write     # -> 2. yuva
python scripts/curl_to_env.py - --list-accounts      # kayıtlı hesapları göster
python scripts/curl_to_env.py hesap1.txt --write --account 1   # 1. yuvayı tazele
python scripts/curl_to_env.py - --reset-accounts     # tüm yuvaları temizle
```

Araç artık cURL'deki `authorization` başlığını da yazıyor (`UPSTREAM_ACCESS_TOKEN` +
`UPSTREAM_AUTH_SCHEME`, `UPSTREAM_AUTH_FROM_COOKIE=false`). Başlık yoksa istek yalnızca
Cookie ile gider ve bunu belirten bir uyarı üretilir; ne başlık ne oturum çerezi varsa
"istek anonim gider" uyarısı çıkar. `referer`'dan da `UPSTREAM_REFERER_PATH` şablonu üretilir.

Kaydedilen hesaplar hesap havuzu tarafından okunur ve istekler aralarında dağıtılır
(aşağıda). Yuvalar `Settings` içinde açıkça tanımlıdır; 4 hesap desteklenir
(`upstream_account_name`, `_2`, `_3`, `_4`).

## Hesap havuzu ve kota yönetimi

Upstream bir hesabı kayan pencerede çok mesaj atınca kilitliyor ve bunu bir metinle
bildiriyor. Havuz bunu şöyle yönetir:

```bash
ACCOUNT_MSG_BUDGET=15               # hesap başına yumuşak bütçe (pencere içi mesaj)
ACCOUNT_QUOTA_WINDOW_SECONDS=1200   # kota penceresi (20 dk)
ACCOUNT_COOLDOWN_SECONDS=1200       # kilit algılanan hesabın dinlenme süresi
ACCOUNT_MAX_SWITCHES=2              # tek istekte en fazla kaç hesap değiştirilsin
ACCOUNT_BUDGET_GROWTH_STREAK=3      # bütçenin +1 artması için gereken temiz pencere
```

- **Seçim:** penceresi en boş hesap tercih edilir. Bütçe bir *engel* değil *tercih
  sırası*dır — tek hesaplı kurulumda istek asla reddedilmez.
- **Kilit:** `upstream limit reached` algılanınca o hesap `ACCOUNT_COOLDOWN_SECONDS`
  boyunca dinlendirilir ve istek **aynı istekte** başka hesapla yeniden denenir
  (yalnızca istemciye henüz tek bir parça gönderilmediyse; aksi halde iki yanıt
  birbirine karışırdı).
- **AIMD:** bir hesap pencere içinde N. mesajda kilitlendiyse gerçek sınır en fazla
  N-1'dir → öğrenilen tavan `N-1` olur (çarpan azaltma). Bütçe kadar mesaj kilit
  olmadan geçilince tavan +1 artar (toplama artırma), ama her mesajda değil —
  `ACCOUNT_BUDGET_GROWTH_STREAK` kadar temiz pencere gerekir.
- **Captcha:** her hesap **kendi** `RECAPTCHA_STATIC_TOKEN` değerini kullanır; token
  tarayıcı oturumuna bağlı olduğu için hesaplar arası paylaşılmaz. Token ~2 dk
  ömürlüdür, yani her hesabın cURL'ünü düzenli tazelemeniz gerekir.
- **Sohbet:** upstream sohbeti hesaba bağlıdır; hesap değişince başka hesabın
  sohbetine yazılmaz.

Durum ve öğrenilen sınırlar:

```bash
curl -H "Authorization: Bearer $API_KEY" http://localhost:8000/v1/admin/accounts
curl -X POST -H "Authorization: Bearer $API_KEY" \
     http://localhost:8000/v1/admin/accounts/2/reset   # dinlenmeyi elle sıfırla
```

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
