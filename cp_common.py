"""
CorporatePAY (connectIPS) — shared pieces for the payment workflows
===================================================================
Login (Stages 1-3), env loading, navigation and small helpers used by both
salary_initiate.py (initiator account) and salary_approve.py (approver account).

The login is lifted from the iMac's working script, including its fixed
settle waits. Fix a portal change here and both scripts get it.
"""

import os
import sys
import time
import logging
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PWTimeout

log = logging.getLogger("corppay")

# Everything is resolved relative to THIS folder, so the scripts work no matter
# which directory you run them from.
HERE = Path(__file__).resolve().parent
RUNS_DIR = HERE / "runs"                 # screenshots + last_batch.json
RUNS_DIR.mkdir(exist_ok=True)
LAST_BATCH = RUNS_DIR / "last_batch.json"

PORTAL_URL = "https://corporatepay.connectips.com/#/login"
MAX_TOTP_ATTEMPTS = 3

FUND_TRANSFER_ROUTE = "/bank/cips/cips-ft"

# Settle waits in seconds: [after TOTP verify, after bank login, after page nav].
# Set CP_HEAVY_TRAFFIC=1 in the .env when the portal is slow (festivals etc.).
REGULAR_MODE = [8, 10, 12]
HEAVY_TRAFFIC_MODE = [55, 35, 35]

ROLE_PREFIX = {"initiator": "CP_INIT_", "approver": "CP_APPR_"}


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_env(env_name: str):
    """Load an env file from this folder; exit loudly if it isn't there."""
    path = Path(env_name)
    if not path.is_absolute():
        path = HERE / env_name
    if not path.exists():
        print(f"Env file not found: {path}")
        print("Copy .env.example to .env and fill it in.")
        sys.exit(1)
    load_dotenv(path, override=True)
    log.info(f"Loaded credentials file: {path.name}")


def waits() -> list:
    return HEAVY_TRAFFIC_MODE if os.getenv("CP_HEAVY_TRAFFIC", "0") == "1" else REGULAR_MODE


def settle(seconds: int):
    if seconds > 5:
        print(f"Waiting {seconds}s for the portal …")
    time.sleep(seconds)


def get_creds(role: str) -> dict:
    """Read one role's credentials; exit with the names of any that are missing."""
    prefix = ROLE_PREFIX[role]
    names = {
        "username": prefix + "USERNAME",
        "password": prefix + "PASSWORD",
        "bank_password": prefix + "BANK_PASSWORD",
        "corporate_code": "CP_CORPORATE_CODE",
    }
    creds = {key: (os.getenv(var) or "").strip() for key, var in names.items()}
    missing = [names[k] for k, v in creds.items() if not v]
    if missing:
        print(f"Missing in the env file: {', '.join(missing)}")
        sys.exit(1)
    return creds


def ask_human(message: str):
    """Hand a step to the person at the keyboard, then carry on."""
    print(f"\n👉 {message}")
    input("   Press Enter when done … ")


def get_totp(role: str, attempt: int = 1) -> str:
    if attempt == 1:
        print(f"\n📱 Google Authenticator code for the {role.upper()} login.")
    else:
        print(f"\n⚠️  TOTP attempt {attempt} — enter a fresh {role.upper()} code.")
    return input("   Enter 6-digit code: ").strip()


def login(page, role: str, creds: dict):
    """Three-stage login on an existing Playwright page. Raises PWTimeout on failure."""
    w = waits()

    # ── Stage 1: Username / Password / Corporate Code ─────────────────────────
    log.info(f"Opening portal login page ({role}) …")
    page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3000)
    page.wait_for_selector(
        'input[placeholder*="Username"], input[name*="user"], input[type="text"]'
    )
    try:
        page.fill('input[placeholder*="Username"]', creds["username"])
        page.fill('input[placeholder*="Password"]', creds["password"])
        page.fill('input[placeholder*="Corporate"]', creds["corporate_code"])
    except Exception:
        all_inputs = page.locator('input:not([type="hidden"])').all()
        all_inputs[0].fill(creds["username"])
        all_inputs[1].fill(creds["password"])
        all_inputs[2].fill(creds["corporate_code"])

    log.info("Filled Stage-1 credentials")
    page.click('button[type="submit"], button:has-text("Login"), button:has-text("Sign In")')
    # networkidle never fires on this portal — wait for the TOTP modal itself.
    page.wait_for_selector('[id="2faLoginSteps"]', timeout=30_000)

    # ── Stage 2: TOTP ─────────────────────────────────────────────────────────
    for attempt in range(1, MAX_TOTP_ATTEMPTS + 1):
        page.wait_for_timeout(1500)            # modal settles / re-settles
        modal = page.locator('[id="2faLoginSteps"]')
        code = get_totp(role, attempt)

        single = modal.locator('input[type="text"], input[type="number"], input[type="tel"]')
        if single.count() == 1:
            single.click(click_count=3)
            single.type(code, delay=80)
        else:
            boxes = modal.locator("input").all()
            for i, digit in enumerate(code):
                boxes[i].click(click_count=3)
                boxes[i].type(digit, delay=100)
                time.sleep(0.1)

        log.info(f"Submitted TOTP (attempt {attempt}/{MAX_TOTP_ATTEMPTS})")
        modal.locator('button:has-text("Verify")').click()

        try:
            settle(w[0])
            page.wait_for_selector('[id="2faLoginSteps"]', state="hidden", timeout=8_000)
            log.info("TOTP verified ✓")
            break
        except PWTimeout:
            err = modal.locator('.alert, .error, [class*="error"], [class*="invalid"], p')
            text = err.first.inner_text() if err.count() > 0 else "Invalid or expired code"
            log.warning(f"TOTP stage failed: {text.strip()[:120]}")
            if attempt == MAX_TOTP_ATTEMPTS:
                raise PWTimeout("TOTP verification failed after all attempts")

    # ── Stage 3: Bank Password ────────────────────────────────────────────────
    log.info("Waiting for dashboard …")
    page.wait_for_selector('button:has-text("Bank Login"), a:has-text("Bank Login")', timeout=20_000)
    page.wait_for_timeout(1000)
    page.click('button:has-text("Bank Login"), a:has-text("Bank Login")')

    page.wait_for_selector(
        '.modal.show input[type="password"], .modal.d-block input[type="password"]',
        timeout=10_000,
    )
    page.wait_for_timeout(500)
    modal = page.locator(".modal.show, .modal.d-block").last
    modal.locator('input[type="password"]').fill(creds["bank_password"])
    modal.locator('button:has-text("Login")').click()
    settle(w[1])
    page.wait_for_selector(".modal.show, .modal.d-block", state="hidden", timeout=20_000)
    page.wait_for_timeout(2000)
    log.info(f"Logged in as {role} ✓")


