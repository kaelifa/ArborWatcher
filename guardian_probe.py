#!/usr/bin/env python3
"""
guardian_probe.py — after login, find how to enter the Guardian/Parent Portal.

What it does:
- Logs in using login_helper.login_guardian
- Saves a landing screenshot (landing.png)
- Prints and saves any links/buttons that look like "Parent/Guardian Portal"
- Saves a full HTML dump (landing.html) so we can fine‑tune selectors if needed
"""

import os, re, json
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from login_helper import login_guardian

def origin(u: str) -> str:
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}"

def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, slow_mo=200)
        ctx = browser.new_context()
        page = ctx.new_page()

        login_guardian(page)
        base = origin(page.url)
        print("After login URL:", page.url)
        print("Origin:", base)

        # save landing html + screenshot
        html = page.content()
        with open("landing.html", "w", encoding="utf-8") as f:
            f.write(html)
        page.screenshot(path="landing.png", full_page=True)

        # Gather potential entry points
        candidates = []

        # 1) Visible buttons/links containing keywords
        keywords = re.compile(r"(parent|guardian)\s*(portal|access|app)?", re.I)
        for sel in ["a", "button", "[role='button']"]:
            for el in page.locator(sel).all():
                try:
                    t = (el.inner_text() or "").strip()
                    href = el.get_attribute("href") or ""
                    if (t and keywords.search(t)) or ("guardian" in (href or "").lower()):
                        bbox = el.bounding_box() or {}
                        candidates.append({"text": t, "href": href, "selector": sel, "bbox": bbox})
                except Exception:
                    pass

        # 2) Any anchor with guardian in href (even if not visible)
        try:
            hrefs = page.eval_on_selector_all("a[href*='guardian']", "els => els.map(e => e.getAttribute('href')).filter(Boolean)") or []
            for h in hrefs:
                candidates.append({"text": "", "href": h, "selector": "a[href*='guardian']"})
        except Exception:
            pass

        # Deduplicate
        seen = set()
        unique = []
        for c in candidates:
            key = (c.get("text",""), c.get("href",""))
            if key in seen: continue
            seen.add(key)
            unique.append(c)

        # Save JSON + print to console
        with open("guardian_candidates.json", "w", encoding="utf-8") as f:
            json.dump(unique, f, indent=2, ensure_ascii=False)

        print("\nGuardian entry candidates (also saved to guardian_candidates.json):")
        if not unique:
            print("  (none found)")
        for i, c in enumerate(unique, 1):
            print(f"{i:2d}. text={c.get('text')!r} href={c.get('href')!r} selector={c.get('selector')}")

        input("\nPress Enter to close…")
        browser.close()

if __name__ == "__main__":
    main()
