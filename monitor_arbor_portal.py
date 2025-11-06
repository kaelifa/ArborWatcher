#!/usr/bin/env python3
"""
monitor_arbor_portal.py — Polite watcher for Arbor Parent Portal

What it does:
- Logs in via login_helper.login_guardian
- Enters Guardian/Parent shell (auto‑retry)
- Crawls key sections (messages, comms, noticeboard, calendar, trips, payments, clubs, documents)
- Compares a compact hash of recent items against a state file
- Sends a Telegram digest ONLY when changes are detected
- Uses polite crawling (throttled actions, exponential backoff with jitter, identifiable headers)

Environment (.env or GitHub Secrets):
  ARBOR_BASE_URL=...
  ARBOR_EMAIL=...
  ARBOR_PASSWORD=...
  ARBOR_CHILD_DOB=...         # optional
  ARBOR_LOGIN_METHOD=email    # optional

  TELEGRAM_TOKEN=123:ABC
  TELEGRAM_CHAT_ID=123456789

  # Optional
  STATE_FILE=.arbor_everything_state.json

Usage (locally):
  python3 -m pip install playwright python-dotenv requests pandas
  python3 -m playwright install
  python3 monitor_arbor_portal.py
"""

# --- Stdlib
import os
import re
import json
import time
import hashlib
import argparse
from typing import List, Dict, Optional
from dataclasses import dataclass
from urllib.parse import urlparse

# --- Third-party
import requests
from playwright.sync_api import sync_playwright, Page
try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    load_dotenv = None

# --- Local login
from login_helper import login_guardian

# ---------- Config ----------
if load_dotenv:
    load_dotenv()

ARBOR_EMAIL = os.getenv("ARBOR_EMAIL", "")
ARBOR_PASSWORD = os.getenv("ARBOR_PASSWORD", "")
STATE_FILE = os.getenv("STATE_FILE", ".arbor_everything_state.json")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Defaults (polite) — overridden by --fast or env vars
MIN_DELAY = float(os.getenv("ARBOR_MIN_DELAY", "1.2"))
MAX_DELAY = float(os.getenv("ARBOR_MAX_DELAY", "2.6"))

# ---------- Polite access helpers ----------
import random

def polite_sleep(min_s=None, max_s=None):
    """Random delay to avoid rapid-fire behaviour."""
    lo = MIN_DELAY if min_s is None else min_s
    hi = MAX_DELAY if max_s is None else max_s
    if hi <= 0:
        return
    time.sleep(random.uniform(lo, hi))

def polite_request_with_backoff(fn, max_attempts=3, base_delay=2.0, max_delay=60.0):
    """Run fn() with exponential backoff + jitter on failures."""
    attempt = 0
    while attempt < max_attempts:
        try:
            return fn()
        except Exception as exc:
            attempt += 1
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay *= random.uniform(0.7, 1.3)
            print(f"[polite] attempt {attempt} failed: {exc}. Retrying in {delay:.1f}s")
            time.sleep(delay)
    return fn()

def polite_headers() -> dict:
    """Identifiable headers so admins can contact you if needed."""
    return {
        "User-Agent": "ArborWatcher/1.0 (+email@kristina.digital)",
        "X-ArborWatcher-Contact": "email@kristina.digital",
        "Accept-Language": "en-GB,en;q=0.9",
    }

def polite_requests_get(url, session=None, **kwargs):
    polite_sleep()
    session = session or requests.Session()
    headers = polite_headers()
    headers.update(kwargs.pop("headers", {}))
    kwargs["headers"] = headers
    return polite_request_with_backoff(lambda: session.get(url, timeout=20, **kwargs))

def polite_requests_post(url, session=None, **kwargs):
    polite_sleep()
    session = session or requests.Session()
    headers = polite_headers()
    headers.update(kwargs.pop("headers", {}))
    kwargs["headers"] = headers
    return polite_request_with_backoff(lambda: session.post(url, timeout=20, **kwargs))

def polite_goto(page: Page, url: str):
    polite_sleep()
    return polite_request_with_backoff(lambda: page.goto(url, wait_until="domcontentloaded", timeout=45000))

# ---------- Core helpers ----------
def origin(u: str) -> str:
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}"

@dataclass
class Item:
    section: str
    title: str
    meta: str
    when: str

def assert_not_permission_modal(page: Page):
    try:
        body = (page.text_content("body") or "").lower()
    except Exception:
        body = ""
    if "it seems like you can't do this" in body:
        raise RuntimeError("🚫 Permission modal: staff-only or invalid route. Enter guardian shell first.")

def ensure_guardian_shell(page: Page) -> bool:
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

    # Fallback: any link with guardian
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
    ok = ensure_guardian_shell(page)
    try:
        assert_not_permission_modal(page)
    except RuntimeError:
        ok = ensure_guardian_shell(page) or ok
        assert_not_permission_modal(page)
    if not ok:
        pass

