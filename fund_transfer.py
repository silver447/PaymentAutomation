"""
CorporatePAY — Fund Transfer: INITIATE (initiator account)
==========================================================
connectIPS (Real-Time) → Fund Transfer → Initiation. One recipient per
transaction, maximum 20 lakh (NPR 20,00,000) each.

Two ways to pick who gets paid:

  existing  the portal's saved recipients are listed in the terminal; you pick
            one, type the amount and remark. You can repeat without logging in
            again.
  manual    rows come from a Google Sheet (or CSV) with columns
              Bank Name | Account Number | Account Name | Amount | Remark
            you pick which rows to send; each becomes its own transaction.

Every transaction is filled in, read back off the form, and shown to you;
Initiate is clicked only after you type 'yes'. Approval stays with the
approver.

  python fund_transfer.py                     # asks which mode
  python fund_transfer.py --mode existing
  python fund_transfer.py --mode manual       # reads CP_TRANSFER_SHEET_ID
  python fund_transfer.py --mode manual --csv transfers.csv
  python fund_transfer.py --dry-run           # fill everything, never click Initiate
"""

import os
import sys
import argparse
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import cp_common as cp

log = cp.log

MAX_PER_TXN = Decimal("2000000")        # 20 lakh — portal limit per Fund Transfer

SHEET_COLS = ["Bank Name", "Account Number", "Account Name", "Amount", "Remark"]

# Labels on the "Enter Details Manually" form. Not yet seen — several guesses each.
# BANK_LABELS = ["Bank Name", "Creditor Bank Name", "Creditor Bank", "Bank"]
ACCT_LABELS = ["Creditor Account Number", "Account Number", "Creditor Account No.", "Account No."]
NAME_LABELS = ["Creditor Account Name", "Account Name", "Creditor Name"]
AMOUNT_LABELS = ["Transaction Amount", "Amount"]
REMARK_LABELS = ["Remark", "Remarks"]


# ── Amounts and prompts ───────────────────────────────────────────────────────

def parse_amount(raw) -> Decimal:
    s = str(raw or "").strip().replace(",", "")
    if s.upper().startswith("NPR"):
        s = s[3:].strip()
    try:
        amount = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"'{raw}' is not a number")
    if amount <= 0:
        raise ValueError("amount must be more than zero")
    if amount.as_tuple().exponent < -2:
        raise ValueError("more than 2 decimal places")
    if amount > MAX_PER_TXN:
        raise ValueError(f"{cp.npr(amount)} is over the 20 lakh limit per transaction")
    return amount


def ask_amount() -> Decimal:
    while True:
        try:
            return parse_amount(input("Amount in NPR (max 20,00,000): "))
        except ValueError as e:
            print(f"  ⚠  {e}")


def ask_remark() -> str:
    remark = ""
    while not remark:
        remark = input("Remark: ").strip()
    return remark


# ── Manual mode: the sheet ────────────────────────────────────────────────────

def load_manual_rows(csv_path) -> list:
    if csv_path:
        rows = cp.read_csv_rows(Path(csv_path).expanduser())
    else:
        sheet_id = os.getenv("CP_TRANSFER_SHEET_ID")
        if not sheet_id:
            print("Set CP_TRANSFER_SHEET_ID in the env file, or pass --csv <file>.")
            sys.exit(1)
        rows = cp.read_sheet_rows(sheet_id, os.getenv("CP_TRANSFER_SHEET_RANGE", "Sheet1!A1:E"))
    if not rows:
        print("The transfer sheet is empty.")
        sys.exit(1)
    missing = [c for c in SHEET_COLS if c not in rows[0]]
    if missing:
        print(f"Sheet is missing column(s): {missing}")
        sys.exit(1)

    transfers = []
    for row in rows:
        bank = str(row.get("Bank Name") or "").strip()
        account = str(row.get("Account Number") or "").strip()
        name = str(row.get("Account Name") or "").strip()
        if not (bank or account or name):
            continue
        t = {"row_no": row["_row_no"], "bank": bank, "account": account, "name": name,
             "remark": str(row.get("Remark") or "").strip(), "amount": None, "problem": None}
        try:
            if not bank:
                raise ValueError("no bank name")
            if not account.isdigit():
                raise ValueError(f"account '{account}' must be digits only")
            if not name:
                raise ValueError("no account name")
            t["amount"] = parse_amount(row.get("Amount"))
        except ValueError as e:
            t["problem"] = str(e)
        transfers.append(t)
    return transfers


