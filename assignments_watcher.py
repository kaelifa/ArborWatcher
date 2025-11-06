#!/usr/bin/env python3
"""
assignments_watcher.py — Telegram alerts for Guardian Consultations + Assignments

Watches these blocks on the guardian dashboard:
- Guardian Consultations
- Overdue Assignments
- Assignments that are due
- Submitted Assignments

Sends a Telegram digest if the top items change.

Env (.env)
----------
ARBOR_BASE_URL (optional) e.g. https://the-castle-school.uk.arbor.education
ARBOR_EMAIL
ARBOR_PASSWORD
TELEGRAM_TOKEN
TELEGRAM_CHAT_ID
STATE_FILE=.arbor_assignments_state.json   (optional)
ARBOR_MIN_DELAY=1.2  (optional)
ARBOR_MAX_DELAY=2.6  (optional)

Run
---
python3 -m pip install playwright python-dotenv requests
python3 -m playwright install
python3 assignments_watcher.py --fast
"""

from __future__ import annotations

import os, re, json, time, random, argparse, hashlib
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright, Page

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

# ---- env
if load_dotenv:
    load_dotenv()

ARBOR_BASE_URL = os.getenv("ARBOR_BASE_URL", "").rstrip("/")
ARBOR_EMAIL = os.getenv("ARBOR_EMAIL", "")
ARBOR_PASSWORD = os.getenv("ARBOR_PASSWORD", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

STATE_FILE = os.getenv("STATE_FILE", ".arbor_assignments_state.json")
MIN_DELAY = float(os.getenv("ARBOR_MIN_DELAY", "1.2"))
MAX_DELAY = float(os.getenv("ARBOR_MAX_DELAY", "2.6"))
COOLDOWN_MINUTES = 60  # dedupe identical digest spam

# use your working login
from login_helper import login_guardian  # noqa: E402

# ---- polite helpers
def polite_sleep(lo: Optional[float] = None, hi: Optional[float] = None):
    lo = MIN_DELAY if lo is None else lo
    hi = MAX_DELAY if hi is None else hi
    if hi <= 0:
        return
    time.sleep(random.uniform(lo, hi))

def _origin(u: str) -> str:
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}"

def polite_goto(page: Page, url: str):
    polite_sleep()
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_load_state("networkidle")

def send_telegram(text: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("[warn] Telegram not configured; skipping send.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = requests.post(url, data=data, timeout=20)
        if not r.ok:
            print(f"[warn] Telegram send failed: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[warn] Telegram send error: {e}")

def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "ignore")).hexdigest()[:16]

def load_state() -> Dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(st: Dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2)
    except Exception:
        pass

# ---- guardian helpers
SECTION_HEADINGS = {
    "consultations": re.compile(r"guardian consultations", re.I),
    "overdue":       re.compile(r"overdue assignments", re.I),
    "due":           re.compile(r"assignments that are due", re.I),
    "submitted":     re.compile(r"submitted assignments", re.I),
}

def ensure_guardian_context(page: Page) -> None:
    """Make sure we land on the guardian dashboard that shows those blocks."""
    base = _origin(page.url)
    # Try the overview first
    for path in [
        "/?/guardians/session-ui/overview",
        "/?/guardians/customer-account-ui/active-payments",  # tends to exist, then we can nav back
    ]:
        try:
            polite_goto(page, base + path)
            # If we can see any of the headings, we're good
            if find_any_heading(page):
                return
        except Exception:
            pass
    # If nothing obvious, just stay where we are—the extractor will try to read blocks anyway.

def find_any_heading(page: Page) -> bool:
    try:
        for sel in ("h1,h2,h3,h4,.heading,.Heading,strong,legend", ":light(h1),:light(h2),:light(h3)"):
            for key, rx in SECTION_HEADINGS.items():
                if page.locator(f"{sel}:has-text('{rx.pattern}')").first.is_visible():
                    return True
    except Exception:
        pass
    # fallback text search
    try:
        txt = (page.inner_text("body") or "").lower()
        return any(rx.search(txt) for rx in SECTION_HEADINGS.values())
    except Exception:
        return False

