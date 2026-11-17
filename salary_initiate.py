"""
CorporatePAY — Salary Payment, step 1 of 2: INITIATE (initiator account)
========================================================================
Reads the salary table (Google Sheet or CSV), keeps rows with
"Salary Cleared" = Y, logs in as the INITIATOR, fills the batch, checks the
portal's own totals against ours, and clicks Initiate.

It then writes runs/last_batch.json so salary_approve.py can check that the
batch the approver is about to approve is exactly this one.

  python salary_initiate.py --csv test.csv --dry-run   # everything but the click
  python salary_initiate.py                            # for real, from the sheet
"""

import os
import re
import sys
import csv
import json
import argparse
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import cp_common as cp

log = cp.log

COL_NAME = "Full Name"
COL_ACCOUNT = "Account Number"
COL_AMOUNT = "Salary Amount"
COL_CLEARED = "Salary Cleared"

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


class Employee:
    __slots__ = ("name", "account", "amount", "row_no")

    def __init__(self, name, account, amount, row_no):
        self.name, self.account, self.amount, self.row_no = name, account, amount, row_no


# ── Input ─────────────────────────────────────────────────────────────────────

def _resolve(path_str: str) -> Path:
    p = Path(path_str).expanduser()
    return p if p.is_absolute() else cp.HERE / p