def pick_manual_rows(transfers: list) -> list:
    print("\n" + "═" * 92)
    print("  TRANSFERS IN THE SHEET")
    print("═" * 92)
    for t in transfers:
        tail = cp.npr(t["amount"]) if not t["problem"] else f"✗ {t['problem']}"
        print(f"  row {t['row_no']:>3}  {t['name'][:30]:<30}  {t['account']:>18}  "
              f"{t['bank'][:22]:<22}  {tail}")
    print("═" * 92)
    by_row = {str(t["row_no"]): t for t in transfers}
    while True:
        ans = input("Sheet row number(s) to send, e.g. 3 or 3,5 (q to quit): ").strip()
        if ans.lower() == "q":
            sys.exit(0)
        picked, bad = [], []
        for part in [p.strip() for p in ans.split(",") if p.strip()]:
            t = by_row.get(part)
            if t is None or t["problem"]:
                bad.append(part)
            else:
                picked.append(t)
        if picked and not bad:
            return picked
        print(f"  ⚠  Can't send row(s): {', '.join(bad) or '(none chosen)'}")


# ── Portal: existing recipients ───────────────────────────────────────────────

GRID = '.ag-root[role="grid"]'     # the recipient list is an AG Grid, not a <table>
GRID_COLUMNS = {"sn": "s.n.", "account": "account number", "name": "account name", "bank": "bank name"}


def list_existing(page) -> list:
    page.get_by_text("Select Existing Recipients", exact=True).first.click()
    try:
        page.wait_for_selector(f"{GRID} .ag-center-cols-container .ag-row", timeout=20_000)
    except PWTimeout:
        raise RuntimeError("The saved-recipient list didn't appear after clicking "
                           "'Select Existing Recipients'.")
    page.wait_for_timeout(800)
    grid = page.locator(GRID).first

    # Map header text → AG Grid col-id, so cells are read by column name, not position
    col_ids = {}
    headers = grid.locator(".ag-header-cell[col-id]")
    for i in range(headers.count()):
        text = headers.nth(i).inner_text().strip().casefold()
        if text:
            col_ids[text] = headers.nth(i).get_attribute("col-id")
    missing = [h for h in GRID_COLUMNS.values() if h not in col_ids]
    if missing:
        raise RuntimeError(f"Recipient list is missing column(s) {missing}; found {list(col_ids)}")

    # Each row exists twice in AG Grid (pinned checkbox part + centre part),
    # linked by row-index. Read the text from the centre part.
    rows = grid.locator(".ag-center-cols-container .ag-row[row-index]")
    recips = []
    for i in range(rows.count()):
        row = rows.nth(i)
        cell = {key: row.locator(f'.ag-cell[col-id="{col_ids[header]}"]').first.inner_text().strip()
                for key, header in GRID_COLUMNS.items()}
        cell["row_index"] = row.get_attribute("row-index")
        recips.append(cell)
    recips.sort(key=lambda r: int(r["row_index"]))
    if not recips:
        raise RuntimeError("The saved-recipient list is empty.")

    expected = int(grid.get_attribute("aria-rowcount") or 0) - 1      # minus header row
    if expected > len(recips):
        print(f"\n⚠️  The grid says {expected} recipients but only {len(recips)} are loaded.")
    if page.locator(".ag-paging-panel").count() and page.locator(".ag-paging-panel").first.is_visible():
        print("\n⚠️  The recipient list is paginated — only the current page is listed here.")
    return recips

def choose_existing(recips: list):
    shown = recips
    while True:
        print("\n" + "─" * 92)
        for r in shown:
            print(f"  {r['sn']:>3}  {r['account']:>18}  {r['name'][:34]:<34}  {r['bank']}")
        print("─" * 92)
        ans = input("Pick by S.N., type text to filter, Enter to show all, q to quit: ").strip()
        if ans.lower() == "q":
            return None
        if not ans:
            shown = recips
        elif ans.isdigit() and len(ans) <= 3:
            match = [r for r in recips if r["sn"] == ans]
            if len(match) == 1:
                return match[0]
            print(f"  ⚠  No recipient with S.N. {ans}")
        else:
            key = ans.casefold()
            shown = [r for r in recips
                     if key in f"{r['name']} {r['account']} {r['bank']}".casefold()] or recips
            if shown is recips:
                print(f"  ⚠  Nothing matches '{ans}'")


