# CorpPayPayments

Salary payments on CorporatePAY for an account with separate initiator and approver logins.

| File | What it does |
|---|---|
| `fund_transfer.py` | Initiator login → Fund Transfer to a saved recipient, or to a new one from the sheet → Initiate |
| `salary_initiate.py` | Initiator login → fills the batch from the sheet → Initiate |
| `salary_approve.py` | Approver login → opens the batch → checks it against what was initiated → Approve |
| `cp_common.py` | Shared login, navigation, helpers |
| `.env` | Both sets of credentials (copy from `.env.example`) |
| `runs/` | Screenshots and the record of the last initiated batch |

## Setup (once)

```bash
conda activate corporatepay
pip install -r requirements.txt     # the existing env already has most of these
cp .env.example .env                # then fill it in
```

## Each run

```bash
python salary_initiate.py           # asks for the remark, shows the batch, wants 'yes'
python salary_approve.py            # shows the batch, verifies on screen, wants 'approve'
```

Each script asks for that login's own Google Authenticator code.

## First time, in this order

1. `python salary_initiate.py --csv test.csv --dry-run` with one small row
2. `python salary_initiate.py --csv test.csv` (a real, small initiate)
3. `python salary_approve.py --dry-run` (opens and checks it, does not approve)
4. `python salary_approve.py`

`test.csv` needs the header `Full Name,Account Number,Salary Amount,Salary Cleared`.

## Fund transfer

```bash
python fund_transfer.py --dry-run            # first time: fills the form, never initiates
python fund_transfer.py                      # asks: saved recipient or sheet
```

- Saved recipient: pick from the list printed in the terminal, then type amount and remark.
- Sheet: columns `Bank Name | Account Number | Account Name | Amount | Remark`.
  Format Account Number as **Plain text** so leading zeros survive. Bank Name must
  be spelled exactly as in the portal's dropdown (case doesn't matter).
- Max NPR 20,00,000 per transaction; anything above is rejected before login.
- Each transfer is read back off the form and needs `yes` before Initiate.