def goto(page: Page, base: str, path: str):
    url = f"{base}{path}"
    polite_goto(page, url)
    page.wait_for_load_state("networkidle")
    assert_not_permission_modal(page)

def lazy_scroll_all(page: Page, container: Optional[str] = None, max_passes: int = 40, pause: float = 0.6):
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

            meta, when = "", ""
            try:
                smalls = el.locator("small, .meta, .subtext, .subtitle").all()
                if smalls:
                    meta_text = " ".join((s.inner_text() or "").strip() for s in smalls[:2])
                    parts = re.split(r"·|\||–|-{1,2}", meta_text)
                    if parts: meta = parts[0].strip()
                    if len(parts) > 1: when = parts[1].strip()
            except Exception:
                pass

            rows.append(Item(section="", title=title, meta=meta, when=when))
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

# ---------- State + Digest ----------
def load_state(path: str) -> Dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(path: str, data: Dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()

def build_digest(all_items: List[Item]) -> str:
    if not all_items:
        return "No new items."
    # group
    by_section: Dict[str, List[Item]] = {}
    for it in all_items:
        by_section.setdefault(it.section, []).append(it)
    # format
    lines = ["Arbor updates:"]
    for sec in sorted(by_section):
        lines.append(f"• {sec}")
        for i in by_section[sec][:8]:
            suffix = f" — {i.when}" if i.when else ""
            meta = f" ({i.meta})" if i.meta else ""
            lines.append(f"  - {i.title}{meta}{suffix}")
    return "\n".join(lines)

# ---------- Notifications ----------
def post_telegram(text: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        polite_requests_post(api, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        })
    except Exception as e:
        print(f"[warn] telegram failed: {e}")

# ---------- Main ----------
def main() -> int:
    parser = argparse.ArgumentParser(description="Polite watcher for Arbor Parent Portal (Telegram alerts)")
    parser.add_argument("--headless", action="store_true", help="Run headless (default headful)")
    parser.add_argument("--fast", action="store_true", help="Reduce delays for local runs")
    args = parser.parse_args()

    global MIN_DELAY, MAX_DELAY
    if args.fast:
        MIN_DELAY, MAX_DELAY = 0.2, 0.6

    if not (ARBOR_EMAIL and ARBOR_PASSWORD):
        print("Set ARBOR_EMAIL and ARBOR_PASSWORD in environment or .env")
        return 2

    state = load_state(STATE_FILE)
    last = state.get("last", {}) if isinstance(state, dict) else {}

    all_items: List[Item] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless, slow_mo=0 if args.headless else 200)
        context = browser.new_context()
        page = context.new_page()

        # Login & enter guardian shell
        login_guardian(page)
        enter_guardian_or_retry(page)
        base = origin(page.url)
        print("🔗 Using origin:", base)

        sections = {
            "Messages":       ["/guardian#/messages"],
            "Communications": ["/guardian#/communications", "/guardian#/comms", "/guardian#/communication-log"],
            "Noticeboard":    ["/guardian#/noticeboard", "/guardian#/announcements", "/guardian#/news"],
            "Calendar":       ["/guardian#/calendar", "/guardian#/events"],
            "Trips":          ["/guardian#/trips", "/guardian#/activities"],
            "Payments":       ["/guardian#/payments", "/guardian#/accounts"],
            "Clubs":          ["/guardian#/clubs", "/guardian#/activities/clubs"],
            "Documents":      ["/guardian#/documents", "/guardian#/report-cards", "/guardian#/letters"],
        }

        # Crawl sections (light delay between them) with progress logs
        for sec, paths in sections.items():
            print(f"→ Checking {sec} …")
            try:
                items = fetch_section(page, base, sec, paths)
                all_items.extend(items)
            except Exception as e:
                print(f"[warn] section {sec} failed: {e}")
            polite_sleep()

        browser.close()

    if not all_items:
        print("No items gathered. Portal UI may have changed or nothing is visible for this account.")
        return 0

    # Compute per-section hashes on the first N entries (stable, low-noise)
    current: Dict[str, str] = {}
    by_section: Dict[str, List[Item]] = {}
    for it in all_items:
        by_section.setdefault(it.section, []).append(it)

    changed_sections = []
    for sec, items in by_section.items():
        basis = "\n".join(f"{i.title}|{i.meta}|{i.when}" for i in items[:10])
        h = sha(basis)
        current[sec] = h
        if last.get(sec) != h:
            changed_sections.append(sec)

    if not changed_sections:
        print("No changes detected.")
        return 0

    digest = build_digest(all_items)
    print(digest)
    post_telegram(digest)

    state["last"] = current
    save_state(STATE_FILE, state)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
