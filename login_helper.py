# login_helper.py
from dotenv import load_dotenv; load_dotenv()
import os, re
from playwright.sync_api import TimeoutError as PWTimeout

# Use the domain that actually worked for you (.education)
ARBOR_BASE_URL   = os.getenv("ARBOR_BASE_URL", "https://the-castle-school.uk.arbor.education").rstrip("/")
ARBOR_EMAIL      = os.getenv("ARBOR_EMAIL", "")
ARBOR_PASSWORD   = os.getenv("ARBOR_PASSWORD", "")
ARBOR_CHILD_DOB  = os.getenv("ARBOR_CHILD_DOB", "")
ARBOR_LOGIN_METHOD = os.getenv("ARBOR_LOGIN_METHOD", "email").lower()

def _click_if_visible(p, *, role=None, name=None, css=None, timeout=2000):
    try:
        if css:
            el = p.locator(css).first
            if el and el.is_visible():
                el.click(timeout=timeout)
                return True
        if role and name:
            p.get_by_role(role, name=name).click(timeout=timeout)
            return True
    except Exception:
        pass
    return False

def _accept_cookies(p):
    for t in ("Accept", "Agree", "Allow all", "I agree", "Got it"):
        if _click_if_visible(p, role="button", name=re.compile(t, re.I), timeout=1200):
            return True
    try:
        if p.locator("text=/cookie/i").first.is_visible():
            for sel in ("button:has-text('Accept')", "button:has-text('OK')", "button:has-text('Agree')"):
                if _click_if_visible(p, css=sel, timeout=1200):
                    return True
    except Exception:
        pass
    return False

def _find_in_tree_for(p, selector_list):
    """
    Search in main page, then in iframes.
    selector_list: list of ('label'|'css', pattern)
    Returns (frame_or_page, locator) or (None, None)
    """
    # page first
    for typ, pat in selector_list:
        try:
            loc = p.get_by_label(pat) if typ == "label" else p.locator(pat).first
            if loc and loc.is_visible():
                return p, loc
        except Exception:
            pass
    # then iframes
    for f in p.frames:
        if f == p.main_frame:
            continue
        for typ, pat in selector_list:
            try:
                loc = f.get_by_label(pat) if typ == "label" else f.locator(pat).first
                if loc and loc.is_visible():
                    return f, loc
            except Exception:
                pass
    return None, None

def _click_login_with_email_if_needed(p):
    texts = [
        r"(log ?in|sign ?in) with email",
        r"continue with email",
        r"use email instead",
    ]
    for t in texts:
        if _click_if_visible(p, role="button", name=re.compile(t, re.I), timeout=1500):
            return True
        if _click_if_visible(p, css=f"button:has-text('{t}')", timeout=1500):
            return True
    return False

def _start_sso_if_requested(p):
    if ARBOR_LOGIN_METHOD == "microsoft":
        for label in (r"Sign in with Microsoft", r"Microsoft", r"Office 365"):
            if _click_if_visible(p, role="button", name=re.compile(label, re.I), timeout=2000):
                return "microsoft"
        if _click_if_visible(p, css="a:has-text('Microsoft')", timeout=2000):
            return "microsoft"
    if ARBOR_LOGIN_METHOD == "google":
        for label in (r"Sign in with Google", r"Google"):
            if _click_if_visible(p, role="button", name=re.compile(label, re.I), timeout=2000):
                return "google"
        if _click_if_visible(p, css="a:has-text('Google')", timeout=2000):
            return "google"
    return None

def login_guardian(page):
    """
    Use the provided Playwright 'page' to log into Arbor guardian portal.
    Works with .education tenant, handles cookie banners, SSO buttons,
    email/password forms in iframes, and optional child DOB step.
    """
    # 1) Navigate to a login page
    for u in (ARBOR_BASE_URL, f"{ARBOR_BASE_URL}/auth/login", f"{ARBOR_BASE_URL}/login"):
        try:
            page.goto(u, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_load_state("networkidle")
            break
        except Exception:
            pass

    _accept_cookies(page)

    # 2) Start SSO if requested (user can complete manually if needed)
    sso = _start_sso_if_requested(page)
    if sso:
        print(f"➡️  {sso.title()} SSO button clicked — complete provider login if prompted.")
        return

    # 3) If the page shows SSO-first, swap to the email form
    _click_login_with_email_if_needed(page)

    # 4) Find email field (page or iframe)
    frame, email_el = _find_in_tree_for(page, [
        ("label", re.compile(r"email", re.I)),
        ("css", "input[type='email']"),
        ("css", "input[name='email']"),
        ("css", "input[autocomplete='username']"),
        ("css", "input[placeholder*='email' i]"),
    ])
    if not email_el:
        raise RuntimeError("Email input not found. If your school uses SSO, set ARBOR_LOGIN_METHOD=microsoft or google in .env")

    email_el.fill(ARBOR_EMAIL)

    # 5) Password
    _, pw_el = _find_in_tree_for(page, [
        ("label", re.compile(r"(password|passcode)", re.I)),
        ("css", "input[type='password']"),
        ("css", "input[name='password']"),
        ("css", "input[autocomplete='current-password']"),
        ("css", "input[placeholder*='password' i]"),
    ])
    if pw_el:
        pw_el.fill(ARBOR_PASSWORD)

    # 6) Submit — try several strategies
    submitted = False
    # A) role/name match
    try:
        (frame or page).get_by_role("button", name=re.compile(r"^log ?in$", re.I)).click(timeout=2500)
        submitted = True
    except Exception:
        pass

    # B) explicit CSS fallbacks
    if not submitted:
        for css in [
            "button:has-text('Log in')",
            "button[type='submit']",
            "input[type='submit']",
            "form button",
        ]:
            try:
                btn = (frame or page).locator(css).first
                if btn and btn.is_visible():
                    btn.click(timeout=2500)
                    submitted = True
                    break
            except Exception:
                pass

    # C) last resort: press Enter in password field
    if not submitted and pw_el:
        try:
            pw_el.press("Enter")
            submitted = True
        except Exception:
            pass

    # Wait for something to happen
    page.wait_for_load_state("networkidle")

    # 6b) Verify we moved past the login form; if not, try once more
    try:
        still_on_login = (
            page.locator("input[type='email']").is_visible() and
            page.locator("input[type='password']").is_visible()
        )
    except Exception:
        still_on_login = False

    if still_on_login:
        # Try one more direct click on the green button
        try:
            (frame or page).locator("button:has-text('Log in')").first.click(timeout=2500)
            page.wait_for_load_state("networkidle")
        except Exception:
            pass

    # 7) Optional DOB step
    if ARBOR_CHILD_DOB:
        try:
            page.wait_for_timeout(600)
            f_dob, dob_input = _find_in_tree_for(page, [
                ("label", re.compile(r"(date of birth|dob)", re.I)),
                ("css", "input[placeholder*='birth' i]"),
                ("css", "input[name*='dob' i]"),
            ])
            if dob_input:
                dob_input.fill(ARBOR_CHILD_DOB)
                try:
                    (f_dob or page).get_by_role("button", name=re.compile(r"(verify|continue|confirm)", re.I)).click(timeout=2500)
                except Exception:
                    (f_dob or page).locator("button[type='submit']").first.click()
        except Exception:
            pass

    page.wait_for_load_state("networkidle")

        # --- Rebind BASE to the actual domain we logged into (.education vs .sc) ---
    from urllib.parse import urlparse
    def _origin(u: str) -> str:
        p = urlparse(u)
        return f"{p.scheme}://{p.netloc}"

    global BASE
    BASE = _origin(page.url)
    print("🔗 Using origin:", BASE)