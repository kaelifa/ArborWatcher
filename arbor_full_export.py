#!/usr/bin/env python3
"""
arbor_full_export.py — One-off full export (server-guardian aware)

- Logs in via login_helper.login_guardian
- Forces entry to the Guardian dashboard (server routes: /?/guardians/…)
- Discovers sections from real links on your tenant and detects student-id
- Scrapes lists/cards or tables; optional rich debug dumps
- Creates per-section ZIPs when --zip is passed

Run:
  python3 -m pip install playwright python-dotenv requests pandas
  python3 -m playwright install
  python3 arbor_full_export.py --zip --fast --debug-dump
"""

# --- Stdlib
import os
import re
import json
import time
import zipfile
import argparse
import random
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
from login_helper import login_guardian

# ---------- Config ----------
if load_dotenv:
    load_dotenv()

ARBOR_EMAIL = os.getenv("ARBOR_EMAIL")
ARBOR_PASSWORD = os.getenv("ARBOR_PASSWORD")

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

def polite_goto(page: Page, url: str):
    polite_sleep()
    return polite_request_with_backoff(lambda: page.goto(url, wait_until="domcontentloaded", timeout=45000))

def polite_headers() -> dict:
    # Friendly, identifiable UA
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

# ---------- Core helpers ----------
def _origin(u: str) -> str:
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
    """Arbor returns a JSON body with 'It seems like you can't do this' for blocked routes."""
    try:
        body = (page.text_content("body") or "").lower()
    except Exception:
        body = ""
    if "it seems like you can't do this" in body:
        raise RuntimeError("🚫 Permission modal: staff-only or invalid route. Enter guardian shell first.")

def is_guardian_shell(page: Page) -> bool:
    # True for SPA or server guardian
    return bool(re.search(r"(guardian#|/\?\/guardians/)", page.url, re.I))

def click_first_guardian_link(page: Page) -> bool:
    """
    From current page, click the first link that goes to /?/guardians/… .
    Returns True if navigation happened and we look like we're in guardian shell.
    """
    try:
        hrefs = page.eval_on_selector_all(
            "a[href*='/?/guardians/']",
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
        ) or []
    except Exception:
        hrefs = []

    if not hrefs:
        # Try a couple of visible menu items
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

    # As a last resort, try generic server landing pages (some tenants redirect correctly)
    base = _origin(page.url)
    for guess in [
        "/?/guardians/session-ui/overview",
        "/?/guardians/customer-account-ui/active-payments",
    ]:
        try:
            url = f"{base}{guess}"
            polite_goto(page, url)
            page.wait_for_load_state("networkidle")
            if is_guardian_shell(page):
                return True
        except Exception:
            pass
    return is_guardian_shell(page)

def enter_guardian_or_retry(page: Page) -> None:
    ok = is_guardian_shell(page) or force_enter_guardian(page)
    try:
        assert_not_permission_modal(page)
    except RuntimeError:
        ok = force_enter_guardian(page) or ok
        assert_not_permission_modal(page)
    if not ok:
        # stay graceful; discovery may still find server links later
        pass

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

def goto(page: Page, base: str, path: str):
    url = path if path.startswith("http") else f"{base}{path}"
    polite_goto(page, url)
    page.wait_for_load_state("networkidle")
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

                meta, when, preview, href = "", "", None, None
                try:
                    smalls = el.locator("small, .meta, .subtext, .subtitle").all()
                    if smalls:
                        meta_text = " ".join((s.inner_text() or "").strip() for s in smalls[:2])
                        parts = re.split(r"·|\||–|-{1,2}", meta_text)
                        if parts: meta = parts[0].strip()
                        if len(parts) > 1: when = parts[1].strip()
                except Exception: pass
                try:
                    pv = el.locator("p, .preview, .desc, .description").first
                    if pv and pv.is_visible(): preview = (pv.inner_text() or "").strip()
                except Exception: pass
                try:
                    a = el.locator("a").first
                    if a: href = a.get_attribute("href")
                except Exception: pass

                rows.append(Item(section="", title=title, meta=meta, when=when, href=href, preview=preview))
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

