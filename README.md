# E-Commerce Inventory Management & Pricing Bot

Keeps pet listings stocked and priced across several accounts on the [StarPets](https://starpets.gg) marketplace. It lists items from inventory, follows the market floor price, and refuses to chase a price crash.

## Features

- **Repricing:** checks listings about every 10 seconds and moves them to the current floor price, never below $0.25.
- **Price crash protection:** compares each floor against the median of its last 10 readings.
  - A floor under 60% of that median is treated as manipulation and ignored.
  - Any other drop bigger than $0.10 or 3%, whichever is larger, is only followed after holding for 3 checks.
- **Inventory:** lists target items automatically, up to 5 pet names per account and 50 listings per variant, with neon and mega neon going first.
- **Multiple accounts:** runs one worker per StarPets login, covering every account linked to it.
- **Account recovery:** relinks dead cookies and re-syncs stale or frozen accounts on its own.
- **Rate limits:** stays under 25 API calls a minute and backs every account off when the server returns HTTP 429.
- **Token refresh:** renews each login's access token every 5 minutes.

## Requirements

- Windows, since the hotkeys use `msvcrt`
- Python 3 with `requests`

```
pip install requests
```

## Setup

1. Fill in `starpets_accounts.json`, adding one entry per login. To get a token, open starpets.gg, press F12, go to Network, and copy the `authorization` header from any request to `market.apineural.com`.

   ```json
   [
     {
       "name": "Main",
       "token": "Bearer ...",
       "refresh_token": "..."
     }
   ]
   ```

2. Edit `TARGET_PETS` at the top of `starpets_relist.py`. The order sets the priority.

3. Optional: add a `collect.txt` next to the scripts, with one `username:password:cookie` per line.

   > **Warning:** when `collect.txt` exists, every login syncs to it at startup. Linked accounts that aren't in the file get unlinked, and accounts in the file that aren't linked get linked. If the file is missing or empty, the sync is skipped.

4. Run it:

   ```
   python starpets_relist.py
   ```

   After a few seconds it also opens `starpets_pricer.py` in a second window, which only reprices.

## Controls

| Key | Action |
|---|---|
| Ctrl+R | Refresh inventory on all accounts, then list |
| Ctrl+T | Unlist everything, freeze accounts, and pause (see known issues) |
| Ctrl+G | Freeze all accounts without unlisting, and pause |
| Ctrl+V | Unfreeze all accounts and resume |
| Ctrl+C | Quit |

## Known issues

- **Ctrl+T doesn't remove listings.** The pause it sets also stops the delete step, so accounts that still have listings stay listed, and it gives up after 20 passes.
- **Ctrl+G skips some accounts.** Accounts with an active sale or that aren't active are skipped rather than frozen. The reason is printed in the log.

## Keep your credentials private

`starpets_accounts.json` only holds placeholders in this repo. Your real tokens, and `collect.txt` with its passwords and cookies, give full access to those accounts, so never commit them.

## Files

| File | Purpose |
|---|---|
| `starpets_relist.py` | The main bot |
| `starpets_pricer.py` | Reprice-only mode, started automatically in its own window |
| `starpets_accounts.json` | Login tokens (placeholders here) |
