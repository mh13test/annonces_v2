import os
import re
import time
import hashlib
from typing import Optional, Dict, Any, Tuple

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, AnyHttpUrl
from playwright.async_api import async_playwright, TimeoutError as PwTimeoutError

# -----------------------------
# Config (env vars)
# -----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()  # ex: -1001234567890

MIN_LAND_M2 = int(os.getenv("MIN_LAND_M2", "0").strip() or "0")
DEDUP_TTL_SECONDS = int(os.getenv("DEDUP_TTL_SECONDS", "86400").strip() or "86400")

# Proxy (recommande sur Railway)
PROXY_SERVER = os.getenv("PROXY_SERVER", "").strip()  # ex: http://host:port  (ou socks5://host:port)
PROXY_USER = os.getenv("PROXY_USER", "").strip()
PROXY_PASS = os.getenv("PROXY_PASS", "").strip()

# Navigateur
HEADLESS = (os.getenv("HEADLESS", "true").strip().lower() != "false")
NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "45000").strip() or "45000")
POST_LOAD_WAIT_MS = int(os.getenv("POST_LOAD_WAIT_MS", "1500").strip() or "1500")

# Garde-fous
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "250000").strip() or "250000")
DISABLE_PREVIEW = (os.getenv("TELEGRAM_DISABLE_PREVIEW", "false").strip().lower() == "true")

app = FastAPI(title="Spitogatos Enricher (Playwright + Proxy)")

# Dedup in-memory (MVP). Pour industrialiser: Redis.
_seen: Dict[str, float] = {}


class Payload(BaseModel):
    url: AnyHttpUrl
    source: Optional[str] = "spitogatos_email"
    email_id: Optional[str] = None


def now_ts() -> float:
    return time.time()


def dedup_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def gc_seen() -> None:
    t = now_ts()
    expired = [k for k, v in _seen.items() if (t - v) > DEDUP_TTL_SECONDS]
    for k in expired:
        _seen.pop(k, None)


def looks_like_challenge(html: str) -> bool:
    h = (html or "").lower()
    return any(s in h for s in [
        "captcha",
        "cf-challenge",
        "cloudflare",
        "checking your browser",
        "/cdn-cgi/",
        "turnstile",
    ])


def clean_int(s: str) -> Optional[int]:
    if not s:
        return None
    s = s.replace("\xa0", " ").strip()
    # Retire tout sauf digits
    s = re.sub(r"[^\d]", "", s)
    return int(s) if s.isdigit() else None


def extract_fields(text: str) -> Dict[str, Any]:
    """
    Extraction heuristique depuis le texte visible.
    Objectif prioritaire: land/plot area.
    """
    # Patterns terrain (EN + GR)
    land_patterns = [
        # Plot area / Land area: ... 2,640
        r"(?i)\b(plot|land)\s*area\b[^0-9]{0,80}([0-9]{1,3}(?:[.,\s][0-9]{3})*)",
        # Lot size
        r"(?i)\blot\s*size\b[^0-9]{0,80}([0-9]{1,3}(?:[.,\s][0-9]{3})*)",
        # Grec (variable selon pages)
        r"(?i)(Εμβαδόν\s*οικοπέδου|Εμβαδόν|οικόπεδο|οικοπέδου)[^0-9]{0,120}([0-9]{1,3}(?:[.,\s][0-9]{3})*)",
    ]

    land_m2 = None
    for pat in land_patterns:
        m = re.search(pat, text)
        if m:
            land_m2 = clean_int(m.groups()[-1])
            if land_m2:
                break

    # Prix (EUR) - best-effort
    price_m = re.search(r"(?i)(€|eur)\s*([0-9]{1,3}(?:[.,\s][0-9]{3})*)", text)
    price_eur = clean_int(price_m.group(2)) if price_m else None

    # Surface habitable (m²) - best-effort
    # (attention: il peut y avoir plusieurs m²; on prend le premier)
    area_m = re.search(r"(?i)\b([0-9]{1,3}(?:[.,\s][0-9]{3})*)\s*m²\b", text)
    area_m2 = clean_int(area_m.group(1)) if area_m else None

    return {
        "land_m2": land_m2,
        "price_eur": price_eur,
        "area_m2": area_m2,
    }


async def telegram_send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": DISABLE_PREVIEW,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, data=data)
        if r.status_code != 200:
            raise RuntimeError(f"Telegram error: {r.status_code} {r.text}")


def format_message(url: str, fields: Dict[str, Any]) -> str:
    land = fields.get("land_m2")
    price = fields.get("price_eur")
    area = fields.get("area_m2")

    parts = ["Nouvelle annonce qualifiee"]
    if land is not None:
        parts.append(f"Terrain: {land} m²")
    if price is not None:
        parts.append(f"Prix: {price} EUR")
    if area is not None:
        parts.append(f"Surface: {area} m²")
    parts.append(url)
    return "\n".join(parts)


async def fetch_page_text(url: str) -> Tuple[str, str]:
    """
    Retourne (html, text) via Playwright.
    Proxy optionnel (mais recommande sur Railway).
    """
    proxy_cfg = None
    if PROXY_SERVER:
        proxy_cfg = {"server": PROXY_SERVER}
        if PROXY_USER:
            proxy_cfg["username"] = PROXY_USER
        if PROXY_PASS:
            proxy_cfg["password"] = PROXY_PASS

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            proxy=proxy_cfg,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = await browser.new_context(
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )

        # Option: réduire le bruit réseau (images/fonts) pour stabilité et cout proxy
        async def route_handler(route):
            r = route.request
            if r.resource_type in ("image", "font", "media"):
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", route_handler)

        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(POST_LOAD_WAIT_MS)

            html = await page.content()
            text = await page.inner_text("body")

            # Clamp pour eviter payloads extremes
            if len(text) > MAX_TEXT_CHARS:
                text = text[:MAX_TEXT_CHARS]

            return html, text

        finally:
            await context.close()
            await browser.close()


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "proxy_configured": bool(PROXY_SERVER),
        "min_land_m2": MIN_LAND_M2,
    }


@app.post("/webhook/spitogatos")
async def webhook(payload: Payload):
    gc_seen()
    url = str(payload.url)

    key = dedup_key(url)
    if key in _seen:
        return {"status": "dedup_skipped"}

    _seen[key] = now_ts()

    try:
        html, text = await fetch_page_text(url)
    except PwTimeoutError:
        raise HTTPException(status_code=504, detail="Timeout loading page")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Browser error: {e}")

    if looks_like_challenge(html):
        # Important: en prod, tu peux ici "retry later" plutot que fail hard.
        raise HTTPException(status_code=403, detail="Blocked by challenge/CAPTCHA")

    fields = extract_fields(text)
    land_m2 = fields.get("land_m2")

    if land_m2 is None:
        return {"status": "no_land_field", "url": url, "fields": fields}

    if MIN_LAND_M2 and land_m2 < MIN_LAND_M2:
        return {"status": "filtered_out", "url": url, "land_m2": land_m2}

    msg = format_message(url, fields)

    try:
        await telegram_send(msg)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Telegram send failed: {e}")

    return {"status": "posted", "url": url, "land_m2": land_m2, "fields": fields}
