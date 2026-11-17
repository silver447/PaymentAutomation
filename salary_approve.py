"""
CorporatePAY — Salary Payment, step 2 of 2: APPROVE (approver account)
======================================================================
Logs in as the APPROVER, opens the pending batch's details, and checks them
against runs/last_batch.json (written by salary_initiate.py): every account
number and the batch total must be on the details screen. Only then does it
ask you to type 'approve', and only then does it click Approve.

The approver screens were written without seeing them. Wherever the script
can't find what it expects, it does NOT guess: it asks you to do that one
click in the browser and then carries on.

  python salary_approve.py --dry-run   # open + verify the batch, never click Approve
  python salary_approve.py
"""

import sys
import json
import argparse
from decimal import Decimal
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import cp_common as cp

log = cp.log


def load_batch() -> dict:
    if not cp.LAST_BATCH.exists():
        print(f"No batch record at {cp.LAST_BATCH}.")
        print("Run salary_initiate.py first — the approver checks against its record.")
        sys.exit(1)
    batch = json.loads(cp.LAST_BATCH.read_text())
    age = datetime.now() - datetime.fromisoformat(batch["initiated_at"])
    if age > timedelta(days=3):
        print(f"The recorded batch was initiated {age.days} days ago — it will have expired.")
        sys.exit(1)
    return batch


def amount_forms(value: str) -> list:
    d = Decimal(value)
    return [f"{d:,.2f}", f"{d:.2f}"]


def show_batch(batch: dict):
    print("\n" + "═" * 72)
    print(f"  BATCH TO APPROVE — initiated {batch['initiated_at']}")
    print("═" * 72)
    for i, p in enumerate(batch["payments"], 1):
        print(f"  {i:>2}. {p['name']:<32}  {p['account']:>20}  {Decimal(p['amount']):>12,.2f}")
    print("─" * 72)
    print(f"  {batch['count']} recipients, NPR {Decimal(batch['total']):,.2f}  |  {batch['remark']}")
    print("═" * 72)


def go_to_approve_tab(page):
    """Click the Approve TAB. Only tab/nav elements are considered — never a
    button — so this can't press a real 'Approve' action by accident."""
    if page.locator('text="View Details"').count() > 0:
        return
    tab = page.locator(
        '[role="tab"]:has-text("Approve"), .nav-link:has-text("Approve"), '
        'ul.nav a:has-text("Approve"), .nav-item a:has-text("Approve")'
    )
    if tab.count() >= 1:
        tab.first.click()
        page.wait_for_timeout(2500)
    if page.locator('text="View Details"').count() == 0:
        cp.ask_human("I can't see a pending batch. In the browser, open the Approve "
                     "tab/list so the batch with 'View Details' is showing.")


def open_details(page, batch: dict):
    links = page.locator('text="View Details"')
    n = links.count()
    if n == 1:
        links.first.click()
    elif n > 1:
        # Several pending batches: pick the row showing our total, if exactly one does.
        rows = page.locator('tr:has-text("View Details")')
        hits = [i for i in range(rows.count())
                if any(f in rows.nth(i).inner_text() for f in amount_forms(batch["total"]))]
        if len(hits) == 1:
            rows.nth(hits[0]).locator('text="View Details"').first.click()
        else:
            cp.ask_human(f"There are {n} pending batches and I can't tell which is ours. "
                         f"Click 'View Details' on the NPR {Decimal(batch['total']):,.2f} one.")
    else:
        cp.ask_human("Click 'View Details' on the batch to approve.")
    page.wait_for_timeout(3000)


def verify_details(page, batch: dict) -> bool:
    """Every account number and the batch total must appear on the details screen."""
    text = page.locator("body").inner_text()
    ok = True
    print("\n  Checking the details screen against the initiated batch:")
    for p in batch["payments"]:
        has_acct = p["account"] in text
        has_amt = any(f in text for f in amount_forms(p["amount"]))
        mark = "✓" if has_acct and has_amt else ("✗ ACCOUNT NOT FOUND" if not has_acct
                                                 else "? amount not found in expected format")
        print(f"    {p['account']:>20}  {Decimal(p['amount']):>12,.2f}  {mark}")
        ok = ok and has_acct
    has_total = any(f in text for f in amount_forms(batch["total"]))
    print(f"    Batch total NPR {Decimal(batch['total']):,.2f}  {'✓' if has_total else '✗ NOT FOUND'}")
    return ok and has_total


def click_approve(page) -> bool:
    scope = page.locator(".modal.show, .modal.d-block").last
    if scope.count() == 0:
        scope = page
    button = scope.get_by_role("button", name="Approve", exact=True)
    if button.count() != 1:
        return False
    button.click()
    return True


def run(batch: dict, dry_run: bool):
    creds = cp.get_creds("approver")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(30_000)
        try:
            cp.login(page, "approver", creds)
            cp.open_salary_payment(page)
            go_to_approve_tab(page)
            open_details(page, batch)
            verified = verify_details(page, batch)
            cp.screenshot(page, "approve_details")

            if not verified:
                print("\n⛔ The details screen does NOT match the initiated batch.")
                print("   The script will not click Approve. Read the screen yourself;")
                print("   if this is just a formatting difference, send Claude the screenshot.")
                input("   Press Enter to close the browser … ")
                return

            if dry_run:
                print("\n── DRY RUN ── details verified; Approve was NOT clicked.")
                input("  Press Enter to close the browser … ")
                return

            print("\n  Details match. Approving sends the money.")
            if input("  Type 'approve' to click Approve, anything else to stop: ").strip().lower() != "approve":
                print("  Stopped — nothing was approved.")
                input("  Press Enter to close the browser … ")
                return

            if click_approve(page):
                log.info("Clicked Approve")
            else:
                cp.ask_human("I couldn't find a single unambiguous Approve button — "
                             "click Approve in the browser yourself.")
            page.wait_for_timeout(3000)
            cp.screenshot(page, "approve_done")

            print("\n  If the portal is now asking for a confirmation, password or OTP,")
            print("  complete it in the browser. Confirm the batch status before closing.")
            input("  Press Enter to close the browser … ")
            done = cp.LAST_BATCH.with_name(f"batch_{datetime.now():%Y%m%d_%H%M%S}_approved.json")
            cp.LAST_BATCH.rename(done)       # so the same batch can't be approved twice
            log.info(f"Batch record archived → {done.name}")

        except (PWTimeout, RuntimeError) as e:
            log.error(f"{type(e).__name__}: {str(e)[:400]}")
            cp.screenshot(page, "approve_error")
            print("\n  Browser left open so you can see where it stopped.")
            input("  Press Enter to close … ")
            raise
        finally:
            context.close()
            browser.close()


def main():
    cp.setup_logging()
    parser = argparse.ArgumentParser(description="CorporatePAY salary payment — approve")
    parser.add_argument("--env", default=".env", help="env file in this folder (default .env)")
    parser.add_argument("--dry-run", action="store_true", help="verify the batch, never click Approve")
    args = parser.parse_args()

    print("\n══════════════════════════════════════════")
    print("  CorporatePAY — Salary Payment: APPROVE")
    print("══════════════════════════════════════════\n")

    cp.load_env(args.env)
    cp.get_creds("approver")
    batch = load_batch()
    show_batch(batch)
    run(batch, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