def _selected_rows(grid) -> set:
    """row-index of every selected row (AG Grid marks both halves of the row)."""
    found = set()
    marked = grid.locator('.ag-row.ag-row-selected, .ag-row[aria-selected="true"]')
    for i in range(marked.count()):
        found.add(marked.nth(i).get_attribute("row-index"))
    if not found:                       # app may keep its own checkbox state instead
        ticked = grid.locator('.ag-row:has(input[type="checkbox"]:checked)')
        for i in range(ticked.count()):
            found.add(ticked.nth(i).get_attribute("row-index"))
    return found


def tick_recipient(page, r: dict):
    grid = page.locator(GRID).first
    idx = r["row_index"]
    row = grid.locator(f'.ag-pinned-left-cols-container .ag-row[row-index="{idx}"]')
    if row.count() == 0:
        row = grid.locator(f'.ag-center-cols-container .ag-row[row-index="{idx}"]')
    box = row.locator('input[type="checkbox"]').first
    if box.count():
        try:
            box.check(timeout=3000)
        except Exception:
            box.click(force=True)
    else:
        row.locator(".ag-cell").first.click()
    page.wait_for_timeout(500)

    selected = _selected_rows(grid)
    if selected != {idx}:
        raise RuntimeError(f"Expected only row {r['sn']} ({r['account']}) to be selected, "
                           f"but the grid shows row-index {sorted(selected) or 'none'}.")
    log.info(f"Selected recipient {r['sn']}: {r['name']} ({r['account']})")

# ── Portal: manual entry ──────────────────────────────────────────────────────

def choose_bank(page, bank: str) -> str:
    # Search on the first word so a near-miss spelling still lists candidates
    return cp.pick_dropdown(
        page, "Creditor Bank Name",
        match=lambda t: t.casefold() == bank.casefold(),
        search=bank.split()[0],
        what=f"creditor bank '{bank}'",
    )

def fill_manual(page, t: dict):
    page.get_by_text("Enter Details Manually", exact=True).first.click()
    page.wait_for_timeout(1000)

    bank_shown = choose_bank(page, t["bank"])
    for labels, value, what in ((ACCT_LABELS, t["account"], "creditor account number"),
                                (NAME_LABELS, t["name"], "creditor account name")):
        field = cp.field_by_label(page, labels)
        if field is None:
            raise RuntimeError(f"Couldn't find the {what} field — send Claude a screenshot "
                               f"of the 'Enter Details Manually' form.")
        cp.type_into(page, field, value)
        if field.input_value().strip() != value:
            raise RuntimeError(f"The {what} field shows '{field.input_value()}', "
                               f"expected '{value}'.")
    return bank_shown


# ── Portal: amount, remark, initiate ──────────────────────────────────────────

def fill_amount_remark(page, amount: Decimal, remark: str) -> Decimal:
    field = cp.field_by_label(page, AMOUNT_LABELS)
    if field is None:
        raise RuntimeError("Couldn't find the Transaction Amount field.")
    cp.type_into(page, field, f"{amount:.2f}")
    shown = cp.amount_shown(field)
    if shown != amount:
        raise RuntimeError(f"Amount field shows '{field.input_value()}', expected {amount:.2f}.")

    box = cp.field_by_label(page, REMARK_LABELS, "input, textarea")
    if box is None:
        raise RuntimeError("Couldn't find the Remark field.")
    cp.type_into(page, box, remark)
    if box.input_value().strip() != remark:
        raise RuntimeError(f"Remark field shows '{box.input_value()}', expected '{remark}'.")
    return shown


def confirm_and_initiate(page, lines: list, dry_run: bool) -> bool:
    cp.screenshot(page, "transfer_prefill")
    print("\n" + "═" * 72)
    print("  FUND TRANSFER — READ BACK FROM THE FORM")
    print("═" * 72)
    for line in lines:
        print(f"  {line}")
    print("═" * 72)

    if dry_run:
        cp.ask_human("DRY RUN — Initiate will not be clicked. Check the browser.")
        return False
    if input("Type 'yes' to click Initiate, anything else to skip: ").strip().lower() != "yes":
        print("  Not initiated.")
        return False

    button = page.get_by_role("button", name="Initiate", exact=True)
    if button.count() != 1:
        raise RuntimeError(f"Expected one Initiate button, found {button.count()}.")
    if not button.is_enabled():
        raise RuntimeError("Initiate is greyed out — the portal hasn't accepted the form. "
                           "Check for red messages in the browser.")
    button.click()
    page.wait_for_timeout(3000)
    cp.screenshot(page, "transfer_initiated")

    expiry = (datetime.now() + timedelta(days=3)).strftime("%A %d %B")
    print("\n  INITIATE CLICKED — NOT YET PAID.")
    print(f"  The approver must approve by {expiry} or it expires.")
    cp.ask_human("Check the browser: deal with any confirmation pop-up and make sure the "
                 "portal reports success.")
    return True


