#!/usr/bin/env python3
"""
monitor_arbor_portal.py — Polite watcher for Arbor Parent Portal (server-guardian aware)

- Logs in via login_helper.login_guardian
- Detects server Guardian shell (/?/guardians/…) as well as SPA (guardian#…)
- Auto-detects student-id and builds server routes (calendar/behaviour/attendance/payments)
- Waits for content to render; scrolls; scrapes list/cards or tables as fallback
- Compares compact hashes per section; sends Telegram digest only on change
- Flags:
    --headless      Run browser headless (default headful)
    --fast          Reduce delays (good for local dev)
    --debug-dump    Save HTML + MAIN text + screenshot per section
"""

# --- Stdlib
import os
import re
import json
import time
import hashlib
import argparse
import random
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

# --- Local login helper (your working one)
from login_helper import login_guardian

# ---------- Config ----------
if load_dotenv:
    load_dotenv()

ARBOR_EMAIL = os.getenv("ARBOR_EMAIL", "")
ARBOR_PASSWORD = os.getenv("ARBOR_PASSWORD", "")

STATE_FILE = os.getenv("STATE_FILE", ".arbor_everything_state.json")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Polite defaults (env override or --fast)
MIN_DELAY = float(os.getenv("ARBOR_MIN_DELAY", "1.2"))
MAX_DELAY = float(os.getenv("ARBOR_MAX_DELAY", "2.6"))

# ---------- Polite helpers ----------
def polite_sleep(min_s: Optional[float] = None, max_s: Optional[float] = None):
    lo = MIN_DELAY if min_s is None else min_s
    hi = MAX_DELAY if max_s is None else max_s
    if hi <= 0:
        return
    time.sleep(random.uniform(lo, hi))

def polite_request_with_backoff(fn, max_attempts=3, base_delay=2.0, max_delay=60.0):
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
    return {
        "User-Agent": "ArborWatcher/1.0 (+email@kristina.digital)",
        "X-ArborWatcher-Contact": "email@kristina.digital",
        "Accept-Language": "en-GB,en;q=0.9",
    }

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
def _origin(u: str) -> str:
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
    # Recognise SPA and server Guardian
    if re.search(r"(guardian#|/\?\/guardians/)", page.url, re.I):
        return True

    # Try common “Parent/Guardian portal” clickers from the landing dashboard
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
                    el.click(); page.wait_for_load_state("networkidle")
                    if re.search(r"(guardian#|/\?\/guardians/)", page.url, re.I): return True
            else:
                el = page.locator(arg).first
                if el and el.is_visible():
                    el.click(); page.wait_for_load_state("networkidle")
                    if re.search(r"(guardian#|/\?\/guardians/)", page.url, re.I): return True
        except Exception:
            pass

    # Fallback: any anchor with guardian in href
    try:
        hrefs = page.eval_on_selector_all(
            "a[href*='guardian']",
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
        ) or []
        if hrefs:
            target = hrefs[0]
            base = _origin(page.url)
            if not target.startswith("http"):
                if not target.startswith("/"): target = "/" + target
                target = base + target
            polite_goto(page, target); page.wait_for_load_state("networkidle")
            return bool(re.search(r"(guardian#|/\?\/guardians/)", page.url, re.I))
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
        pass  # stay graceful

def is_guardian_shell(page: Page) -> bool:
    # True for SPA or server guardian
    return bool(re.search(r"(guardian#|/\?\/guardians/)", page.url, re.I))

def click_first_guardian_link(page: Page) -> bool:
    """
    From the current page, click the first link that goes to /?/guardians/… .
    Returns True if navigation happened and we look like we're in guardian shell.
    """
    # 1) Try raw JS to harvest all matching anchors
    try:
        hrefs = page.eval_on_selector_all(
            "a[href*='/?/guardians/']",
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
        ) or []
    except Exception:
        hrefs = []

    # 2) Try a couple of visible menu items if no JS list
    if not hrefs:
        for text in ["Calendar", "Attendance", "Payments", "Behaviour", "Parent", "Guardian"]:
            try:
                a = page.locator(f"a:has-text('{text}')").first
                if a and a.is_visible():
                    href = a.get_attribute("href") or ""
                    if "/?/guardians/" in (href or ""):
                        hrefs = [href]
                        break
            except Exception:
                pass

    if not hrefs:
        return False

    target = hrefs[0]
    base = _origin(page.url)
    if not target.startswith("http"):
        if not target.startswith("/"):
            target = "/" + target
        target = base + target

    polite_goto(page, target)
    page.wait_for_load_state("networkidle")
    return is_guardian_shell(page)