def read_from_sheet(sheet_id: str, cell_range: str) -> list:
    """Read the table via the Sheets API. Uses its own token file, so the
    statement workflow's token.json is never touched."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token = _resolve(os.getenv("CP_SHEETS_TOKEN", "token_sheets.json"))
    client = _resolve(os.getenv("CP_GOOGLE_CREDENTIALS", "credentials.json"))

    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), SHEETS_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not client.exists():
                raise FileNotFoundError(
                    f"{client} not found (needed for Sheets sign-in). "
                    f"Set CP_GOOGLE_CREDENTIALS, or use --csv."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(client), SHEETS_SCOPES)
            creds = flow.run_local_server(port=0)
        token.write_text(creds.to_json())

    resp = (
        build("sheets", "v4", credentials=creds)
        .spreadsheets().values()
        .get(spreadsheetId=sheet_id, range=cell_range, valueRenderOption="FORMATTED_VALUE")
        .execute()
    )
    values = resp.get("values", [])
    if not values:
        raise ValueError(f"No data returned for range {cell_range}")
    header = [h.strip() for h in values[0]]
    rows = []
    for n, raw in enumerate(values[1:], start=1):
        raw = list(raw) + [""] * (len(header) - len(raw))
        row = dict(zip(header, raw))
        row["_row_no"] = n
        rows.append(row)
    return rows


def read_from_csv(path: Path) -> list:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = []
        for n, row in enumerate(csv.DictReader(fh), start=1):
            row["_row_no"] = n
            rows.append(row)
        return rows


def parse_employees(rows: list) -> list:
    """Keep Salary Cleared == Y. Anything ambiguous is a hard error — this moves money."""
    if not rows:
        raise ValueError("The salary table is empty.")
    missing = [c for c in (COL_NAME, COL_ACCOUNT, COL_AMOUNT, COL_CLEARED) if c not in rows[0]]
    if missing:
        raise ValueError(f"Missing column(s): {missing}. Found: "
                         f"{[k for k in rows[0] if not str(k).startswith('_')]}")

    employees, skipped = [], 0
    for row in rows:
        n = row["_row_no"]
        cleared = str(row.get(COL_CLEARED) or "").strip().upper()
        name = str(row.get(COL_NAME) or "").strip()
        account = str(row.get(COL_ACCOUNT) or "").strip()
        if not name and not account:
            continue
        if cleared != "Y":
            if cleared not in ("N", ""):
                raise ValueError(f"Row {n} ({name}): '{COL_CLEARED}' is '{cleared}' — expected Y or N.")
            skipped += 1
            continue
        if not account.isdigit():
            raise ValueError(f"Row {n} ({name}): account '{account}' must be digits only.")
        raw = str(row.get(COL_AMOUNT) or "").strip().replace(",", "")
        try:
            amount = Decimal(raw)
        except (InvalidOperation, ValueError):
            raise ValueError(f"Row {n} ({name}): amount '{raw}' is not a number.")
        if amount <= 0:
            raise ValueError(f"Row {n} ({name}): amount must be positive.")
        if amount.as_tuple().exponent < -2:
            raise ValueError(f"Row {n} ({name}): amount {amount} has more than 2 decimals.")
        employees.append(Employee(name, account, amount, n))

    seen = {}
    for e in employees:
        if e.account in seen:
            raise ValueError(f"Account {e.account} appears twice (rows {seen[e.account].row_no} "
                             f"and {e.row_no}). Fix the sheet.")
        seen[e.account] = e
    if not employees:
        raise ValueError("No rows marked Y — nothing to pay.")
    log.info(f"{len(employees)} to pay, {skipped} skipped (not cleared)")
    return employees


def batch_total(employees) -> Decimal:
    return sum((e.amount for e in employees), Decimal("0"))


def format_batch_count(n: int) -> str:
    """The portal wants a zero on the front: 3 → '03', 10 → '010'."""
    return f"0{n}"


def confirm(employees, remark: str):
    total = batch_total(employees)
    width = max(len(e.name) for e in employees)
    print("\n" + "═" * 72)
    print("  SALARY BATCH — REVIEW BEFORE INITIATING")
    print("═" * 72)
    for i, e in enumerate(employees, 1):
        print(f"  {i:>2}. {e.name:<{width}}  {e.account:>20}  {e.amount:>12,.2f}")
    print("─" * 72)
    print(f"  Batch Count  : {format_batch_count(len(employees))}  ({len(employees)} people)")
    print(f"  Batch Amount : NPR {total:,.2f}")
    print(f"  Remark       : {remark}")
    print("═" * 72)
    if input("\nType 'yes' to proceed, anything else to abort: ").strip().lower() != "yes":
        print("Aborted — nothing was sent.")
        sys.exit(0)


# ── Portal steps ──────────────────────────────────────────────────────────────

def _field_by_label(page, label: str, tag: str = "input"):
    container = page.locator(
        f'div:has(> label:text-is("{label}")), div:has(> span:text-is("{label}"))'
    ).first
    if container.count() == 0:
        container = page.locator(f'text="{label}"').first.locator("xpath=..")
    return container.locator(tag).first


def fill_batch_information(page, total: Decimal, count: int):
    log.info("Filling Batch Information …")
    page.wait_for_selector("text=Batch Information", timeout=20_000)

    debtor = _field_by_label(page, "Debtor Account Number", "select")
    real = [o for o in debtor.locator("option").all()
            if (o.get_attribute("value") or "").strip()]
    wanted = (os.getenv("CP_DEBTOR_ACCOUNT") or "").strip()
    if wanted:
        real = [o for o in real if wanted in o.inner_text()]
    if len(real) != 1:
        raise RuntimeError(
            f"Expected exactly one debtor account to choose, found {len(real)}. "
            f"Set CP_DEBTOR_ACCOUNT in the env file to the account number to debit."
        )
    debtor.select_option(value=real[0].get_attribute("value"))
    log.info(f"Debtor account: {real[0].inner_text().strip()}")
    page.wait_for_timeout(500)

    amount_field = _field_by_label(page, "Batch Amount")
    amount_field.click()
    amount_field.fill("")
    amount_field.type(f"{total:.2f}", delay=40)
    page.keyboard.press("Tab")
    page.wait_for_timeout(400)
    log.info(f"Batch Amount typed {total:.2f} → portal shows '{amount_field.input_value().strip()}'")

    count_str = format_batch_count(count)
    count_field = _field_by_label(page, "Batch Count")
    count_field.click()
    count_field.fill("")
    count_field.type(count_str, delay=40)
    page.keyboard.press("Tab")
    page.wait_for_timeout(300)
    log.info(f"Batch Count typed '{count_str}'")

    try:
        purpose = _field_by_label(page, "Category Purpose", "select").input_value().strip()
        if purpose.upper() != "SALC":
            log.warning(f"Category Purpose is '{purpose}', expected SALC")
    except Exception:
        log.warning("Could not read Category Purpose — check it by eye")


def open_recipient_list(page):
    log.info("Opening the saved-recipient list …")
    page.wait_for_selector("text=Transaction Information", timeout=20_000)
    page.locator('text="Individual"').first.click()
    page.wait_for_timeout(500)
    page.locator('text="Select Existing Recipients"').first.click()
    page.wait_for_timeout(1500)
    page.wait_for_selector("text=Total Batch Count", timeout=20_000)


def index_recipient_rows(page) -> dict:
    """account number → row. Reads only rows in the DOM; pagination not handled —
    a missing employee aborts the run by name rather than underpaying."""
    rows = page.locator("table tbody tr")
    index = {}
    for i in range(rows.count()):
        row = rows.nth(i)
        cells = row.locator("td")
        if cells.count() < 5:
            continue
        account = cells.nth(2).inner_text().strip()
        if account:
            index[account] = row
    log.info(f"Recipient list has {len(index)} row(s)")
    return index


def _names_agree(sheet_name: str, portal_name: str) -> bool:
    a = " ".join(sheet_name.split()).casefold()
    b = " ".join(portal_name.replace("…", "").replace("...", "").split()).casefold()
    return bool(a and b) and (a == b or a.startswith(b) or b.startswith(a))


def match_rows(employees, index: dict) -> list:
    missing = [e for e in employees if e.account not in index]
    if missing:
        for e in missing:
            log.error(f"No saved recipient for {e.name} (account {e.account})")
        raise RuntimeError(f"{len(missing)} employee(s) not in the saved recipient list.")
    pairs = []
    for e in employees:
        row = index[e.account]
        portal_name = row.locator("td").nth(3).inner_text().strip()
        if not _names_agree(e.name, portal_name):
            print(f"\n⚠️  Name mismatch on account {e.account}")
            print(f"     sheet : {e.name}\n     portal: {portal_name}")
            if input("     Continue with this row? [y/N]: ").strip().lower() != "y":
                raise RuntimeError("Aborted at name mismatch.")
        pairs.append((e, row, portal_name))
    return pairs


def fill_rows(page, pairs: list, remark: str):
    # Columns: [checkbox, S.N., Account Number, Account Name, Bank Name, Amount, Remarks]
    for e, row, portal_name in pairs:
        cells = row.locator("td")
        checkbox = cells.nth(0).locator('input[type="checkbox"]')
        if not checkbox.is_checked():
            checkbox.check()
        page.wait_for_timeout(200)

        amount_input = cells.nth(5).locator("input").first
        amount_input.click()
        amount_input.fill("")
        amount_input.type(f"{e.amount:.2f}", delay=30)
        page.keyboard.press("Tab")
        page.wait_for_timeout(250)

        remark_input = cells.nth(6).locator("input").first
        remark_input.click()
        remark_input.fill("")
        remark_input.type(remark, delay=15)
        page.keyboard.press("Tab")
        page.wait_for_timeout(250)
        log.info(f"  ✓ {portal_name} ({e.account}) → {amount_input.input_value().strip()}")


def verify_totals(page, employees):
    """Compare the portal's own running totals with ours; abort on any difference."""
    summary = page.locator("text=/Total Batch Count/").first.inner_text()
    flat = summary.replace(",", "")
    m_count = re.search(r"Total Batch Count:\s*(\d+)", flat)
    m_total = re.search(r"Total Amount:\s*(?:NPR\s*)?([\d.]+)", flat)
    if not m_count or not m_total:
        raise RuntimeError(f"Could not parse the portal totals from: {summary!r}")
    got_count, got_total = int(m_count.group(1)), Decimal(m_total.group(1))
    if got_count != len(employees):
        raise RuntimeError(f"Count mismatch — portal {got_count}, expected {len(employees)}.")
    if got_total != batch_total(employees):
        raise RuntimeError(f"Amount mismatch — portal {got_total:,.2f}, "
                           f"expected {batch_total(employees):,.2f}.")
    log.info(f"Totals verified: {got_count} recipients, NPR {got_total:,.2f} ✓")


