# Windows / PowerShell Kullanımı

## Sıfırdan kurulum: cURL yapıştır, gerisi otomatik

`.env` dosyasını elle doldurmayın. Çerez ve reCAPTCHA token'ı binlerce karakterdir;
Notepad'de yapıştırıldığında alt satıra taşar, `.env` satır tabanlı olduğu için de
yalnızca ilk satır okunur ve ayar sessizce bozulur. Bunun yerine:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\windows\setup-from-curl.ps1
```

Betik cURL'ü yapıştırmanızı ister. Tarayıcıda:

1. Hedef siteye giriş yapın, `F12` → **Network** sekmesi.
2. Sohbete bir mesaj gönderin, listede `post-to-evaluation` isteğini bulun.
3. Sağ tık → **Copy** → **Copy as cURL (bash)**.
4. PowerShell penceresine yapıştırın, **boş bir satırda Enter**'a basın.

Panodan doğrudan okumak için:

```powershell
.\scripts\windows\setup-from-curl.ps1 -FromClipboard
```

Otomatik doldurulanlar:

| Ayar | cURL'deki kaynağı |
| --- | --- |
| `TARGET_DOMAIN` | URL'nin alan adı |
| `UPSTREAM_COOKIE` | `-H 'cookie: ...'` başlığının tamamı |
| `UPSTREAM_ACCEPT_LANGUAGE` | `-H 'accept-language: ...'` |
| `UPSTREAM_USER_AGENT` | `-H 'user-agent: ...'` |
| `UPSTREAM_RECAPTCHA_FIELD` | gövdedeki captcha alanının **adı** |
| `RECAPTCHA_STATIC_TOKEN` | gövdedeki captcha **değeri** |
| `config\models.yaml` → `upstream_id` | gövdedeki `modelAId` |

Mevcut ayarlarınız korunur, aynı anahtardan birden fazla varsa tekilleştirilir.
Yazmadan önce görmek için `-DryRun`, değerleri maskesiz görmek için `-ShowSecrets`.

Sonra:

```powershell
.\scripts\windows\run.ps1                    # pencere 1
.\scripts\windows\chat.ps1 -Diagnose         # pencere 2: doğrulama
.\scripts\windows\chat.ps1 "Merhaba"
```

`-Diagnose` çıktısında `cookie_count` sıfırdan büyük ve `token.found: true`
olmalıdır. reCAPTCHA token'ları kısa ömürlüdür (~2 dakika); `recaptcha_rejected`
hatasında taze bir cURL alıp `setup-from-curl.ps1`'i yeniden çalıştırın.

---

Üç betik: **`setup-from-curl.ps1`** `.env` dosyasını cURL'den doldurur,
**`run.ps1`** servisi başlatır, **`chat.ps1`** mesaj gönderir.

> İlk kez çalıştırıyorsanız PowerShell betik çalıştırmaya izin vermelisiniz
> (yalnız bu oturum için, kalıcı değişiklik yapmaz):
>
> ```powershell
> Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
> ```

---

## 1. Servisi başlat

**PowerShell penceresi #1** (bu pencere açık kalmalı):

```powershell
cd C:\path\to\ApiWrapper
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\windows\run.ps1
```

Betik sanal ortamı kurar, bağımlılıkları yükler, `.env` yoksa oluşturup Notepad'de açar.

Seçenekler:

```powershell
.\scripts\windows\run.ps1 -Port 8080          # farklı port
.\scripts\windows\run.ps1 -Reload             # kod değişince otomatik yeniden başlat
.\scripts\windows\run.ps1 -SkipInstall        # kurulumu atla (hızlı başlangıç)
```

Hazır olduğunda: <http://127.0.0.1:8000/docs>

### `.env` içinde doldurulması gerekenler

Sizin yakaladığınız cURL'e göre:

```ini
TARGET_DOMAIN=<hedef domain, https:// olmadan>
UPSTREAM_RECAPTCHA_FIELD=recaptchaV2Token
UPSTREAM_ACCEPT_LANGUAGE=tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7
UPSTREAM_COOKIE=arena-auth-prod-v1.0=...; cf_clearance=...
RECAPTCHA_PROVIDER=static
RECAPTCHA_STATIC_TOKEN=<tarayıcıdan aldığınız token>
API_KEYS=sk-local-dev-key
```

Ayrıca `config\models.yaml` içindeki `upstream_id` değerlerini gerçek `modelAId`
değerleriyle değiştirin.

---

## 2. Mesaj gönder

**PowerShell penceresi #2** (servis çalışırken):

```powershell
cd C:\path\to\ApiWrapper
.\scripts\windows\chat.ps1 "Merhaba, kendini tanıt"
```

Yanıt canlı (token token) akar.

### Diğer kullanımlar

```powershell
# Sürekli sohbet (geçmişi hatırlar)
.\scripts\windows\chat.ps1 -Interactive

# Streaming kapalı, tek parça yanıt
.\scripts\windows\chat.ps1 "Kısa cevap ver" -NoStream

# Model ve sistem mesajı
.\scripts\windows\chat.ps1 "Python nedir?" -Model gpt-4o -System "Kısa ve teknik yanıtla"

# Farklı adres/anahtar
.\scripts\windows\chat.ps1 "test" -BaseUrl http://127.0.0.1:8080 -ApiKey sk-baska
```

### Sorun teşhisi

```powershell
.\scripts\windows\chat.ps1 -Diagnose
```

`/health`, `/v1/admin/auth` (token ayıklandı mı, süresi doldu mu) ve
`/v1/admin/config` çıktısını birlikte gösterir.

---

## Tek satırlık ham `curl` alternatifi

PowerShell'de `curl` aslında `Invoke-WebRequest` takma adıdır ve streaming'i
düzgün göstermez. Gerçek curl için **`curl.exe`** yazın:

```powershell
curl.exe -N http://127.0.0.1:8000/v1/chat/completions `
  -H "Authorization: Bearer sk-local-dev-key" `
  -H "Content-Type: application/json" `
  --data-raw '{\"model\":\"gpt-4o-mini\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Merhaba\"}]}'
```

---

## Sık karşılaşılan hatalar

| Belirti | Çözüm |
|---|---|
| `betik çalıştırılamıyor / execution policy` | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` |
| `Servise ulaşılamadı` | Pencere #1'de `run.ps1` çalışıyor mu? Port doğru mu? |
| `upstream_unauthorized` (401) | `UPSTREAM_COOKIE` süresi dolmuş — tarayıcıdan yeniden kopyalayın |
| `recaptcha_rejected` | `RECAPTCHA_STATIC_TOKEN` kısa ömürlüdür (~2 dk), yenileyin |
| `model_not_found` | `config\models.yaml` içindeki `upstream_id` hâlâ `REPLACE_...` olabilir |
| Türkçe karakterler bozuk | Betik UTF-8 ayarını kendi yapar; PowerShell 7 kullanmak daha sorunsuzdur |
| Yanıt akmıyor, toplu geliyor | `Invoke-RestMethod` kullanmayın; `chat.ps1` bunu `HttpClient` ile çözer |
