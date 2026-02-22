import os, sys, asyncio
from playwright.async_api import async_playwright

URL = os.getenv("TEST_URL", "").strip()
if not URL:
    print("Missing TEST_URL env var", file=sys.stderr)
    sys.exit(2)

def looks_like_challenge(html: str) -> bool:
    h = (html or "").lower()
    return any(x in h for x in ["captcha", "cf-challenge", "cloudflare", "checking your browser"])

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            locale="en-US",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"),
        )
        page = await context.new_page()
        await page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(1500)

        html = await page.content()
        text = await page.inner_text("body")

        blocked = looks_like_challenge(html)
        print("BLOCKED" if blocked else "OK")
        print(f"text_len={len(text)} html_len={len(html)}")

        await context.close()
        await browser.close()

asyncio.run(main())