def save_batch_record(employees, remark: str):
    record = {
        "initiated_at": datetime.now().isoformat(timespec="seconds"),
        "remark": remark,
        "count": len(employees),
        "total": f"{batch_total(employees):.2f}",
        "payments": [{"name": e.name, "account": e.account, "amount": f"{e.amount:.2f}"}
                     for e in employees],
    }
    cp.LAST_BATCH.write_text(json.dumps(record, indent=2))
    log.info(f"Batch record → {cp.LAST_BATCH}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run(employees, remark: str, dry_run: bool):
    creds = cp.get_creds("initiator")
    total = batch_total(employees)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(30_000)
        try:
            cp.login(page, "initiator", creds)
            cp.open_salary_payment(page)

            fill_batch_information(page, total, len(employees))
            open_recipient_list(page)
            pairs = match_rows(employees, index_recipient_rows(page))
            fill_rows(page, pairs, remark)
            verify_totals(page, employees)
            cp.screenshot(page, "initiate_prefill")

            if dry_run:
                print("\n── DRY RUN ── everything is filled and verified; Initiate was NOT clicked.")
                input("  Check the browser, then press Enter to close it … ")
                return

            log.info("Clicking Initiate …")
            page.locator('button:has-text("Initiate")').first.click()
            page.wait_for_timeout(3000)
            cp.screenshot(page, "initiate_done")
            save_batch_record(employees, remark)

            expiry = (datetime.now() + timedelta(days=3)).strftime("%A %d %B")
            print("\n" + "═" * 72)
            print("  INITIATE CLICKED — NOT YET PAID")
            print("═" * 72)
            print(f"  {len(employees)} recipients, NPR {total:,.2f}")
            print("  Look at the browser: if the portal shows a confirmation box or an")
            print("  error, deal with it now.")
            print("  Next: python salary_approve.py   (approver account)")
            print(f"  The batch expires if not approved by {expiry}.")
            print("═" * 72)
            input("\n  Press Enter to close the browser … ")

        except (PWTimeout, RuntimeError) as e:
            log.error(f"{type(e).__name__}: {str(e)[:400]}")
            cp.screenshot(page, "initiate_error")
            print("\n  Browser left open so you can see where it stopped.")
            input("  Press Enter to close … ")
            raise
        finally:
            context.close()
            browser.close()


def main():
    cp.setup_logging()
    parser = argparse.ArgumentParser(description="CorporatePAY salary payment — initiate")
    parser.add_argument("--env", default=".env", help="env file in this folder (default .env)")
    parser.add_argument("--remark", default=None, help='e.g. "Salary Payment Kartik"')
    parser.add_argument("--csv", default=None, help="read a local CSV instead of the Google Sheet")
    parser.add_argument("--dry-run", action="store_true", help="fill everything, do not click Initiate")
    args = parser.parse_args()

    print("\n══════════════════════════════════════════")
    print("  CorporatePAY — Salary Payment: INITIATE")
    print("══════════════════════════════════════════\n")

    cp.load_env(args.env)
    cp.get_creds("initiator")          # fail now, not after the confirmation

    if args.csv:
        rows = read_from_csv(Path(args.csv).expanduser())
    else:
        sheet_id = os.getenv("CP_SALARY_SHEET_ID")
        if not sheet_id:
            print("Set CP_SALARY_SHEET_ID in the env file, or pass --csv <file>.")
            sys.exit(1)
        rows = read_from_sheet(sheet_id, os.getenv("CP_SALARY_SHEET_RANGE", "Sheet1!A1:D"))
    employees = parse_employees(rows)

    remark = args.remark
    while not remark:
        remark = input('Remark for all payees (e.g. "Salary Payment Kartik"): ').strip()

    confirm(employees, remark)
    run(employees, remark, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
