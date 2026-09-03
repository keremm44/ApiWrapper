"""Uygulama yapılandırması — tek doğruluk kaynağı.

Tüm ayarlar ortam değişkenlerinden (veya .env dosyasından) okunur.
Hedef domain ve model kimlikleri KODA GÖMÜLMEZ; config'den gelir.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

UnsupportedParamPolicy = Literal["ignore", "hint", "error"]
RecaptchaProviderName = Literal["static", "noop", "browser", "external"]


class Settings(BaseSettings):
    """Ortam değişkeni tabanlı ayarlar."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- server
    app_name: str = "ApiWrapper"
    app_version: str = "1.0.0"
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = True
    root_path: str = ""

    # ------------------------------------------------------------------ auth
    #: Yerel istemcilerin kullanacağı API anahtarları (virgülle ayrılmış).
    api_keys: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["sk-local-dev-key"]
    )
    #: True ise kimlik doğrulama tamamen devre dışı (yalnız güvenli yerel ağda).
    auth_disabled: bool = False

    # ------------------------------------------------------------------ CORS
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["*"])
    cors_allow_credentials: bool = False

    # -------------------------------------------------------------- upstream
    #: Örn. "example-llm.com" (şemasız!). Zorunlu alan.
    target_domain: str = "localhost"
    upstream_scheme: Literal["https", "http"] = "https"
    #: Stream uç noktası şablonu; {chat_id} yer tutucusu zorunlu.
    upstream_stream_path: str = "/nextjs-api/stream/post-to-evaluation/{chat_id}"
    #: Tarayıcıdaki sohbet sayfası yolu (referer üretimi için).
    upstream_referer_path: str = "/c/{chat_id}"
    upstream_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    )
    #: Ham Cookie başlığı (oturum gerekiyorsa).
    upstream_cookie: str = ""
    #: Doğrudan bearer token. Verilirse çerezden ayıklamaya gerek kalmaz (en yüksek öncelik).
    upstream_access_token: str = ""
    #: Cookie içinden `access_token` ayıklanıp `Authorization: Bearer` olarak eklensin mi?
    upstream_auth_from_cookie: bool = True
    #: Token'ın aranacağı çerez adları (virgülle ayrılmış). Boşsa sezgisel arama yapılır.
    upstream_token_cookie_names: Annotated[list[str], NoDecode] = Field(default_factory=list)
    #: Authorization başlığının şeması. Boş bırakılırsa token ham olarak gönderilir.
    upstream_auth_scheme: str = "Bearer"
    #: Token süresi dolmuşsa uyar (JWT `exp`); istek yine de denenir.
    upstream_warn_on_expired_token: bool = True
    #: Ek başlıklar, "K1=V1;K2=V2" biçiminde.
    upstream_extra_headers: str = ""
    upstream_proxy: str | None = None
    upstream_verify_tls: bool = True
    upstream_http2: bool = True

    # -------------------------------------------------------------- timeouts
    connect_timeout: float = 10.0
    read_timeout: float = 300.0
    write_timeout: float = 30.0
    pool_timeout: float = 10.0
    #: Tek bir upstream isteği için toplam üst sınır (saniye).
    total_request_timeout: float = 600.0
    #: İki token arasında izin verilen en uzun sessizlik.
    stream_idle_timeout: float = 120.0

    # ----------------------------------------------------------------- pools
    max_connections: int = 200
    max_keepalive_connections: int = 50
    #: Upstream'e aynı anda gidebilecek istek sayısı.
    max_concurrent_upstream: int = 32

    # ----------------------------------------------------------------- retry
    retry_max_attempts: int = 3
    retry_base_delay: float = 0.5
    retry_max_delay: float = 8.0

    # ------------------------------------------------------- circuit breaker
    breaker_enabled: bool = True
    breaker_failure_threshold: int = 5
    breaker_reset_timeout: float = 30.0

    # ------------------------------------------------------------ rate limit
    rate_limit_enabled: bool = True
    #: Dakikadaki istek sayısı (anahtar başına).
    rate_limit_rpm: int = 120
    #: Ani yük için kova kapasitesi.
    rate_limit_burst: int = 30

    # ----------------------------------------------------------------- input
    max_body_bytes: int = 4 * 1024 * 1024
    max_messages: int = 400
    max_prompt_chars: int = 500_000
    unsupported_params: UnsupportedParamPolicy = "ignore"

    # ------------------------------------------------------------- recaptcha
    recaptcha_provider: RecaptchaProviderName = "static"
    recaptcha_static_token: str = ""
    recaptcha_token_ttl: float = 100.0
    #: external sağlayıcı için: token döndüren HTTP uç noktası.
    recaptcha_external_url: str = ""
    recaptcha_external_api_key: str = ""
    recaptcha_external_timeout: float = 60.0
    #: browser (Playwright) sağlayıcı için.
    recaptcha_site_key: str = ""
    recaptcha_action: str = "chat"
    recaptcha_browser_timeout: float = 45.0

    # --------------------------------------------------------------- session
    session_ttl: float = 1800.0
    session_cache_size: int = 1024
    #: True ise her istek için yeni chat_id üretilir (durumsuz mod).
    session_stateless: bool = True

    # --------------------------------------------------------------- models
    models_file: str = "config/models.yaml"

    # -------------------------------------------------------------- metrics
    metrics_enabled: bool = True

    # ----------------------------------------------------------- validators
    @field_validator(
        "api_keys", "cors_origins", "upstream_token_cookie_names", mode="before"
    )
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """Virgülle ayrılmış listeyi (veya JSON dizisini) listeye çevirir."""
        if isinstance(v, str):
            text = v.strip()
            if text.startswith("[") and text.endswith("]"):
                import json

                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            return [item.strip() for item in text.split(",") if item.strip()]
        return v

    @field_validator("target_domain", mode="before")
    @classmethod
    def _strip_scheme(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().rstrip("/")
            for prefix in ("https://", "http://"):
                if v.startswith(prefix):
                    v = v[len(prefix) :]
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v

    # ------------------------------------------------------------ computed
    @property
    def base_url(self) -> str:
        return f"{self.upstream_scheme}://{self.target_domain}"

    @property
    def origin(self) -> str:
        return self.base_url

    def stream_url(self, chat_id: str) -> str:
        return self.base_url + self.upstream_stream_path.format(chat_id=chat_id)

    def referer_url(self, chat_id: str) -> str:
        return self.base_url + self.upstream_referer_path.format(chat_id=chat_id)

    def parsed_extra_headers(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in self.upstream_extra_headers.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, _, value = part.partition("=")
            key, value = key.strip(), value.strip()
            if key:
                out[key] = value
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Süreç ömrü boyunca tek Settings örneği."""
    return Settings()
