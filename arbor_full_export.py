#!/usr/bin/env python3
"""
Arbor Parent Portal — ONE-OFF FULL EXPORT

What it does (one run):
- Logs into guardian portal
- Crawls key sections and collects ALL rows (by scrolling/pagination)
- Saves JSON + CSV per section under exports/<timestamp>/
- Downloads available documents/attachments

Sections (best-effort):
- Messages (threads list with latest preview)
- Communications (emails/SMS/letters listed in portal)
- Noticeboard / Announcements
- Calendar / Events (full list shown)
- Trips / Activities
- Payments (summary rows)
- Clubs
- Documents (downloads files where possible)

Run:
  python -m pip install playwright python-dotenv requests pandas
  python -m playwright install
  python arbor_full_export.py --zip

.env needed:
  ARBOR_BASE_URL=https://the-castle-school.uk.arbor.sc
  ARBOR_EMAIL=you@example.com
  ARBOR_PASSWORD=your_password
  # Optional
  ARBOR_CHILD_DOB=01/02/2014
"""

import os
import re
import csv
import json
import time
import zipfile
import argparse
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Dict, Any, Optional

import pandas as pd
import requests

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    load_dotenv = None

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page

# ---------- Config ----------
if load_dotenv:
    load_dotenv()

BASE = os.getenv("ARBOR_BASE_URL", "https://login.arbor.sc").rstrip("/")
EMAIL = os.getenv("ARBOR_EMAIL")
PASSWORD = os.getenv("ARBOR_PASSWORD")
DOB = os.getenv("ARBOR_CHILD_DOB", "")

# ---------- Helpers ----------
def nowstamp():
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip()[:180] or "item"

@dataclass
class Row:
    section: str
    title: str
    meta: str
    when: str
    href: Optional[str] = None
    preview: Optional[str] = None