def force_enter_guardian(page: Page) -> bool:
    """
    Ensure we're inside Guardian pages by clicking any /?/guardians/ link.
    """
    if is_guardian_shell(page):
        return True
    if click_first_guardian_link(page):
        return True

    # As a last resort, try some generic server landing pages (some tenants redirect correctly)
    base = _origin(page.url)
    for guess in [
        "/?/guardians/session-ui/overview",
        "/?/guardians/customer-account-ui/active-payments",
    ]:
        try:
            goto(page, base, guess)
            if is_guardian_shell(page):
                return True
        except Exception:
            pass
    return is_guardian_shell(page)

def ensure_child_selected(page: Page):
    """Click a visible student tile or picker if shown."""
    try:
        for css in [
            "[data-testid*='student-card' i]",
            ".student-card",
            "a:has-text('View Profile')",
            "button:has-text('Select')",
            "button:has-text('Continue')",
            "[role='button']:has-text('Select')",
        ]:
            el = page.locator(css).first
            if el and el.is_visible():
                el.click(); page.wait_for_load_state("networkidle"); break

        for css in [
            "button:has-text('Switch')",
            "button:has-text('Change child')",
            "[aria-haspopup='listbox']",
        ]:
            btn = page.locator(css).first
            if btn and btn.is_visible():
                btn.click(); page.wait_for_timeout(300)
                opt = page.locator("[role='option']").first
                if opt and opt.is_visible():
                    opt.click(); page.wait_for_load_state("networkidle"); break
    except Exception:
        pass

def get_student_id(page: Page) -> Optional[str]:
    """Find first student-id in any server guardian link on the page."""
    try:
        hrefs = page.eval_on_selector_all(
            "a[href*='/?/guardians/']",
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
        ) or []
    except Exception:
        hrefs = []
    for h in hrefs:
        m = re.search(r"student-id/(\d+)", h)
        if m:
            return m.group(1)
    try:
        html = page.content()
        m = re.search(r"student-id/(\d+)", html or "")
        if m:
            return m.group(1)
    except Exception:
        pass
    return None

def discover_guardian_sections(page: Page, base: str, sid: Optional[str]) -> Dict[str, List[str]]:
    """
    Harvest server Guardian links from the current page and group them into known buckets.
    We only keep URLs under '/?/guardians/'. SPA routes are kept as fallbacks.
    """
    # Collect all guardian links (visible or hidden)
    try:
        hrefs = page.eval_on_selector_all(
            "a[href*='/?/guardians/']",
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
        ) or []
    except Exception:
        hrefs = []

    # Normalise to absolute, unique
    norm, seen = [], set()
    for h in hrefs:
        if not h.startswith("http"):
            if not h.startswith("/"):
                h = "/" + h
            h = base + h
        if h in seen:
            continue
        seen.add(h)
        norm.append(h)

    buckets: Dict[str, List[str]] = {
        "Calendar": [],
        "Payments": [],
        "Behaviour": [],
        "Attendance": [],
        "Lessons (overview)": [],
        # We’ll keep SPA routes as fallbacks:
        "Messages": [],
        "Communications": [],
        "Noticeboard": [],
        "Clubs": [],
        "Trips": [],
        "Documents": [],
    }

    def add(key: str, url_abs: str):
        rel = url_abs
        if url_abs.startswith(base):
            rel = url_abs[len(base):]
        buckets[key].append(rel)

    for url in norm:
        u = url.lower()
        if "student-ui/calendar-event" in u or "student-ui/calendar" in u:
            add("Calendar", url)
        elif "customer-account-ui" in u or "active-payments" in u:
            add("Payments", url)
        elif "behaviour-ui" in u or "student-behaviour" in u:
            add("Behaviour", url)
        elif "recent-attendance" in u:
            add("Attendance", url)
        elif "/session-ui/overview" in u:
            add("Lessons (overview)", url)
        elif "documents" in u or "report-cards" in u or "letters" in u:
            add("Documents", url)
        elif "/clubs" in u:
            add("Clubs", url)
        elif "/trips" in u or "/activities" in u:
            add("Trips", url)

    # SPA fallbacks
    buckets["Messages"]       += ["/guardian#/messages"]
    buckets["Communications"] += ["/guardian#/communications", "/guardian#/comms", "/guardian#/communication-log"]
    buckets["Noticeboard"]    += ["/guardian#/noticeboard", "/guardian#/announcements", "/guardian#/news"]
    buckets["Clubs"]          += ["/guardian#/clubs", "/guardian#/activities/clubs"]
    buckets["Trips"]          += ["/guardian#/trips", "/guardian#/activities"]
    buckets["Documents"]      += ["/guardian#/documents", "/guardian#/report-cards", "/guardian#/letters"]

    # De-dupe each list
    for k, v in buckets.items():
        uniq, s = [], set()
        for p in v:
            if p in s: 
                continue
            s.add(p)
            uniq.append(p)
        buckets[k] = uniq

    # If we have sid but didn’t discover common pages, synthesise them
    if sid:
        if not buckets["Calendar"]:
            add("Calendar", f"{base}/?/guardians/student-ui/calendar/student-id/{sid}")
        if not buckets["Attendance"]:
            add("Attendance", f"{base}/?/guardians/student-ui/recent-attendance/student-id/{sid}")
        if not buckets["Behaviour"]:
            add("Behaviour", f"{base}/?/guardians/behaviour-ui/student-behaviour/student-id/{sid}")
        if not buckets["Documents"]:
            for guess in [
                f"{base}/?/guardians/student-ui/documents/student-id/{sid}",
                f"{base}/?/guardians/student-ui/report-cards/student-id/{sid}",
                f"{base}/?/guardians/student-ui/letters/student-id/{sid}",
            ]:
                add("Documents", guess)

    return buckets

