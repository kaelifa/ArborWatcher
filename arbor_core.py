#!/usr/bin/env python3
"""
arbor_copy.py — Consolidated one-off exporter for Arbor Parent Portal

- Logs in via login_helper.login_guardian (works with .education/.sc & SSO variants)
- Enters the Guardian/Parent shell safely (with auto-retry)
- Crawls key sections and saves JSON+CSV under exports/<timestamp>/
- Best-effort downloads documents
- Creates separate ZIPs per populated section (optional --zip)
- Polite crawling: throttled navigation, backoff & identifiable headers

Usage:
  python3 -m pip install playwright python-dotenv requests pandas
  python3 -m playwright install
  python3 arbor_copy.py --zip --headless   # or omit --headless to watch

.env keys:
  ARBOR_BASE_URL, ARBOR_EMAIL, ARBOR_PASSWORD
  (optional) ARBOR_CHILD_DOB, ARBOR_LOGIN_METHOD
"""

# --- Standard library
import os
import re
import json
import time
import zipfile
import argparse
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Optional, Dict
from urllib.parse import urlparse

# --- Third-party
import pandas as pd
import requests
from playwright.sync_api import sync_playwright, Page
try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    load_dotenv = None

# --- Local
# Requires login_helper.py in the same folder, providing login_guardian(page)
from login_helper import login_guardian

# ---------- Config ----------
if load_dotenv:
    load_dotenv()

DEFAULT_BASE = os.getenv("ARBOR_BASE_URL", "https://login.arbor.sc").rstrip("/")
ARBOR_EMAIL = os.getenv("ARBOR_EMAIL")
ARBOR_PASSWORD = os.getenv("ARBOR_PASSWORD")

# ---------- Polite access helpers ----------
import random

def polite_sleep(min_s=0.5, max_s=1.5):
    """Sleep a random interval between actions to avoid rapid-fire requests."""
    time.sleep(random.uniform(min_s, max_s))

def polite_request_with_backoff(fn, max_attempts=3, base_delay=2.0, max_delay=60.0):
    """
    Executes fn() with polite exponential backoff on failure.
    Example: polite_request_with_backoff(lambda: page.goto(url, wait_until='domcontentloaded', timeout=45000))
    """
    attempt = 0
    while attempt < max_attempts:
        try:
            result = fn()
            return result
        except Exception as exc:
            attempt += 1
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay *= random.uniform(0.7, 1.3)  # jitter
            print(f"[polite] attempt {attempt} failed: {exc}. Retrying in {delay:.1f}s")
            time.sleep(delay)
    # Final attempt — bubble up any error
    return fn()

def polite_goto(page: Page, url: str):
    """Playwright navigation wrapped in polite backoff and light delay."""
    polite_sleep()
    return polite_request_with_backoff(
        lambda: page.goto(url, wait_until="domcontentloaded", timeout=45000)
    )

def polite_headers() -> dict:
    """Identifiable, polite headers for direct HTTP requests."""
    return {
        "User-Agent": "ArborWatcher/1.0 (+email@kristina.digital)",
        "X-ArborWatcher-Contact": "email@kristina.digital",
        "Accept-Language": "en-GB,en;q=0.9",
    }

def polite_requests_get(url, session=None, **kwargs):
    """requests.get() with polite headers, delay and backoff."""
    polite_sleep()
    session = session or requests.Session()
    headers = polite_headers()
    headers.update(kwargs.pop("headers", {}))
    kwargs["headers"] = headers
    return polite_request_with_backoff(lambda: session.get(url, timeout=20, **kwargs))

# ---------- Core helpers ----------
def origin(u: str) -> str:
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}"

def nowstamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

@dataclass
class Item:
    section: str
    title: str
    meta: str
    when: str
    href: Optional[str] = None
    preview: Optional[str] = None

def assert_not_permission_modal(page: Page):
    """Detect Arbor's permission/invalid-route modal after a navigation."""
    try:
        body = (page.text_content("body") or "").lower()
    except Exception:
        body = ""
    if "it seems like you can't do this" in body:
        raise RuntimeError("🚫 Permission modal: staff-only or invalid route. Enter guardian shell first.")