def rows_to_files(rows: List[Row], outdir: str, basename: str):
    data = [asdict(r) for r in rows]
    with open(os.path.join(outdir, f"{basename}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    pd.DataFrame(data).to_csv(os.path.join(outdir, f"{basename}.csv"), index=False)

def login_guardian(page: Page):
    page.goto(BASE, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_load_state("networkidle")
    page.get_by_label(re.compile("Email", re.I)).fill(EMAIL)
    page.get_by_label(re.compile("Password|Passcode", re.I)).fill(PASSWORD)
    page.get_by_role("button", name=re.compile("Log ?in|Sign ?in|Continue", re.I)).click()
    page.wait_for_load_state("networkidle")
    # DOB prompt occasionally
    try:
        if DOB:
            dob_input = page.get_by_label(re.compile("Date of birth|DOB", re.I))
            if dob_input and dob_input.is_visible():
                dob_input.fill(DOB)
                page.get_by_role("button", name=re.compile("Verify|Continue|Confirm", re.I)).click()
                page.wait_for_load_state("networkidle")
    except PWTimeout:
        pass

def lazy_scroll_all(page: Page, scroll_container_selector: Optional[str] = None, max_passes: int = 40):
    """
    Scrolls the page or a container to load lazy lists.
    """
    last_height = -1
    passes = 0
    while passes < max_passes:
        if scroll_container_selector:
            page.locator(scroll_container_selector).evaluate("(el) => el.scrollTo(0, el.scrollHeight)")
        else:
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(0.6)
        h = page.evaluate("() => document.body.scrollHeight")
        if h == last_height:
            break
        last_height = h
        passes += 1

def collect_rows_from_current(page: Page, limit: Optional[int] = None) -> List[Row]:
    rows = []
    cards = page.locator("main").locator("li, div[role='listitem'], .ListItem, .card, .row").all()
    for el in cards:
        try:
            title = ""
            for sel in ("h1", "h2", "h3", "h4", ".title", ".Heading", "strong"):
                try:
                    t = el.locator(sel).first
                    if t and t.is_visible():
                        title = t.inner_text().strip()
                        if title:
                            break
                except Exception:
                    pass
            if not title:
                text = el.inner_text().strip()
                title = text.split("\n")[0][:180] if text else "(untitled)"

            meta = ""
            when = ""
            preview = None

            smalls = el.locator("small, .meta, .subtext, .subtitle").all()
            if smalls:
                meta_text = " ".join(s.inner_text().strip() for s in smalls[:2])
                parts = re.split(r"·|\||–|-{1,2}", meta_text)
                if parts:
                    meta = parts[0].strip()
                if len(parts) > 1:
                    when = parts[1].strip()

            try:
                pv = el.locator("p, .preview, .desc, .description").first
                if pv and pv.is_visible():
                    preview = pv.inner_text().strip()
            except Exception:
                pass

            href = None
            try:
                a = el.locator("a").first
                if a:
                    href = a.get_attribute("href")
            except Exception:
                pass

            rows.append(Row(section="", title=title, meta=meta, when=when, href=href, preview=preview))
            if limit and len(rows) >= limit:
                break
        except Exception:
            continue
    return rows

def goto(page: Page, path: str):
    page.goto(f"{BASE}{path}", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_load_state("networkidle")

def fetch_section(page: Page, section: str, paths: List[str]) -> List[Row]:
    for path in paths:
        try:
            goto(page, path)
            lazy_scroll_all(page)
            rows = collect_rows_from_current(page)
            if rows:
                for r in rows:
                    r.section = section
                return rows
        except Exception:
            continue
    return []

def download_documents(page: Page, outdir: str) -> List[Row]:
    """
    On Documents section, try to click/download any file links.
    Saves files into outdir/docs/.
    """
    docs_rows = fetch_section(page, "Documents",
        ["/guardian#/documents", "/guardian#/report-cards", "/guardian#/letters"]
    )
    if not docs_rows:
        return []

    ensure_dir(os.path.join(outdir, "docs"))

    # Click any anchors that look like downloads; attempt using Playwright's download handler
    anchors = page.locator("a").all()
    for a in anchors:
        href = a.get_attribute("href") or ""
        text = (a.inner_text() or "").strip()
        if not href:
            continue
        # Heuristics: links that lead to a file or a 'download' endpoint
        if re.search(r"\.(pdf|docx?|xlsx?|csv|png|jpg|jpeg)$", href, re.I) or "download" in href.lower():
            filename = sanitize(text or href.split("/")[-1])
            try:
                with page.expect_download(timeout=5000) as d_info:
                    a.click(button="left")
                d = d_info.value
                d.save_as(os.path.join(outdir, "docs", filename or "document"))
            except Exception:
                # fallback: try direct request using cookies (same-origin only)
                try:
                    cookies = page.context.cookies()
                    jar = requests.cookies.RequestsCookieJar()
                    for c in cookies:
                        # only attach cookies for same site
                        jar.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path"))
                    url = href if href.startswith("http") else BASE.rstrip("/") + "/" + href.lstrip("/")
                    r = requests.get(url, cookies=jar, timeout=20)
                    if r.ok and r.content:
                        # try infer extension
                        ext = ""
                        ct = r.headers.get("content-type", "")
                        if "pdf" in ct: ext = ".pdf"
                        elif "msword" in ct or "doc" in ct: ext = ".doc"
                        elif "excel" in ct or "sheet" in ct: ext = ".xlsx"
                        elif "png" in ct: ext = ".png"
                        elif "jpeg" in ct or "jpg" in ct: ext = ".jpg"
                        with open(os.path.join(outdir, "docs", filename + ext), "wb") as f:
                            f.write(r.content)
                except Exception:
                    pass

    return docs_rows

def export_all(zip_after: bool):
    ts = nowstamp()
    outdir = os.path.join("exports", ts)
    ensure_dir(outdir)

    if not (EMAIL and PASSWORD):
        raise SystemExit("Set ARBOR_EMAIL and ARBOR_PASSWORD in .env")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, slow_mo=200)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        login_guardian(page)  # <-- add this

        # Sections
        messages = fetch_section(page, "Messages", ["/guardian#/messages"])
        comms = fetch_section(page, "Communications",
                              ["/guardian#/communications", "/guardian#/comms", "/guardian#/communication-log"])
        notices = fetch_section(page, "Noticeboard",
                                ["/guardian#/noticeboard", "/guardian#/announcements", "/guardian#/news"])
        calendar = fetch_section(page, "Calendar", ["/guardian#/calendar", "/guardian#/events"])
        trips = fetch_section(page, "Trips", ["/guardian#/trips", "/guardian#/activities"])
        payments = fetch_section(page, "Payments", ["/guardian#/payments", "/guardian#/accounts"])
        clubs = fetch_section(page, "Clubs", ["/guardian#/clubs", "/guardian#/activities/clubs"])

        # Documents (also tries to download files)
        docs = []
        try:
            goto(page, "/guardian#/documents")
            lazy_scroll_all(page)
            docs = download_documents(page, outdir)
        except Exception:
            # try alternates if direct documents path failed
            try:
                docs = download_documents(page, outdir)
            except Exception:
                pass

        browser.close()

    # Save each section
    rows_to_files(messages, outdir, "messages")
    rows_to_files(comms, outdir, "communications")
    rows_to_files(notices, outdir, "noticeboard")
    rows_to_files(calendar, outdir, "calendar")
    rows_to_files(trips, outdir, "trips")
    rows_to_files(payments, outdir, "payments")
    rows_to_files(clubs, outdir, "clubs")
    rows_to_files(docs, outdir, "documents")

    # Write a small manifest
    manifest = {
        "exported_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "base_url": BASE,
        "sections": {
            "messages": len(messages),
            "communications": len(comms),
            "noticeboard": len(notices),
            "calendar": len(calendar),
            "trips": len(trips),
            "payments": len(payments),
            "clubs": len(clubs),
            "documents_listed": len(docs)
        }
    }
    with open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    if zip_after:
        zip_path = outdir + ".zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(outdir):
                for file in files:
                    full = os.path.join(root, file)
                    arc = os.path.relpath(full, start=os.path.dirname(outdir))
                    z.write(full, arc)
        print(f"Export complete: {outdir}  (ZIP: {zip_path})")
    else:
        print(f"Export complete: {outdir}")

def main():
    ap = argparse.ArgumentParser(description="One-off full export of Arbor Parent Portal.")
    ap.add_argument("--zip", action="store_true", help="Zip the export folder when done.")
    args = ap.parse_args()
    export_all(zip_after=args.zip)

if __name__ == "__main__":
    main()