"""Geliştirme için sahte upstream: AI SDK data-stream yayınlar.

Kullanım:
    python scripts/mock_upstream.py            # 9100 portunda dinler
Ardından .env içinde:
    TARGET_DOMAIN=127.0.0.1:9100
    UPSTREAM_SCHEME=http
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

app = FastAPI(title="Mock Upstream LLM")

#: True ise, gerçek hedef gibi davranıp Authorization başlığını zorunlu kılar.
REQUIRE_AUTH = os.getenv("MOCK_REQUIRE_AUTH", "1") not in ("0", "false", "False")


@app.get("/c/{chat_id}")
async def chat_page(chat_id: str) -> HTMLResponse:
    return HTMLResponse(f"<html><body>chat {chat_id}</body></html>")


@app.post("/nextjs-api/stream/post-to-evaluation/{chat_id}")
async def stream(chat_id: str, request: Request):
    if REQUIRE_AUTH:
        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("bearer ") or len(authorization) < 20:
            return JSONResponse(
                {"error": "Unauthorized: missing or invalid access token"},
                status_code=401,
            )

    raw = (await request.body()).decode("utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}
    prompt = payload.get("userMessage", {}).get("content", "")
    model = payload.get("modelAId", "unknown")

    reply = (
        f"[{model}] Merhaba! İsteğinizi aldım ({len(prompt)} karakter). "
        "Bu, sahte upstream tarafından üretilmiş akışlı bir yanıttır. "
        "Türkçe karakterler: ğüşiöçİĞÜŞÖÇ."
    )

    async def gen():
        yield f'f:{json.dumps({"messageId": chat_id})}\n'.encode()
        for word in reply.split(" "):
            await asyncio.sleep(0.02)
            yield f"0:{json.dumps(word + ' ', ensure_ascii=False)}\n".encode()
        done = {
            "finishReason": "stop",
            "usage": {"promptTokens": len(prompt) // 4, "completionTokens": len(reply) // 4},
        }
        yield f"e:{json.dumps(done)}\n".encode()
        yield f"d:{json.dumps(done)}\n".encode()

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9100, log_level="warning")