def ensure_guardian_shell(page: Page) -> bool:
    """
    Ensure we're inside the Guardian/Parent Portal SPA.
    Returns True if URL contains 'guardian#', else attempts to enter via UI and returns best-effort result.
    """
    if re.search(r"guardian#", page.url, re.I):
        return True

    candidates = [
        ("role", ("link",   re.compile(r"(parent|guardian)\s+portal", re.I))),
        ("role", ("button", re.compile(r"(parent|guardian)\s+portal", re.I))),
        ("css",  "a:has-text('Parent Portal')"),
        ("css",  "a:has-text('Guardian')"),
        ("css",  "button:has-text('Parent Portal')"),
        ("css",  "button:has-text('Guardian')"),
    ]
    for kind, arg in candidates:
        try:
            if kind == "role":
                role, name = arg
                el = page.get_by_role(role, name=name)
                if el and el.is_visible():
                    el.click()
                    page.wait_for_load_state("networkidle")
                    if re.search(r"guardian#", page.url, re.I):
                        return True
            else:
                el = page.locator(arg).first
                if el and el.is_visible():
                    el.click()
                    page.wait_for_load_state("networkidle")
                    if re.search(r"guardian#", page.url, re.I):
                        return True
        except Exception:
            pass

    # Fallback: follow any <a> whose href contains 'guardian'
    try:
        hrefs = page.eval_on_selector_all(
            "a[href*='guardian']",
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
        ) or []
        if hrefs:
            target = hrefs[0]
            if target.startswith("http"):
                polite_goto(page, target)
            else:
                base = origin(page.url)
                if not target.startswith('/'):
                    target = '/' + target
                polite_goto(page, base + target)
            page.wait_for_load_state("networkidle")
            return bool(re.search(r"guardian#", page.url, re.I))
    except Exception:
        pass
    return False

def enter_guardian_or_retry(page: Page) -> None:
    """Self-healing entry into Guardian shell; retries once if permission modal appears."""
    ok = ensure_guardian_shell(page)
    try:
        assert_not_permission_modal(page)
    except RuntimeError:
        ok = ensure_guardian_shell(page) or ok
        assert_not_permission_modal(page)
    if not ok:
        # Some tenants lazy-load; continue as downstream routes use guardian# paths
        pass

def goto(page: Page, base: str, path: str):
    """Navigate within the guardian app and fail fast on permission modal."""
    url = f"{base}{path}"
    polite_goto(page, url)
    page.wait_for_load_state("networkidle")
    assert_not_permission_modal(page)

def lazy_scroll_all(page: Page, container: Optional[str] = None, max_passes: int = 40, pause: float = 0.6):
    """Scroll to load lazy lists."""
    last_h = -1
    for _ in range(max_passes):
        if container:
            page.locator(container).evaluate("(el) => el.scrollTo(0, el.scrollHeight)")
        else:
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(pause)
        h = page.evaluate("() => document.body.scrollHeight")
        if h == last_h:
            break
        last_h = h

def collect_items(page: Page, limit: Optional[int] = None) -> List[Item]:
    rows: List[Item] = []
    cards = page.locator("main").locator("li, div[role='listitem'], .ListItem, .card, .row").all()
    for el in cards:
        try:
            title = ""
            for sel in ("h1","h2","h3","h4",".title",".Heading","strong"):
                try:
                    t = el.locator(sel).first
                    if t and t.is_visible():
                        title = (t.inner_text() or "").strip()
                        if title: break
                except Exception:
                    pass
            if not title:
                text = (el.inner_text() or "").strip()
                title = (text.split("\n")[0][:180] if text else "(untitled)")

            meta, when, preview, href = "", "", None, None
            try:
                smalls = el.locator("small, .meta, .subtext, .subtitle").all()
                if smalls:
                    meta_text = " ".join((s.inner_text() or "").strip() for s in smalls[:2])
                    parts = re.split(r"·|\||–|-{1,2}", meta_text)
                    if parts: meta = parts[0].strip()
                    if len(parts) > 1: when = parts[1].strip()
            except Exception:
                pass
            try:
                pv = el.locator("p, .preview, .desc, .description").first
                if pv and pv.is_visible():
                    preview = (pv.inner_text() or "").strip()
            except Exception:
                pass
            try:
                a = el.locator("a").first
                if a:
                    href = a.get_attribute("href")
            except Exception:
                pass

            rows.append(Item(section="", title=title, meta=meta, when=when, href=href, preview=preview))
            if limit and len(rows) >= limit:
                break
        except Exception:
            continue
    return rows

def fetch_section(page: Page, base: str, section: str, paths: List[str]) -> List[Item]:
    for path in paths:
        try:
            goto(page, base, path)
            lazy_scroll_all(page)
            items = collect_items(page)
            if items:
                for it in items: it.section = section
                return items
        except Exception:
            continue
    return []

def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip()[:180] or "item"

def download_documents(page: Page, base: str, outdir: str) -> List[Item]:
    """Best-effort download of docs on the Documents-like pages."""
    docs = fetch_section(page, base, "Documents",
                         ["/guardian#/documents", "/guardian#/report-cards", "/guardian#/letters"])
    if not docs:
        return []

    os.makedirs(os.path.join(outdir, "docs"), exist_ok=True)

    anchors = page.locator("a").all()
    for a in anchors:
        href = a.get_attribute("href") or ""
        text = (a.inner_text() or "").strip()
        if not href:
            continue
        looks_file = re.search(r"\.(pdf|docx?|xlsx?|csv|png|jpe?g)$", href, re.I) or ("download" in href.lower())
        if not looks_file:
            continue
        filename = sanitize(text or href.split("/")[-1])
        # try Playwright's download flow
        try:
            with page.expect_download(timeout=3500) as d_info:
                a.click(button="left")
            d = d_info.value
            d.save_as(os.path.join(outdir, "docs", filename))
            continue
        except Exception:
            pass
        # fallback: authenticated requests (same-origin only)
        try:
            cookies = page.context.cookies()
            jar = requests.cookies.RequestsCookieJar()
            for c in cookies:
                jar.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path"))
            url = href if href.startswith("http") else base.rstrip("/") + "/" + href.lstrip("/")
            r = polite_requests_get(url, cookies=jar)
            if r.ok and r.content:
                ext = ""
                ct = r.headers.get("content-type","")
                if "pdf" in ct: ext = ".pdf"
                elif "msword" in ct or "doc" in ct: ext = ".doc"
                elif "excel" in ct or "sheet" in ct: ext = ".xlsx"
                elif "png" in ct: ext = ".png"
                elif "jpeg" in ct or "jpg" in ct: ext = ".jpg"
                with open(os.path.join(outdir, "docs", filename + ext), "wb") as f:
                    f.write(r.content)
        except Exception:
            pass

    return docs