# ── Run ───────────────────────────────────────────────────────────────────────

def run(mode: str, queue: list, dry_run: bool):
    creds = cp.get_creds("initiator")
    done = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(30_000)
        try:
            cp.login(page, "initiator", creds)

            if mode == "existing":
                while True:
                    cp.open_fund_transfer(page)
                    debtor, debtor_name = cp.select_only_debtor(page)
                    r = choose_existing(list_existing(page))
                    if r is None:
                        break
                    amount, remark = ask_amount(), ask_remark()
                    tick_recipient(page, r)
                    shown = fill_amount_remark(page, amount, remark)
                    lines = [f"From   : {debtor}  {debtor_name}",
                             f"To     : {r['name']}",
                             f"         {r['account']}  ({r['bank']})",
                             f"Amount : {cp.npr(shown)}",
                             f"Remark : {remark}"]
                    if confirm_and_initiate(page, lines, dry_run):
                        done.append(f"{r['name']} ({r['account']})  {cp.npr(shown)}")
                    if input("\nAnother transfer? [y/N]: ").strip().lower() != "y":
                        break

            else:
                for i, t in enumerate(queue):
                    print(f"\n── Sheet row {t['row_no']}: {t['name']} ──")
                    cp.open_fund_transfer(page)
                    debtor, debtor_name = cp.select_only_debtor(page)
                    remark = t["remark"] or ask_remark()
                    bank_shown = fill_manual(page, t)
                    shown = fill_amount_remark(page, t["amount"], remark)
                    lines = [f"From   : {debtor}  {debtor_name}",
                             f"To     : {t['name']}",
                             f"         {t['account']}  ({bank_shown})",
                             f"Amount : {cp.npr(shown)}",
                             f"Remark : {remark}"]
                    if confirm_and_initiate(page, lines, dry_run):
                        done.append(f"row {t['row_no']}: {t['name']} ({t['account']})  {cp.npr(shown)}")
                    elif not dry_run and i < len(queue) - 1:
                        if input("Continue with the remaining rows? [y/N]: ").strip().lower() != "y":
                            break

            print("\n" + "═" * 72)
            print(f"  {len(done)} transfer(s) initiated this run — awaiting approval:")
            for d in done:
                print(f"    • {d}")
            print("═" * 72)
            input("\nPress Enter to close the browser … ")

        except (PWTimeout, RuntimeError) as e:
            log.error(f"{type(e).__name__}: {str(e)[:400]}")
            cp.screenshot(page, "transfer_error")
            if done:
                print("\n  Already initiated before the error:")
                for d in done:
                    print(f"    • {d}")
            print("\n  Browser left open so you can see where it stopped.")
            input("  Press Enter to close … ")
            raise
        finally:
            context.close()
            browser.close()


def main():
    cp.setup_logging()
    parser = argparse.ArgumentParser(description="CorporatePAY fund transfer — initiate")
    parser.add_argument("--env", default=".env", help="env file in this folder (default .env)")
    parser.add_argument("--mode", choices=["existing", "manual"], default=None)
    parser.add_argument("--csv", default=None, help="manual mode: read a CSV instead of the sheet")
    parser.add_argument("--dry-run", action="store_true", help="fill everything, never click Initiate")
    args = parser.parse_args()

    print("\n══════════════════════════════════════════")
    print("  CorporatePAY — Fund Transfer: INITIATE")
    print("══════════════════════════════════════════\n")

    cp.load_env(args.env)
    cp.get_creds("initiator")

    mode = args.mode
    if not mode:
        print("  [1] Saved recipient (choose from the portal's list)")
        print("  [2] New recipient details from the Google Sheet")
        mode = {"1": "existing", "2": "manual"}.get(input("\nEnter 1 or 2: ").strip())
        if not mode:
            print("Invalid choice.")
            sys.exit(1)

    # Manual mode reads and checks the sheet BEFORE logging in, so a bad row
    # or a Google sign-in prompt never interrupts a live portal session.
    queue = pick_manual_rows(load_manual_rows(args.csv)) if mode == "manual" else []
    run(mode, queue, args.dry_run)


if __name__ == "__main__":
    main()