def open_salary_payment(page):
    """Sidebar: connectIPS (Real-Time) → Salary Payment (same clicks the iMac script uses)."""
    log.info("Navigating to connectIPS (Real-Time) → Salary Payment …")
    page.wait_for_selector("text=connectIPS (Real-Time)", timeout=20_000)
    page.click("text=connectIPS (Real-Time)")
    page.wait_for_timeout(800)
    page.wait_for_selector("text=Salary Payment", timeout=10_000)
    page.click("text=Salary Payment")
    settle(waits()[2])


def screenshot(page, label: str) -> Path:
    from datetime import datetime
    path = RUNS_DIR / f"{label}_{datetime.now():%Y%m%d_%H%M%S}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        log.info(f"Screenshot → {path}")
    except Exception:
        pass
    return path


# ── Shared form helpers (used by fund_transfer.py) ────────────────────────────

import re
import csv
from decimal import Decimal


def npr(amount) -> str:
    """Nepali grouping, as the portal shows it: 2125000 → 'NPR 21,25,000.00'."""
    whole, frac = f"{Decimal(amount):.2f}".split(".")
    if len(whole) > 3:
        head, tail, groups = whole[:-3], whole[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"NPR {whole}.{frac}"


def field_by_label(page, labels, tag: str = "input"):
    """First form field (of `tag`) whose label text exactly matches one of
    `labels`. Returns None if none is found — callers decide what that means."""
    for label in ([labels] if isinstance(labels, str) else labels):
        container = page.locator(
            f'div:has(> label:text-is("{label}")), div:has(> span:text-is("{label}"))'
        ).first
        if container.count() == 0:
            text = page.locator(f'text="{label}"')
            if text.count() == 0:
                continue
            container = text.first.locator("xpath=..")
        field = container.locator(tag).first
        if field.count() > 0:
            return field
    return None


def type_into(page, field, value: str):
    """Clear, type slowly, Tab out so the portal runs its formatting/validation."""
    field.click()
    field.fill("")
    field.type(value, delay=30)
    page.keyboard.press("Tab")
    page.wait_for_timeout(300)


def amount_shown(field):
    """Read an amount back from a field the portal formatted ('60,000.00' etc.)."""
    digits = re.sub(r"[^\d.]", "", field.input_value())
    return Decimal(digits) if digits else None


def open_fund_transfer(page):
    """Sidebar: connectIPS (Real-Time) → Fund Transfer, landing on Initiation.

    Everything is scoped to <aside class="sidebar">, so the dashboard's own
    "connectIPS (Real-Time)" tab and its "Fund Transfer" history rows can't be
    clicked by mistake. The Fund Transfer link is found by its route
    (#/bank/cips/cips-ft), which is unique to connectIPS — NCHL-IPS has its own.
    """
    log.info("Navigating to connectIPS (Real-Time) → Fund Transfer …")
    sidebar = page.locator("aside.sidebar")
    sidebar.wait_for(timeout=20_000)

    link = sidebar.locator(f'a[href*="{FUND_TRANSFER_ROUTE}"]').first
    if not link.is_visible():
        # Bootstrap collapse toggle; only click it if it's closed, or it collapses
        toggle = sidebar.locator(
            'a[data-bs-toggle="collapse"]:has(p.sidebar-title:text-is("connectIPS (Real-Time)"))'
        ).first
        if toggle.get_attribute("aria-expanded") != "true":
            toggle.click()
        link.wait_for(state="visible", timeout=10_000)

    link.click()
    heavy = os.getenv("CP_HEAVY_TRAFFIC", "0") == "1"
    page.wait_for_selector("text=Debtor Information", timeout=90_000 if heavy else 30_000)
    page.wait_for_timeout(1000)
    if FUND_TRANSFER_ROUTE not in page.url:
        raise RuntimeError(f"Expected the connectIPS Fund Transfer page, but the URL is {page.url}")

DROPDOWN = "whitelabelled-dropdown"     # the portal's react-select class prefix


def pick_dropdown(page, label: str, match, search: str = "", what: str = "") -> str:
    """Choose the ONE option for which match(text) is True in the dropdown
    belonging to <label for=...>. The portal's dropdowns are react-select
    (div#<id> > .whitelabelled-dropdown__control), not <select>; plain
    <select> is handled too. Returns the text shown after choosing."""
    what = what or label
    lab = page.locator(f'label:text-is("{label}")').first
    if lab.count() == 0:
        raise RuntimeError(f"Couldn't find the '{label}' label.")
    field_id = lab.get_attribute("for") or ""
    box = page.locator(f'[id="{field_id}"]').first
    if not field_id or box.count() == 0:
        raise RuntimeError(f"The '{label}' label doesn't point at a field (for='{field_id}').")

    if box.evaluate("e => e.tagName.toLowerCase()") == "select":
        texts = [o.inner_text().strip() for o in box.locator("option").all()]
        hits = [t for t in texts if match(t)]
        if len(hits) != 1:
            raise RuntimeError(f"Expected one matching {what}, found {len(hits)}. Options: {texts[:8]}")
        box.select_option(label=hits[0])
        page.wait_for_timeout(600)
        return box.evaluate("s => s.options[s.selectedIndex].text").strip()

    box.locator(f".{DROPDOWN}__control").click()
    if search:
        page.keyboard.type(search, delay=30)        # react-select filters as you type
    options = page.locator(f".{DROPDOWN}__option")
    try:
        options.first.wait_for(state="visible", timeout=10_000)
    except PWTimeout:
        page.keyboard.press("Escape")
        raise RuntimeError(f"The {what} dropdown showed no options"
                           + (f" for '{search}'." if search else "."))
    texts = [options.nth(i).inner_text().strip() for i in range(options.count())]
    hits = [i for i, t in enumerate(texts) if match(t)]
    if len(hits) != 1:
        page.keyboard.press("Escape")
        raise RuntimeError(f"Expected one matching {what}, found {len(hits)}. Options: {texts[:8]}")
    options.nth(hits[0]).click()
    page.wait_for_timeout(800)
    shown = box.locator(f".{DROPDOWN}__single-value")
    return shown.first.inner_text().strip() if shown.count() else texts[hits[0]]


def select_only_debtor(page):
    """Pick the single debtor account (or CP_DEBTOR_ACCOUNT if there are several)."""
    wanted = (os.getenv("CP_DEBTOR_ACCOUNT") or "").strip()
    account = pick_dropdown(
        page, "Debtor Account Number",
        match=lambda t: bool(re.search(r"\d{6,}", t)) and wanted in t,
        what="debtor account",
    )
    page.wait_for_timeout(800)                       # Debtor Account Name auto-fills
    name_field = field_by_label(page, "Debtor Account Name")
    name = name_field.input_value().strip() if name_field else ""
    if not name:
        raise RuntimeError("Debtor Account Name didn't fill in after choosing the account.")
    log.info(f"Debtor: {account}  {name}")
    return account, name

def read_sheet_rows(sheet_id: str, cell_range: str) -> list:
    """Rows of a Google Sheet range as dicts (first row = headers)."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

    def here(p):
        p = Path(p).expanduser()
        return p if p.is_absolute() else HERE / p

    token = here(os.getenv("CP_SHEETS_TOKEN", "token_sheets.json"))
    client = here(os.getenv("CP_GOOGLE_CREDENTIALS", "credentials.json"))
    creds = Credentials.from_authorized_user_file(str(token), scopes) if token.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not client.exists():
                raise FileNotFoundError(f"{client} not found (needed for Sheets sign-in). "
                                        f"Set CP_GOOGLE_CREDENTIALS, or use --csv.")
            creds = InstalledAppFlow.from_client_secrets_file(str(client), scopes).run_local_server(port=0)
        token.write_text(creds.to_json())

    values = (build("sheets", "v4", credentials=creds).spreadsheets().values()
              .get(spreadsheetId=sheet_id, range=cell_range, valueRenderOption="FORMATTED_VALUE")
              .execute().get("values", []))
    if not values:
        raise ValueError(f"No data in {cell_range}")
    header = [h.strip() for h in values[0]]
    rows = []
    for n, raw in enumerate(values[1:], start=2):           # n = sheet row number
        raw = list(raw) + [""] * (len(header) - len(raw))
        row = dict(zip(header, raw))
        row["_row_no"] = n
        rows.append(row)
    return rows


def read_csv_rows(path: Path) -> list:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = []
        for n, row in enumerate(csv.DictReader(fh), start=2):
            row["_row_no"] = n
            rows.append(row)
        return rows