def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip()[:180] or "item"

def rows_to_files(rows: List[Item], outdir: str, basename: str):
    data = [asdict(r) for r in rows]
    with open(os.path.join(outdir, f"{basename}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    pd.DataFrame(data).to_csv(os.path.join(outdir, f"{basename}.csv"), index=False)

# ---------- Discover sections from your tenant ----------
def discover_guardian_sections(page: Page, base: str, sid: Optional[str]) -> Dict[str, List[str]]:
    """
    Harvest server Guardian links from the current page and group them into known buckets.
    We only keep URLs under '/?/guardians/'. SPA routes are kept as fallbacks.
    """
    try:
        hrefs = page.eval_on_selector_all(
            "a[href*='/?/guardians/']",
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
        ) or []
    except Exception:
        hrefs = []

    norm, seen = [], set()
    for h in hrefs:
        if not h.startswith("http"):
            if not h.startswith("/"): h = "/" + h
            h = base + h
        if h in seen: continue
        seen.add(h)
        norm.append(h)

    buckets: Dict[str, List[str]] = {
        "Calendar": [],
        "Payments": [],
        "Behaviour": [],
        "Attendance": [],
        "Lessons (overview)": [],
        "Messages": [],
        "Communications": [],
        "Noticeboard": [],
        "Clubs": [],
        "Trips": [],
        "Documents": [],
    }

    def add(key: str, url_abs: str):
        rel = url_abs[len(base):] if url_abs.startswith(base) else url_abs
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

    # SPA fallbacks (kept but usually blocked on your tenant)
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

# ---------- Documents ----------
def download_documents(page: Page, base: str, outdir: str, paths: List[str]) -> List[Item]:
    docs = fetch_section(page, base, "Documents", paths)
    if not docs:
        return []
    docs_dir = os.path.join(outdir, "docs")
    os.makedirs(docs_dir, exist_ok=True)

    anchors = page.locator("a").all()
    for a in anchors:
        href = a.get_attribute("href") or ""
        text = (a.inner_text() or "").strip()
        if not href: continue
        looks_file = re.search(r"\.(pdf|docx?|xlsx?|csv|png|jpe?g)$", href, re.I) or ("download" in href.lower())
        if not looks_file: continue
        filename = sanitize(text or href.split("/")[-1])
        # Try Playwright download
        try:
            with page.expect_download(timeout=3500) as d_info:
                a.click(button="left")
            d = d_info.value
            d.save_as(os.path.join(docs_dir, filename))
            continue
        except Exception:
            pass
        # Fallback: same-origin GET with cookies
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
                with open(os.path.join(docs_dir, filename + ext), "wb") as f:
                    f.write(r.content)
        except Exception:
            pass
    return docs

# ---------- Export ----------
def export_all(zip_after: bool = False, headless: bool = False, debug_dump_flag: bool = False):
    if not (ARBOR_EMAIL and ARBOR_PASSWORD):
        raise SystemExit("Set ARBOR_EMAIL and ARBOR_PASSWORD in .env")

    ts = nowstamp()
    outdir = os.path.join("exports", ts)
    ensure_dir(outdir)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, slow_mo=0 if headless else 200)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        # Login & force-enter Guardian (server or SPA)
        login_guardian(page)
        force_enter_guardian(page)
        enter_guardian_or_retry(page)
        ensure_child_selected(page)

        base = _origin(page.url)
        sid = get_student_id(page)

        # If still no student id, try opening common server pages that often reveal it
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

        # Discover sections dynamically from your tenant
        sections = discover_guardian_sections(page, base, sid)

        results: Dict[str, List[Item]] = {}
        for name, paths in sections.items():
            if not paths:
                results[name] = []
                continue
            print(f"→ Exporting {name} …")
            items = fetch_section(page, base, name, paths)
            if debug_dump_flag:
                dd = os.path.join(outdir, "_debug_dump"); os.makedirs(dd, exist_ok=True)
                with open(os.path.join(dd, f"{name.lower().replace(' ','_')}.html"), "w", encoding="utf-8") as f:
                    f.write(page.content())
                try:
                    main_text = page.locator("main").inner_text()
                except Exception:
                    main_text = page.inner_text("body")
                with open(os.path.join(dd, f"{name.lower().replace(' ','_')}.MAIN.txt"), "w", encoding="utf-8") as f:
                    f.write(main_text or "")
                page.screenshot(path=os.path.join(dd, f"{name.lower().replace(' ','_')}.png"), full_page=True)
            results[name] = items
            polite_sleep()

        # Documents (download files too)
        print("→ Exporting Documents …")
        document_paths: List[str] = sections.get("Documents", [])
        docs = download_documents(page, base, outdir, document_paths)
        if debug_dump_flag:
            dd = os.path.join(outdir, "_debug_dump"); os.makedirs(dd, exist_ok=True)
            with open(os.path.join(dd, "documents.html"), "w", encoding="utf-8") as f:
                f.write(page.content())
            try:
                main_text = page.locator("main").inner_text()
            except Exception:
                main_text = page.inner_text("body")
            with open(os.path.join(dd, "documents.MAIN.txt"), "w", encoding="utf-8") as f:
                f.write(main_text or "")
            page.screenshot(path=os.path.join(dd, "documents.png"), full_page=True)
        results["Documents"] = docs

        browser.close()

    # Save data per section
    for sec, items in results.items():
        rows_to_files(items, outdir, sec.lower().replace(" ", "_"))

    # Manifest
    manifest = {
        "exported_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "base_url": base,
        "student_id": sid,
        "sections": {sec: len(items) for sec, items in results.items()},
    }
    with open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    # Per-section ZIPs
    if zip_after:
        section_files = {
            "messages": ["messages.json", "messages.csv"],
            "communications": ["communications.json", "communications.csv"],
            "noticeboard": ["noticeboard.json", "noticeboard.csv"],
            "calendar": ["calendar.json", "calendar.csv"],
            "trips": ["trips.json", "trips.csv"],
            "payments": ["payments.json", "payments.csv"],
            "clubs": ["clubs.json", "clubs.csv"],
            "lessons_(overview)": ["lessons_(overview).json", "lessons_(overview).csv"],
            "behaviour": ["behaviour.json", "behaviour.csv"],
            "attendance": ["attendance.json", "attendance.csv"],
            "documents": ["documents.json", "documents.csv"],
        }
        zipped_any = False
        for section, files in section_files.items():
            existing = [os.path.join(outdir, f) for f in files if os.path.exists(os.path.join(outdir, f))]
            docs_dir = os.path.join(outdir, "docs")
            if section == "documents" and os.path.isdir(docs_dir):
                for root, _, fs in os.walk(docs_dir):
                    for fn in fs:
                        existing.append(os.path.join(root, fn))
            if not existing:
                continue
            zip_path = os.path.join(outdir, f"{section}.zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                for full in existing:
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
    ap = argparse.ArgumentParser(description="One-off full export of Arbor Parent Portal (server-guardian aware)")
    ap.add_argument("--zip", action="store_true", help="Create a zip per populated section")
    ap.add_argument("--headless", action="store_true", help="Run headless (default headful)")
    ap.add_argument("--fast", action="store_true", help="Reduce delays for local runs")
    ap.add_argument("--debug-dump", action="store_true", help="Save HTML + MAIN text + screenshot per section")
    args = ap.parse_args()

    global MIN_DELAY, MAX_DELAY
    if args.fast:
        MIN_DELAY, MAX_DELAY = 0.2, 0.6

    export_all(zip_after=args.zip, headless=args.headless, debug_dump_flag=args.debug_dump)

if __name__ == "__main__":
    main()