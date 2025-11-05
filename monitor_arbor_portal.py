#!/usr/bin/env python3
"""
Arbor Parent Portal — everything watcher (Telegram-enabled)

Monitors key areas of the Arbor Parent Portal for changes and posts a digest
to Telegram (free push to iPhone/iPad). Email and Discord webhook are optional.

Watched areas (best-effort; selectors are written to be resilient):
- In-app messages
- Communications
- Noticeboard / Announcements
- Calendar / Events
- Trips / Activities
- Payments
- Clubs
- Documents

Env vars (set as GitHub Secrets in Actions)
-------------------------------------------
ARBOR_BASE_URL=https://the-castle-school.uk.arbor.sc
ARBOR_EMAIL=you@example.com
ARBOR_PASSWORD=*********
ARBOR_CHILD_DOB=01/02/2014      # optional

TELEGRAM_TOKEN=123:ABC          # Telegram bot token
TELEGRAM_CHAT_ID=123456789      # your chat id

# Optional extras
DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
REPORT_EMAIL_TO=you@example.com
REPORT_EMAIL_FROM=alerts@example.com
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_TLS=true
SMTP_USER=alerts@example.com
SMTP_PASS=*********
STATE_FILE=.arbor_everything_state.json

Usage (locally)
---------------
python -m pip install playwright python-dotenv requests pandas
python -m playwright install
python monitor_arbor_portal.py
"""

import os
import sys
import requests
from dotenv import load_dotenv

# Load .env file
if load_dotenv:
    load_dotenv()

import os, re
from urllib.parse import urlparse, urlunparse

# read from env as you do now
ARBOR_BASE_URL = os.getenv("ARBOR_BASE_URL", "https://login.arbor.sc").rstrip("/")

def normalize_base_url(url: str) -> str:
    """Prefer the .sc tenant host; fall back to given URL."""
    u = url.rstrip("/")
    # If a reset link or .education host was pasted, try to map to .sc
    if ".education" in u and ".arbor.education" in u:
        # e.g. https://the-castle-school.uk.arbor.education/...  -> https://the-castle-school.uk.arbor.sc
        parts = urlparse(u)
        host = parts.netloc.replace(".arbor.education", ".arbor.sc")
        return f"https://{host}"
    return u

ARBOR_BASE_URL = normalize_base_url(ARBOR_BASE_URL)

# ---------------------------------------------------------------------
# Telegram test helper (only runs if you use --test)
# ---------------------------------------------------------------------
def telegram_test():
    """Send a one-off test message to confirm Telegram config works."""
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("⚠️  Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID in .env")
        return False
    try:
        msg = "✅ Telegram test passed — watcher starting."
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            print("⚠️  Telegram test failed:", data)
            return False
        print("✅ Telegram connection confirmed.")
        return True
    except Exception as e:
        print("⚠️  Telegram test error:", e)
        return False
# ---------------------------------------------------------------------

class MaintenanceMode(Exception):
    pass

def is_maintenance(page) -> bool:
    try:
        txt = (page.text_content("body") or "").lower()
        return ("maintenance mode is turned on" in txt) or ("undergoing maintenance" in txt)
    except Exception:
        return False

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Callable
import requests
import pandas as pd

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    load_dotenv = None  # optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


def getenv(name: str, default: str = "") -> str:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v


if load_dotenv:
    load_dotenv()

# --- Telegram connection test --------------------------------------------
import requests

def telegram_test():
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("⚠️  Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID in .env")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "✅ Telegram test passed — watcher starting."},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            print("⚠️  Telegram test failed:", data)
        else:
            print("✅ Telegram connection confirmed.")
    except Exception as e:
        print("⚠️  Telegram test error:", e)

telegram_test()
# --------------------------------------------------------------------------

ARBOR_BASE_URL = getenv("ARBOR_BASE_URL", "https://login.arbor.sc").rstrip("/")
ARBOR_EMAIL = getenv("ARBOR_EMAIL")
ARBOR_PASSWORD = getenv("ARBOR_PASSWORD")
ARBOR_CHILD_DOB = getenv("ARBOR_CHILD_DOB", "")
STATE_FILE = getenv("STATE_FILE", ".arbor_everything_state.json")
HEADFUL = getenv("HEADFUL", "").lower() in ("1", "true", "yes", "on")