def extract_section_items(page: Page, heading_rx: re.Pattern) -> List[Dict[str, str]]:
    """
    Given a heading regex, find the block and pull list rows.
    Handles a few DOM shapes (list of <li>, plain <a> rows, or table rows).
    """
    items: List[Dict[str, str]] = []

    # 1) Find the heading element
    heading = None
    for sel in [
        "h1,h2,h3,h4,legend,.heading,.Heading,strong",
        ":light(h1),:light(h2),:light(h3),:light(legend)",
    ]:
        try:
            cand = page.locator(sel).filter(has_text=heading_rx).first
            if cand and cand.is_visible():
                heading = cand
                break
        except Exception:
            pass

    # 2) From the heading, look at the container that follows
    roots: List = []
    try:
        if heading:
            parent = heading.locator("xpath=..")
            for _ in range(4):  # walk up a few times to find a container
                if parent and parent.count() > 0:
                    roots.append(parent)
                    parent = parent.locator("xpath=..")
                else:
                    break
    except Exception:
        pass

    if not roots:
        roots = [page.locator("main")]

    # 3) Within those roots, grab rows
    for root in roots:
        try:
            # Lists of links
            for el in root.locator("li, .row, .card, a").all():
                try:
                    txt = (el.inner_text() or "").strip()
                    if not txt:
                        continue
                    # Simple heuristic: first line looks like the title; rest carry status/due
                    lines = [ln.strip() for ln in txt.split("\n") if ln.strip()]
                    if not lines:
                        continue
                    title = lines[0][:180]
                    rest = " ".join(lines[1:3])[:120]
                    # Prefer lines that look like assignments: look for "(Due ...)" or datey text
                    if heading_rx.pattern.lower().startswith("overdue"):
                        if "(due" not in txt.lower() and "late" not in txt.lower():
                            continue
                    items.append({"title": title, "meta": rest})
                    if len(items) >= 15:
                        return items
                except Exception:
                    continue

            # Table fallback
            if not items:
                tbls = root.locator("table,[role='table']").all()
                for t in tbls:
                    for tr in t.locator("tr").all():
                        tds = [(c.inner_text() or "").strip() for c in tr.locator("th,td").all()]
                        if not tds:
                            continue
                        title = tds[0][:180]
                        meta = " | ".join(tds[1:3])[:120] if len(tds) > 1 else ""
                        items.append({"title": title, "meta": meta})
                        if len(items) >= 15:
                            return items
        except Exception:
            continue

    return items

def extract_all(page: Page) -> Dict[str, List[Dict[str, str]]]:
    """
    Extract the four target sections from the current page.
    """
    data: Dict[str, List[Dict[str, str]]] = {}
    for key, rx in SECTION_HEADINGS.items():
        data[key] = extract_section_items(page, rx)
    return data

def build_digest(changes: Dict[str, List[Dict[str, str]]]) -> str:
    lines = ["<b>Arbor — updates</b>", ""]
    order = ["consultations", "overdue", "due", "submitted"]
    titles = {
        "consultations": "Guardian Consultations",
        "overdue": "Overdue Assignments",
        "due": "Assignments that are due",
        "submitted": "Submitted Assignments",
    }
    for sec in order:
        items = changes.get(sec, [])
        if not items:
            continue
        lines.append(f"<b>{titles[sec]}</b>")
        for it in items[:5]:
            meta = f" — {it.get('meta','')}" if it.get("meta") else ""
            lines.append(f"• {it.get('title','')}{meta}")
        if len(items) > 5:
            lines.append(f"… and {len(items) - 5} more")
        lines.append("")
    return "\n".join(lines).strip()

# ---- main
def main():
    ap = argparse.ArgumentParser(description="Telegram alerts for Guardian Consultations + Assignments")
    ap.add_argument("--headless", action="store_true", help="Run headless")
    ap.add_argument("--fast", action="store_true", help="Reduce polite delays")
    args = ap.parse_args()

    global MIN_DELAY, MAX_DELAY
    if args.fast:
        MIN_DELAY, MAX_DELAY = 0.2, 0.6

    if not (ARBOR_EMAIL and ARBOR_PASSWORD):
        raise SystemExit("Set ARBOR_EMAIL and ARBOR_PASSWORD in .env")

    state = load_state()
    last_hash = state.get("last_hash")
    last_sent_hash = state.get("last_sent_hash")
    last_sent_ts = state.get("last_sent_ts", 0)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless, slow_mo=0 if args.headless else 150)
        context = browser.new_context()
        page = context.new_page()

        # login
        login_guardian(page)

        # try to land on the dashboard that contains these blocks
        ensure_guardian_context(page)

        # extract
        data = extract_all(page)
        browser.close()

    # Build a deterministic basis string
    order = ["consultations", "overdue", "due", "submitted"]
    lines: List[str] = []
    for sec in order:
        items = data.get(sec, [])
        for it in items[:10]:
            lines.append(f"{sec}|{it.get('title','')}|{it.get('meta','')}")
    basis = "\n".join(lines)
    cur_hash = sha(basis)

    # no change
    if last_hash == cur_hash:
        print("No change.")
        return 0

    # change detected — craft a digest of only sections that have content
    changed_sections = {k: v for k, v in data.items() if v}
    if not changed_sections:
        # content changed from something to nothing or vice versa. Still notify once.
        changed_sections = data

    digest = build_digest(changed_sections)
    now_ts = int(time.time())

    # cooldown identical-digest spam
    if last_sent_hash == cur_hash and (now_ts - last_sent_ts) < COOLDOWN_MINUTES * 60:
        print("Change detected matches last sent digest but still within cooldown; not sending.")
    else:
        send_telegram(digest)
        state["last_sent_hash"] = cur_hash
        state["last_sent_ts"] = now_ts

    state["last_hash"] = cur_hash
    save_state(state)
    print(digest)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())