def goto(page: Page, base: str, path: str):
    url = f"{base}{path}"
    polite_goto(page, url); page.wait_for_load_state("networkidle")
    assert_not_permission_modal(page)

def wait_for_guardian_ready(page: Page, timeout_ms: int = 12000) -> bool:
    selectors = [
        "main li", "main [role='listitem']", "main .ListItem", "main .card", "main .row",
        ":light(main li)", ":light(main [role='listitem'])", ":light(main .ListItem)", ":light(main .card)", ":light(main .row)",
        "text=/no (messages|items|events|results)/i",
        "table", "[role='table']",
    ]
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        for sel in selectors:
            try:
                if page.locator(sel).first.is_visible():
                    return True
            except Exception:
                pass
        page.wait_for_timeout(250)
    return False

def lazy_scroll_all(page: Page, container: Optional[str] = None, max_passes: int = 40, pause: float = 0.6):
    last_h = -1
    for _ in range(max_passes):
        if container:
            page.locator(container).evaluate("(el) => el.scrollTo(0, el.scrollHeight)")
        else:
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(pause)
        h = page.evaluate("() => document.body.scrollHeight")
        if h == last_h: break
        last_h = h

def collect_items(page: Page, limit: Optional[int] = None) -> List[Item]:
    rows: List[Item] = []
    # Cards / lists (pierce shadow DOM too)
    candidates = [
        "main li, main div[role='listitem'], main .ListItem, main .card, main .row",
        ":light(main li), :light(main div[role='listitem']), :light(main .ListItem), :light(main .card), :light(main .row)",
    ]
    for cand in candidates:
        for el in page.locator(cand).all():
            try:
                title = ""
                for sel in ("h1","h2","h3","h4",".title",".Heading","strong"):
                    try:
                        t = el.locator(sel).first
                        if t and t.is_visible():
                            title = (t.inner_text() or "").strip()
                            if title: break
                    except Exception: pass
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
                except Exception: pass

                rows.append(Item(section="", title=title, meta=meta, when=when))
                if limit and len(rows) >= limit: return rows
            except Exception:
                continue
        if rows: break

    # Table fallback
    if not rows:
        try:
            tables = page.locator("table, [role='table']").all()
            for t in tables:
                trs = t.locator("tr").all()
                for tr in trs:
                    tds = [(c.inner_text() or "").strip() for c in tr.locator("th,td").all()]
                    if not tds: continue
                    title = tds[0][:180]
                    meta = " | ".join(tds[1:3]) if len(tds) > 1 else ""
                    when = tds[-1] if len(tds) >= 2 else ""
                    rows.append(Item(section="", title=title, meta=meta, when=when))
                    if limit and len(rows) >= limit: break
                if rows: break
        except Exception:
            pass
    return rows

def fetch_section(page: Page, base: str, section: str, paths: List[str]) -> List[Item]:
    for path in paths:
        try:
            goto(page, base, path)
            wait_for_guardian_ready(page, timeout_ms=12000)
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
    if not os.path.exists(path): return {}
    with open(path, "r", encoding="utf-8") as f: return json.load(f)

def save_state(path: str, data: Dict) -> None:
    with open(path, "w", encoding="utf-8") as f: json.dump(data, f, indent=2)

def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def digest_sha(text: str) -> str:
    # Normalise whitespace to avoid tiny differences triggering new sends
    norm = "\n".join([line.rstrip() for line in text.strip().splitlines() if line.strip()]) + "\n"
    return sha(norm)