EMAIL_TO = getenv("REPORT_EMAIL_TO", "")
EMAIL_FROM = getenv("REPORT_EMAIL_FROM", "")
SMTP_HOST = getenv("SMTP_HOST", "")
SMTP_PORT = int(getenv("SMTP_PORT", "587") or "587")
SMTP_TLS = getenv("SMTP_TLS", "true").lower() in ("1", "true", "yes", "on")
SMTP_USER = getenv("SMTP_USER", "") or None
SMTP_PASS = getenv("SMTP_PASS", "") or None
DISCORD_WEBHOOK = getenv("DISCORD_WEBHOOK", "")
TELEGRAM_TOKEN = getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = getenv("TELEGRAM_CHAT_ID", "")


# Utility helpers
def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# Notification helpers
def send_email(subject: str, body: str) -> None:
    if not (EMAIL_TO and EMAIL_FROM and SMTP_HOST):
        return
    from email.message import EmailMessage
    import smtplib

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(body)

    if SMTP_TLS:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        server.starttls()
    else:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)

    try:
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASS or "")
        server.send_message(msg)
    finally:
        server.quit()


def post_discord(text: str) -> None:
    if not DISCORD_WEBHOOK:
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": text}, timeout=15)
    except Exception as e:
        print(f"[warn] discord post failed: {e}", file=sys.stderr)


def post_telegram(text: str) -> None:
    token = TELEGRAM_TOKEN
    chat = TELEGRAM_CHAT_ID
    if not token or not chat:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat, "text": text[:4000], "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"[warn] telegram post failed: {e}", file=sys.stderr)


# Login and navigation
import re

def login_guardian(page) -> None:
    """
    Navigate to the guardian login, fill email/pass (and optional DOB),
    and land on the portal home. Handles .sc/.education and maintenance.
    """
    base = ARBOR_BASE_URL
    candidates = [
        base,
        f"{base}/auth/login",
        f"{base}/login",
    ]

    # 1) Reach a login page
    landed = False
    for url in candidates:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_load_state("networkidle")
            if is_maintenance(page):
                raise MaintenanceMode("Arbor shows maintenance page")
            landed = True
            break
        except MaintenanceMode:
            raise
        except Exception:
            continue
    if not landed:
        raise RuntimeError("Could not reach Arbor login page. Check ARBOR_BASE_URL.")

    # 2) Fill email (try several selectors)
    email_filled = False
    for kind, sel in [
        ("label", re.compile(r"email", re.I)),
        ("css", "input[type='email']"),
        ("css", "input[name='email']"),
        ("css", "input[autocomplete='username']"),
    ]:
        try:
            el = page.get_by_label(sel) if kind == "label" else page.locator(sel).first
            if el and el.is_visible():
                el.fill(os.getenv("ARBOR_EMAIL", ""))
                email_filled = True
                break
        except Exception:
            pass
    if not email_filled:
        raise RuntimeError("Could not find email field — confirm you’re on the correct login page.")

    # 3) Fill password
    for kind, sel in [
        ("label", re.compile(r"(password|passcode)", re.I)),
        ("css", "input[type='password']"),
        ("css", "input[name='password']"),
        ("css", "input[autocomplete='current-password']"),
    ]:
        try:
            el = page.get_by_label(sel) if kind == "label" else page.locator(sel).first
            if el and el.is_visible():
                el.fill(os.getenv("ARBOR_PASSWORD", ""))
                break
        except Exception:
            pass

    # 4) Submit
    try:
        page.get_by_role("button", name=re.compile(r"(log ?in|sign ?in|continue)", re.I)).click()
    except Exception:
        btn = page.locator("button[type='submit']").first
        if btn and btn.is_visible():
            btn.click()

    # 5) Optional DOB verification
    dob = os.getenv("ARBOR_CHILD_DOB", "")
    if dob:
        try:
            page.wait_for_timeout(600)
            dob_input = page.get_by_label(re.compile(r"(date of birth|dob)", re.I))
            if dob_input and dob_input.is_visible():
                dob_input.fill(dob)
                page.get_by_role("button", name=re.compile(r"(verify|continue|confirm)", re.I)).click()
        except Exception:
            pass

    page.wait_for_load_state("networkidle")
    if is_maintenance(page):
        raise MaintenanceMode("Arbor shows maintenance page")