def rows_to_files(rows: List[Item], outdir: str, basename: str):
    data = [asdict(r) for r in rows]
    with open(os.path.join(outdir, f"{basename}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    pd.DataFrame(data).to_csv(os.path.join(outdir, f"{basename}.csv"), index=False)

# ---------- Export ----------
def export_all(zip_after: bool = False, headless: bool = False):
    if not (ARBOR_EMAIL and ARBOR_PASSWORD):
        raise SystemExit("Set ARBOR_EMAIL and ARBOR_PASSWORD in .env")

    ts = nowstamp()
    outdir = os.path.join("exports", ts)
    ensure_dir(outdir)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, slow_mo=0 if headless else 200)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        # Login using shared helper
        login_guardian(page)

        # Enter guardian shell & bind origin
        enter_guardian_or_retry(page)
        base = origin(page.url)
        print("🔗 Using origin:", base)

        # Crawl sections (polite delay between each)
        sections: Dict[str, List[str]] = {
            "Messages":      ["/guardian#/messages"],
            "Communications":[ "/guardian#/communications", "/guardian#/comms", "/guardian#/communication-log"],
            "Noticeboard":   ["/guardian#/noticeboard", "/guardian#/announcements", "/guardian#/news"],
            "Calendar":      ["/guardian#/calendar", "/guardian#/events"],
            "Trips":         ["/guardian#/trips", "/guardian#/activities"],
            "Payments":      ["/guardian#/payments", "/guardian#/accounts"],
            "Clubs":         ["/guardian#/clubs", "/guardian#/activities/clubs"],
        }
        results: Dict[str, List[Item]] = {}
        for name, paths in sections.items():
            items = fetch_section(page, base, name, paths)
            results[name] = items
            polite_sleep(1.5, 3.0)

        # Documents (also tries to download files)
        docs = download_documents(page, base, outdir)
        results["Documents"] = docs

        browser.close()

    # Save each section
    for sec, items in results.items():
        rows_to_files(items, outdir, sec.lower())

    # Write a manifest
    manifest = {
        "exported_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "base_url": base,
        "sections": {sec: len(items) for sec, items in results.items()}
    }
    with open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    # Per-section ZIPs (skip empties)
    if zip_after:
        section_files = {
            "messages": ["messages.json", "messages.csv"],
            "communications": ["communications.json", "communications.csv"],
            "noticeboard": ["noticeboard.json", "noticeboard.csv"],
            "calendar": ["calendar.json", "calendar.csv"],
            "trips": ["trips.json", "trips.csv"],
            "payments": ["payments.json", "payments.csv"],
            "clubs": ["clubs.json", "clubs.csv"],
            "documents": ["documents.json", "documents.csv"],
        }
        zipped_any = False

        for section, files in section_files.items():
            existing_files = [os.path.join(outdir, f) for f in files if os.path.exists(os.path.join(outdir, f))]
            docs_dir = os.path.join(outdir, "docs")
            if section == "documents" and os.path.isdir(docs_dir):
                for root, _, fs in os.walk(docs_dir):
                    for fn in fs:
                        existing_files.append(os.path.join(root, fn))

            if not existing_files:
                continue  # skip empty section

            zip_path = os.path.join(outdir, f"{section}.zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                for full in existing_files:
                    arc = os.path.relpath(full, start=outdir)
                    z.write(full, arc)
            print(f"✅ Created {zip_path}")
            zipped_any = True

        if zipped_any:
            print(f"🎉 Export complete: {outdir} (separate ZIPs for each populated section)")
        else:
            print(f"⚠️ Export complete but no data found to zip: {outdir}")
    else:
        print(f"✅ Export complete: {outdir}")

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(description="Consolidated Arbor Parent Portal exporter")
    ap.add_argument("--zip", action="store_true", help="Create a zip per populated section")
    ap.add_argument("--headless", action="store_true", help="Run browser headless (default is headful for visibility)")
    args = ap.parse_args()
    export_all(zip_after=args.zip, headless=args.headless)

if __name__ == "__main__":
    main()