def should_send_digest(state: Dict, digest: str, min_interval_minutes: int = 30) -> bool:
    """Return True if digest is new or sufficiently old since last identical send."""
    try:
        from datetime import datetime, timedelta
        last_sha = state.get("last_digest_sha")
        last_at = state.get("last_digest_at")
        this_sha = digest_sha(digest)
        if last_sha == this_sha and last_at:
            t = datetime.fromisoformat(last_at)
            if datetime.now() - t < timedelta(minutes=min_interval_minutes):
                # Duplicate within interval, skip sending
                return False
        # store for later
        state["last_digest_sha"] = this_sha
        state["last_digest_at"] = datetime.now().isoformat(timespec="seconds")
        return True
    except Exception:
        # On any error, be safe and send
        return True
def build_digest(all_items: List[Item]) -> str:
    if not all_items: return "No new items."
    by_section: Dict[str, List[Item]] = {}
    for it in all_items: by_section.setdefault(it.section, []).append(it)
    lines = ["Arbor updates:"]
    for sec in sorted(by_section):
        lines.append(f"• {sec}")
        for i in by_section[sec][:8]:
            suffix = f" — {i.when}" if i.when else ""
            meta = f" ({i.meta})" if i.meta else ""
            lines.append(f"  - {i.title}{meta}{suffix}")
    return "\n".join(lines)

# ---------- Debug ----------
def debug_dump(page: Page, outdir: str, name: str):
    try:
        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, f"{name}.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
        try:
            main_text = page.locator("main").inner_text()
        except Exception:
            main_text = page.inner_text("body")
        with open(os.path.join(outdir, f"{name}.MAIN.txt"), "w", encoding="utf-8") as f:
            f.write(main_text or "")
        page.screenshot(path=os.path.join(outdir, f"{name}.png"), full_page=True)
    except Exception:
        pass

# ---------- Notifications ----------
def post_telegram(text: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID): return
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        polite_requests_post(api, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True})
    except Exception as e:
        print(f"[warn] telegram failed: {e}")

# ---------- Main ----------
def main() -> int:
    parser = argparse.ArgumentParser(description="Polite watcher for Arbor Parent Portal (Telegram alerts)")
    parser.add_argument("--headless", action="store_true", help="Run headless (default headful)")
    parser.add_argument("--fast", action="store_true", help="Reduce delays for local runs")
    parser.add_argument("--debug-dump", action="store_true", help="Save HTML + MAIN text + screenshot per section")
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

        # Login & enter Guardian (server or SPA)
        login_guardian(page)

        # NEW: force-enter Guardian shell
        force_enter_guardian(page)
        enter_guardian_or_retry(page)   # keep your existing safety check
        ensure_child_selected(page)     # keep (no harm if not shown)

        base = _origin(page.url)
        sid  = get_student_id(page)

        # If still no student id, try opening common server pages that reveal it
        if not sid:
            for probe in ["/?/guardians/student-ui/calendar",
                        "/?/guardians/student-ui/recent-attendance",
                        "/?/guardians/behaviour-ui/student-behaviour"]:
                try:
                    goto(page, base, probe)
                    sid = get_student_id(page)
                    if sid:
                        break
                except Exception:
                    pass

        print("🔗 Using origin:", base, " student-id:", sid)

        # Build sections using **server routes** first, with SPA fallbacks
        sections = discover_guardian_sections(page, base, sid)

        for sec, paths in sections.items():
            if not paths:
                continue
            print(f"→ Checking {sec} …")
            try:
                items = fetch_section(page, base, sec, paths)
                if args.debug_dump:
                    debug_dump(page, "_debug_dump", sec.lower().replace(" ", "_"))
                all_items.extend(items)
            except Exception as e:
                print(f"[warn] section {sec} failed: {e}")
            polite_sleep()

        # Documents (server guesses + SPA fallbacks)
        print("→ Checking Documents …")
        document_paths: List[str] = []
        if sid:
            document_paths += [
                f"/?/guardians/student-ui/documents/student-id/{sid}",
                f"/?/guardians/student-ui/report-cards/student-id/{sid}",
                f"/?/guardians/student-ui/letters/student-id/{sid}",
            ]
        document_paths += ["/guardian#/documents", "/guardian#/report-cards", "/guardian#/letters"]
        try:
            items = fetch_section(page, base, "Documents", document_paths)
            if args.debug_dump:
                debug_dump(page, "_debug_dump", "documents")
            all_items.extend(items)
        except Exception as e:
            print(f"[warn] section Documents failed: {e}")

        browser.close()

    if not all_items:
        print("No items gathered. Portal UI may have changed or nothing is visible for this account.")
        return 0

    # Hash comparison per section
    current: Dict[str, str] = {}
    by_section: Dict[str, List[Item]] = {}
    for it in all_items: by_section.setdefault(it.section, []).append(it)

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
    if should_send_digest(state, digest):
        post_telegram(digest)
    else:
        print('[info] Duplicate digest suppressed')

    state["last"] = current
    save_state(STATE_FILE, state)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())