@dataclass
class Item:
    section: str
    title: str
    meta: str
    when: str
    href: Optional[str] = None
    preview: Optional[str] = None


# Generic extractor for list-like pages
def extract_list_rows(container_locator, limit=25) -> List[Dict[str, Any]]:
    rows = container_locator.locator("li, div[role='listitem'], .ListItem, .card, .row").all()[:limit]
    out = []
    for r in rows:
        try:
            title = ""
            for sel in ("h3", "h4", ".title", ".Heading", "strong"):
                try:
                    el = r.locator(sel).first
                    if el and el.is_visible():
                        title = el.inner_text().strip()
                        break
                except Exception:
                    pass
            if not title:
                title = r.inner_text().split("\n")[0].strip()

            meta = ""
            when = ""
            preview = None
            sm = r.locator("small, .meta, .subtext, .subtitle").all()
            if sm:
                meta_text = " ".join(s.inner_text().strip() for s in sm[:2])
                parts = re.split(r"·|\||–|-{1,2}", meta_text)
                if parts:
                    meta = parts[0].strip()
                if len(parts) > 1:
                    when = parts[1].strip()

            try:
                preview_el = r.locator("p, .preview, .desc, .description").first
                if preview_el and preview_el.is_visible():
                    preview = preview_el.inner_text().strip()
            except Exception:
                pass

            href = None
            try:
                a = r.locator("a").first
                if a:
                    href = a.get_attribute("href")
            except Exception:
                pass

            out.append({"title": title, "meta": meta, "when": when, "href": href, "preview": preview})
        except Exception:
            continue
    return out


# Section scrapers
def section_messages(page) -> List[Item]:
    page.goto(f"{ARBOR_BASE_URL}/guardian#/messages", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_load_state("networkidle")
    rows = extract_list_rows(page.locator("main"))
    return [Item("Messages", r["title"], r["meta"], r["when"], r["href"], r["preview"]) for r in rows]


def section_comms(page) -> List[Item]:
    for path in ("/guardian#/communications", "/guardian#/comms", "/guardian#/communication-log"):
        try:
            page.goto(f"{ARBOR_BASE_URL}{path}", wait_until="domcontentloaded", timeout=25000)
            page.wait_for_load_state("networkidle")
            rows = extract_list_rows(page.locator("main"))
            if rows:
                return [Item("Communications", r["title"], r["meta"], r["when"], r["href"], r["preview"]) for r in rows]
        except Exception:
            continue
    return []


def section_notices(page) -> List[Item]:
    for path in ("/guardian#/noticeboard", "/guardian#/announcements", "/guardian#/news"):
        try:
            page.goto(f"{ARBOR_BASE_URL}{path}", wait_until="domcontentloaded", timeout=25000)
            page.wait_for_load_state("networkidle")
            rows = extract_list_rows(page.locator("main"))
            if rows:
                return [Item("Noticeboard", r["title"], r["meta"], r["when"], r["href"], r["preview"]) for r in rows]
        except Exception:
            continue
    return []


def section_calendar(page) -> List[Item]:
    for path in ("/guardian#/calendar", "/guardian#/events"):
        try:
            page.goto(f"{ARBOR_BASE_URL}{path}", wait_until="domcontentloaded", timeout=25000)
            page.wait_for_load_state("networkidle")
            rows = extract_list_rows(page.locator("main"))
            if rows:
                return [Item("Calendar", r["title"], r["meta"], r["when"], r["href"], r["preview"]) for r in rows]
        except Exception:
            continue
    return []


def section_trips(page) -> List[Item]:
    for path in ("/guardian#/trips", "/guardian#/activities"):
        try:
            page.goto(f"{ARBOR_BASE_URL}{path}", wait_until="domcontentloaded", timeout=25000)
            page.wait_for_load_state("networkidle")
            rows = extract_list_rows(page.locator("main"))
            if rows:
                return [Item("Trips", r["title"], r["meta"], r["when"], r["href"], r["preview"]) for r in rows]
        except Exception:
            continue
    return []


def section_payments(page) -> List[Item]:
    for path in ("/guardian#/payments", "/guardian#/accounts"):
        try:
            page.goto(f"{ARBOR_BASE_URL}{path}", wait_until="domcontentloaded", timeout=25000)
            page.wait_for_load_state("networkidle")
            rows = extract_list_rows(page.locator("main"))
            if rows:
                return [Item("Payments", r["title"], r["meta"], r["when"], r["href"], r["preview"]) for r in rows]
        except Exception:
            continue
    return []


def section_clubs(page) -> List[Item]:
    for path in ("/guardian#/clubs", "/guardian#/activities/clubs"):
        try:
            page.goto(f"{ARBOR_BASE_URL}{path}", wait_until="domcontentloaded", timeout=25000)
            page.wait_for_load_state("networkidle")
            rows = extract_list_rows(page.locator("main"))
            if rows:
                return [Item("Clubs", r["title"], r["meta"], r["when"], r["href"], r["preview"]) for r in rows]
        except Exception:
            continue
    return []


def section_documents(page) -> List[Item]:
    for path in ("/guardian#/documents", "/guardian#/report-cards", "/guardian#/letters"):
        try:
            page.goto(f"{ARBOR_BASE_URL}{path}", wait_until="domcontentloaded", timeout=25000)
            page.wait_for_load_state("networkidle")
            rows = extract_list_rows(page.locator("main"))
            if rows:
                return [Item("Documents", r["title"], r["meta"], r["when"], r["href"], r["preview"]) for r in rows]
        except Exception:
            continue
    return []


SECTIONS: List[Callable] = [
    section_messages,
    section_comms,
    section_notices,
    section_calendar,
    section_trips,
    section_payments,
    section_clubs,
    section_documents,
]


def build_digest(all_items: List[Item]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    lines = [f"Arbor digest at {now}"]
    if not all_items:
        lines.append("No items found.")
        return "\n".join(lines)

    by_section: Dict[str, List[Item]] = {}
    for it in all_items:
        by_section.setdefault(it.section, []).append(it)

    for section in sorted(by_section.keys()):
        lines.append("")
        lines.append(f"{section}:")
        for it in by_section[section][:10]:
            lines.append(f"• {it.title}")
            meta = it.meta.strip()
            when = it.when.strip()
            if meta or when:
                lines.append(f"  {meta}{(' · ' + when) if when else ''}".rstrip())
            if it.preview:
                pv = it.preview
                if len(pv) > 200:
                    pv = pv[:200].rstrip() + " …"
                lines.append(f"  {pv}")
            if it.href:
                lines.append(f"  Link: {it.href}")
    return "\n".join(lines).strip()


def main() -> int:
    if not (ARBOR_EMAIL and ARBOR_PASSWORD):
        print("Set ARBOR_EMAIL and ARBOR_PASSWORD in environment or .env", file=sys.stderr)
        return 2

    state = load_state(STATE_FILE)
    last = state.get("last", {})

    all_items: List[Item] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not HEADFUL)
        context = browser.new_context()
        page = context.new_page()
        try:
            login_guardian(page)
        except MaintenanceMode:
            # Optional: notify once per day, then exit 0 to avoid spam
            # (You can keep the daily-notice logic we discussed earlier)
            print("⚠️ Arbor is in maintenance mode. Will try again later.")
            return 0

        # Crawl sections
        for fn in SECTIONS:
            try:
                items = fn(page)
                all_items.extend(items)
            except Exception as e:
                print(f"[warn] section {fn.__name__} failed: {e}", file=sys.stderr)

        browser.close()

    if not all_items:
        print("No items gathered. The portal UI may have changed or access is restricted.")
        return 0

    # Compute per-section hashes on top entries
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
        return 0

    digest = build_digest(all_items)
    subject = "Arbor: new updates in " + ", ".join(sorted(changed_sections))

    if EMAIL_TO and EMAIL_FROM and SMTP_HOST:
        try:
            send_email(subject, digest)
        except Exception as e:
            print(f"[warn] email send failed: {e}", file=sys.stderr)

    post_discord(digest)
    post_telegram(digest)

    print(digest)

    state["last"] = current
    save_state(STATE_FILE, state)
    return 0


# ---------------------------------------------------------------------
# Entry point — ensures Telegram test runs only when you use --test
# ---------------------------------------------------------------------
if __name__ == "__main__":
    if "--test" in sys.argv:
        telegram_test()
        sys.exit(0)
    else:
        sys.exit(main())
# ---------------------------------------------------------------------