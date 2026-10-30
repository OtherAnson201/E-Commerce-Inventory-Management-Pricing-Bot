import os, re, sys, time, json, requests, threading, traceback, subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# turn on ansi colors on windows
if os.name == "nt":
    import ctypes
    _kernel = ctypes.windll.kernel32
    _kernel.SetConsoleMode(_kernel.GetStdHandle(-11), 7)

class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GOLD   = "\033[38;5;220m"
    CYAN   = "\033[38;5;51m"
    GREEN  = "\033[38;5;82m"
    RED    = "\033[38;5;196m"
    ORANGE = "\033[38;5;208m"
    GREY   = "\033[38;5;244m"
    WHITE  = "\033[38;5;255m"
    YELLOW = "\033[38;5;226m"

# wrap text in a color code
def c(color, text):
    return f"{color}{text}{C.RESET}"

TARGET_PETS = [
    "Oakee Wizard",
    "Oakee Knight",
    "Stygian Owl",
    "Strawberry Shortcake Ducky",
    "Sushi Penguin",
    "Unicorn Ducky",
    "Dragonfruit Fox",
    "Dango Penguins",
    "Admin Abuse Egg",
    "Pilot Gull",
    "Bison",
    "Nurse Shark",
    "Alicorn",
    "Purrowl",
    "Glormy Crab",
    "Sea Turtle",
    "Silverback Gorilla",
]

PUMPING_PRIORITY = {
    "normal":    0,
    "neon":      1,
    "mega_neon": 2,
}

VARIANT_CAP   = 50
PET_NAME_CAP  = 5
API_BATCH     = 10
CURRENCY      = "usd"

CHECK_INTERVAL      = 10
LINKED_REFRESH      = 300
RELOAD_CONCURRENCY  = 6
STALE_GRACE_POLLS   = 2
UNLIST_POLL_SECONDS = 5
UNLIST_MAX_PASSES   = 20
CTRL_R_MAX_WAIT     = 90
LOG_REPEAT_SECONDS  = 300

MAX_DROP           = 0.10
MAX_DROP_PCT       = 0.03
FLOOR_HISTORY      = 10
DROP_CONFIRM_TICKS = 3
CRASH_FLOOR_PCT    = 0.60
CRASH_STEP_PCT     = 0.10
MIN_LIST_PRICE     = 0.25

RATE_WINDOW            = 60.0
RATE_LIMIT             = 25
FREEZE_LIMIT           = 12   # stay under 15/min
BACKOFF_PROBE_INTERVAL = 15
API_RETRY_ATTEMPTS     = 4
API_RETRY_DELAY        = 3

DIR          = Path(__file__).parent
ACCS_FILE    = DIR / "starpets_accounts.json"
COLLECT_FILE = DIR / "collect.txt"
API          = "https://market.apineural.com"
LINKED_URL   = f"{API}/api/lnkd-rbx-accounts?withItemsCount=1"

_print_lock = threading.Lock()
_last_said  = {}

_paused       = threading.Event()
_tick_lock    = threading.Lock()
_tick_running = threading.Event()

_floor_lock   = threading.Lock()
_floor_hist   = defaultdict(list)
_pending_drop = {}

_backoff_lock    = threading.Lock()
_backoff_active  = False
_backoff_until   = 0.0
_backoff_probing = False

_rate_lock    = threading.Lock()
_rate_calls   = []
_freeze_lock  = threading.Lock()
_freeze_calls = []

# timestamped colored print, thread safe
def log(account_name, color, msg):
    with _print_lock:
        print(c(C.GREY, f"[{time.strftime('%H:%M:%S')}] ") +
              c(C.BOLD, f"[{account_name}] ") +
              c(color, msg), flush=True)

# like log, but the same line for the same account only prints every few minutes
def log_throttled(account_name, color, msg):
    now = time.monotonic()
    with _print_lock:
        last = _last_said.get((account_name, msg))
        if last is not None and now - last < LOG_REPEAT_SECONDS:
            return
        _last_said[(account_name, msg)] = now
    log(account_name, color, msg)

# sort key so neon/mega list before normal
def item_priority(item):
    pumping = (item.get("pumping") or "normal").lower()
    return PUMPING_PRIORITY.get(pumping, 0)

# true if the floor is way under the real price (someone crashing it)
def is_manipulation(ref, floor):
    if not ref or ref <= 0:
        return False
    return floor < ref * CRASH_FLOOR_PCT

# how far under the reference price were allowed to follow
def max_drop_for(ref):
    if not ref or ref <= 0:
        return MAX_DROP
    return max(MAX_DROP, ref * MAX_DROP_PCT)

# flip every account into 429 backoff
def _trigger_backoff(name=None, retry_after=None):
    global _backoff_active, _backoff_until
    with _backoff_lock:
        if retry_after:
            _backoff_until = max(_backoff_until, time.monotonic() + retry_after)
        if not _backoff_active:
            _backoff_active = True
            detail = (f"server asked for {retry_after:.0f}s" if retry_after
                      else f"re-checking every {BACKOFF_PROBE_INTERVAL}s")
            log(name or "RATE", C.RED,
                f"HTTP 429 — backing off ALL accounts, {detail}")

# read the Retry-After header in seconds, or none
def _parse_retry_after(r):
    try:
        secs = float(r.headers.get("Retry-After") or 0)
    except (TypeError, ValueError):
        return None
    return secs if 0 < secs <= 3600 else None

# ping the api to see if the 429 is over
def _probe_rate_limit_cleared():
    global _backoff_until
    try:
        r = requests.get(LINKED_URL, timeout=10)
    except requests.exceptions.RequestException:
        return False
    if r.status_code != 429:
        return True
    ra = _parse_retry_after(r)
    if ra:
        with _backoff_lock:
            _backoff_until = max(_backoff_until, time.monotonic() + ra)
    return False

# block here until the 429 backoff lifts
def _check_backoff():
    global _backoff_active, _backoff_until, _backoff_probing
    while True:
        with _backoff_lock:
            if not _backoff_active:
                return
            wait_left = _backoff_until - time.monotonic()
            i_probe = not _backoff_probing and wait_left <= 0
            if i_probe:
                _backoff_probing = True

        if wait_left > 0:
            time.sleep(min(wait_left, BACKOFF_PROBE_INTERVAL))
            continue

        if not i_probe:
            time.sleep(1)
            continue

        try:
            cleared = _probe_rate_limit_cleared()
        finally:
            with _backoff_lock:
                _backoff_probing = False

        if cleared:
            with _backoff_lock:
                _backoff_active = False
                _backoff_until = 0.0
            log("RATE", C.GREEN, "429 backoff cleared — resuming")
            return

        time.sleep(BACKOFF_PROBE_INTERVAL)

# wait until this rolling window has room for one more call
def _window_wait(lock, calls, limit):
    while True:
        with lock:
            now = time.monotonic()
            while calls and calls[0] < now - RATE_WINDOW:
                calls.pop(0)
            if len(calls) < limit:
                calls.append(time.monotonic())
                return
            sleep_for = RATE_WINDOW - (now - calls[0]) + 0.1
        if sleep_for > 0:
            time.sleep(sleep_for)

# wait so we stay under the api rate limit
def _rate_wait():
    _check_backoff()
    _window_wait(_rate_lock, _rate_calls, RATE_LIMIT)

# same wait but for the slower freeze endpoint
def _freeze_rate_wait():
    _window_wait(_freeze_lock, _freeze_calls, FREEZE_LIMIT)

# run an api call with retries and 429 handling
def _api_call(fn, name=None, retries=API_RETRY_ATTEMPTS, backoff_on_429=True):
    for attempt in range(retries):
        if attempt > 0:
            _rate_wait()
        try:
            r = fn()
            if backoff_on_429 and r.status_code == 429:
                _trigger_backoff(name, retry_after=_parse_retry_after(r))
            return r
        except (requests.exceptions.ReadTimeout, requests.exceptions.ChunkedEncodingError) as e:
            if name:
                log(name, C.ORANGE, f"API error ({type(e).__name__}) — not retrying")
            return None
        except requests.exceptions.RequestException as e:
            if attempt < retries - 1:
                time.sleep(API_RETRY_DELAY)
            elif name:
                log(name, C.RED, f"API call failed after {retries} attempts ({type(e).__name__}): {e}")
        except Exception as e:
            if name:
                log(name, C.RED, f"Unexpected error in API call ({type(e).__name__}): {e}\n{traceback.format_exc()}")
            return None
    return None

# response body as json, empty dict if it isnt json
def _json(r):
    try:
        return r.json()
    except Exception:
        return {}

# short reason a request failed, for logs
def _why(r):
    return "no response" if r is None else f"HTTP {r.status_code} {r.text[:150]}"

# read the accounts json, empty list if missing or bad
def load_accounts():
    if not ACCS_FILE.exists():
        return []
    try:
        return json.loads(ACCS_FILE.read_text())
    except Exception:
        return []

# write the accounts json
def save_accounts(accs):
    ACCS_FILE.write_text(json.dumps(accs, indent=2))

# first run, make the accounts file or take one token
def setup_accounts():
    accs = load_accounts()
    if accs:
        return accs

    print(c(C.ORANGE, "\nNo accounts found."))
    print(c(C.WHITE,  f"Create the file manually at: {ACCS_FILE}"))
    print(c(C.GREY,   "Format:"))
    print(c(C.CYAN,   '['))
    print(c(C.CYAN,   '  {"name": "Main",  "token": "Bearer eyJ..."},'))
    print(c(C.CYAN,   '  {"name": "Alt1",  "token": "Bearer eyJ..."}'))
    print(c(C.CYAN,   ']'))
    print()
    print(c(C.GREY, "How to get your token:"))
    print(c(C.GREY, "  1. Open starpets.gg → F12 → Network tab → Fetch/XHR"))
    print(c(C.GREY, "  2. Click any request to market.apineural.com"))
    print(c(C.GREY, '  3. Copy ONLY the "authorization" header value (starts with Bearer)'))
    print()

    print(c(C.WHITE, "Or paste ONE token now to get started (you can add more to the JSON later):"))
    token = input(c(C.CYAN, "  Bearer token: ")).strip()
    if not token:
        print(c(C.RED, "No token provided. Create starpets_accounts.json manually and re-run."))
        sys.exit(1)
    if not token.startswith("Bearer "):
        token = "Bearer " + token

    accs = [{"name": "Main", "token": token}]
    save_accounts(accs)
    print(c(C.GREEN, f"\n  Saved to {ACCS_FILE.name} — add more accounts by editing the file.\n"))
    return accs

# a requests session with the starpets headers
def make_session(token):
    s = requests.Session()
    s.headers.update({
        "accept":           "*/*",
        "accept-language":  "en-CA,en;q=0.9",
        "authorization":    token,
        "content-type":     "application/json",
        "origin":           "https://starpets.gg",
        "referer":          "https://starpets.gg/",
        "sec-ch-ua":        '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest":   "empty",
        "sec-fetch-mode":   "cors",
        "sec-fetch-site":   "cross-site",
        "user-agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    })
    return s

# same headers but asking for an ndjson stream
def stream_headers(s):
    h = dict(s.headers)
    h["content-type"] = "application/octet-stream"
    return h

# parse newline separated json into a list
def parse_ndjson(text):
    if not text or not text.strip():
        return []
    items = []
    for line in text.strip().split("\n"):
        try:
            items.append(json.loads(line))
        except Exception:
            pass
    return items

# lowercase and squash spaces so names compare
def _norm_item_name(name):
    return re.sub(r"\s+", " ", str(name or "")).strip().lower()

_TARGET_NORM = {_norm_item_name(t) for t in TARGET_PETS}

# true if the item is one of our target pets
def is_target(item):
    return _norm_item_name(item.get("name")) in _TARGET_NORM

# fetch this accounts listed target pets, reserved ones left out
def get_listed_targets(s, account_id, name):
    _rate_wait()
    r = _api_call(lambda: s.get(f"{API}/api/v2/store/items/u/{account_id}/c/{CURRENCY}",
                                headers=stream_headers(s), timeout=(10, 60)), name)
    if r is None or r.status_code == 429:  # _api_call already started the backoff
        return None
    if r.status_code not in (200, 304):
        log(name, C.RED, f"ERROR fetching listings: HTTP {r.status_code}")
        return None
    items = parse_ndjson(r.content.decode("utf-8", errors="replace"))
    return [i for i in items if is_target(i) and (i.get("reserveLevel") or 0) == 0]

# id, name and state of each account in a linked accounts response
def _parse_linked(r):
    linked = []
    for acc in (_json(r).get("accounts") or []):
        state    = acc.get("stateOfLinked") if acc.get("stateOfLinked") is not None else acc.get("state")
        username = (acc.get("robloxAccountName") or acc.get("robloxUsername") or
                    acc.get("username") or acc.get("name") or str(acc["id"]))
        linked.append({"id": acc["id"], "name": username, "state": state})
    return linked

# list the roblox accounts linked to this login
def get_linked_roblox_accounts(s, login_name):
    _rate_wait()
    r = _api_call(lambda: s.get(LINKED_URL, timeout=15), login_name)
    if r is None:
        return []
    if r.status_code not in (200, 304):
        log(login_name, C.RED, f"Failed to fetch linked accounts: HTTP {r.status_code}")
        return []
    return _parse_linked(r)

# read one linked accounts state number
def get_account_state(s, account_id):
    _rate_wait()
    r = _api_call(lambda: s.get(LINKED_URL, timeout=15))
    if r is None or r.status_code not in (200, 304):
        return None
    return next((a["state"] for a in _parse_linked(r) if str(a["id"]) == str(account_id)), None)

# turn a state number into a readable word
def state_label(state):
    if state is None:   return "UNKNOWN"
    if state == 0:      return "ERROR"
    if state == 1:      return "NORMAL"
    if state == 2:      return "LINKED/ACTIVE"
    if state == 3:      return "STALE_INVENTORY"
    if state == 4:      return "FROZEN"
    if state == 5:      return "INV_LOADING"
    if state == 9:      return "UNKNOWN(9)/SETUP"
    return f"UNKNOWN({state})"

# read user:pass:cookie lines out of collect.txt
def load_collect_file():
    result = {}
    if not COLLECT_FILE.exists():
        return result
    try:
        for line in COLLECT_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":", 2)
            if len(parts) == 3:
                user, _pass, cookie = parts
                result[user.strip().lower()] = cookie.strip()
    except Exception:
        pass
    return result

# relink a roblox account using its saved cookie
def relink_account_by_cookie(s, roblox_username, login_name):
    cookie = load_collect_file().get(roblox_username.strip().lower())
    if not cookie:
        log_throttled(login_name, C.ORANGE, f"No cookie found for {roblox_username} in collect.txt — cannot relink")
        return False
    return link_account_by_cookie(s, cookie, login_name)

# unlink a roblox account by id
def unlink_account(s, account_id, name):
    _rate_wait()
    r = _api_call(lambda: s.delete(f"{API}/api/lnkd-rbx-accounts/{account_id}", timeout=20), name)
    if r is not None and r.status_code in (200, 201, 204):
        return True
    log(name, C.RED, f"Unlink failed: {_why(r)}")
    return False

# link a roblox account from a cookie
def link_account_by_cookie(s, cookie, name):
    _rate_wait()
    r = _api_call(lambda: s.post(f"{API}/api/lnkd-rbx-accounts/link/cookie",
                                 json={"cookie": cookie}, timeout=20), name)
    if r is not None and r.status_code in (200, 201):
        return True
    log(name, C.RED, f"Link failed: {_why(r)}")
    return False

# make the linked accounts match collect.txt
def sync_accounts_with_collect(s, login_name):
    collect = load_collect_file()
    if not collect:
        log(login_name, C.ORANGE, "[sync] collect.txt empty/missing — skipping (refusing to mass-unlink)")
        return

    linked = get_linked_roblox_accounts(s, login_name)
    linked_by_user = {a["name"].strip().lower(): a for a in linked}

    to_unlink = [a for u, a in linked_by_user.items() if u not in collect]
    to_link   = [(u, ck) for u, ck in collect.items() if u not in linked_by_user]

    if to_unlink and len(to_unlink) == len(linked) and len(collect) < len(linked):
        log(login_name, C.RED,
            f"[sync] would unlink ALL {len(linked)} linked accounts but collect.txt only "
            f"has {len(collect)} entries — aborting unlink as a safety check")
        to_unlink = []

    n_unlinked = sum(unlink_account(s, a["id"], f"{login_name}/{a['name']}") for a in to_unlink)

    n_linked = 0
    for user, cookie in to_link:
        rname = f"{login_name}/{user}"
        if not cookie:
            log(rname, C.ORANGE, "[sync] no cookie in collect.txt line — skipping")
            continue
        if link_account_by_cookie(s, cookie, rname):
            n_linked += 1

    if to_unlink or to_link:
        log(login_name, C.GOLD,
            f"[sync] {n_unlinked}/{len(to_unlink)} unlinked, {n_linked}/{len(to_link)} linked")

# tell starpets to activate this account in the current game
def activate_account(s, account_id, name):
    _rate_wait()
    r = _api_call(lambda: s.patch(f"{API}/api/lnkd-rbx-accounts/{account_id}/activate-in-current-game",
                                  json={}, timeout=15), name)
    if r is not None and (r.ok or _json(r).get("statusCode") == "LINKED_RBX_ACCOUNT_IS_NOT_INACTIVE"):
        return True
    log(name, C.RED, f"Activate failed: {_why(r)}")
    return False

# trigger an inventory sync, activating first if needed
def click_reload(s, account_id, name):
    for attempt in range(2):
        _rate_wait()
        r = _api_call(lambda: s.post(f"{API}/api/inventory/{account_id}/sync", json={}, timeout=20), name)
        if r is None:
            return False
        if r.ok:
            return True
        sc = _json(r).get("statusCode", "")
        if sc == "LINKED_RBX_ACCOUNT_IS_NOT_ACTIVE":
            if attempt == 0:
                activate_account(s, account_id, name)
                time.sleep(3)
                continue
            log_throttled(name, C.GREY, "Reload skipped — account still loading, retrying next tick")
        elif sc == "LIMIT_LINKED_RBX_ACCOUNT_INVENTORIES":
            log_throttled(name, C.ORANGE, "Reload on cooldown — retrying next tick")
        else:
            log(name, C.RED, f"Reload failed: {_why(r)}")
        return False

# poll until the reload finishes or times out
def wait_for_reload(s, account_id, name, max_wait=300):
    deadline = time.monotonic() + max_wait
    stale_polls = 0
    while time.monotonic() < deadline:
        if _paused.is_set():
            return
        time.sleep(10)
        if _paused.is_set():
            return
        state = get_account_state(s, account_id)
        if state == 5:
            stale_polls = 0
        elif state == 3:
            stale_polls += 1
            if stale_polls > STALE_GRACE_POLLS:
                log(name, C.ORANGE,
                    f"Still stale (state=3) after {stale_polls} polls — sync didn't take, "
                    f"stopping wait so it can be re-triggered")
                return
        else:
            return
    log(name, C.ORANGE, f"Reload still pending after {max_wait}s — continuing anyway")

# fetch the accounts current inventory
def get_fresh_inventory(s, account_id, name):
    _rate_wait()
    r = _api_call(lambda: s.get(f"{API}/api/v2/inventory/items/{account_id}",
                                headers=stream_headers(s), timeout=(10, 60)), name)
    if r is None or r.status_code == 429:  # _api_call already started the backoff
        return []
    if r.status_code not in (200, 304):
        log(name, C.RED, f"ERROR fetching inventory: HTTP {r.status_code}")
        return []
    return parse_ndjson(r.content.decode("utf-8", errors="replace"))

# pull the floor/minimum price out of an item
def get_item_minimum(item):
    price_block = item.get("price")
    if isinstance(price_block, dict):
        minimum = price_block.get("minimum")
        if isinstance(minimum, dict):
            val = minimum.get(CURRENCY, 0)
            if val:
                return float(val)
        if isinstance(minimum, (int, float)) and minimum > 0:
            return float(minimum)
    return 0.0

# pull what the item is currently listed at
def get_current_listed_price(item):
    price_block = item.get("price")
    if isinstance(price_block, dict):
        cur = price_block.get("current")
        if isinstance(cur, (int, float)):
            return float(cur)
        if isinstance(cur, dict):
            return float(cur.get(CURRENCY, 0) or 0)
    if isinstance(price_block, (int, float)):
        return float(price_block)
    return 0.0

# median of a list, none if empty
def _median(values):
    v = sorted(values)
    n = len(v)
    if n == 0:
        return None
    mid = n // 2
    return v[mid] if n % 2 else (v[mid - 1] + v[mid]) / 2.0

# decide if a floor drop is real or someone crashing it
def check_floor_drop(product_id, floor):
    if floor <= 0:
        return True, None
    key = str(product_id)
    with _floor_lock:
        hist = _floor_hist[key]
        ref  = _median(hist)
        if ref is not None and (ref - floor) > max_drop_for(ref):
            if is_manipulation(ref, floor):
                _pending_drop.pop(key, None)
                return False, ref

            pending = _pending_drop.get(key)
            if pending is not None and floor <= pending[0]:
                pending[1] += 1
            else:
                pending = _pending_drop[key] = [floor, 1]
            if pending[1] >= DROP_CONFIRM_TICKS:
                del _pending_drop[key]
                hist.clear()
                hist.append(max(floor, ref - ref * CRASH_STEP_PCT))
                return True, ref
            return False, ref
        _pending_drop.pop(key, None)
        hist.append(floor)
        if len(hist) > FLOOR_HISTORY:
            del hist[:-FLOOR_HISTORY]
    return True, ref

# refetch and confirm a price update actually stuck
def _verify_prices_applied(s, account_id, chunk, name):
    listed = get_listed_targets(s, account_id, name)
    if not listed:
        return False
    by_id = {str(i.get("id")): i for i in listed}
    for u in chunk:
        item = by_id.get(str(u["id"]))
        if item is None:
            return False
        if round(get_current_listed_price(item), 2) != round(u["price"], 2):
            return False
    return True

# push price changes in batches, retry on fail
def update_prices(s, updates, name, account_id):
    for i in range(0, len(updates), API_BATCH):
        if _paused.is_set():
            return False
        chunk = updates[i:i + API_BATCH]
        payload = {"items": [{"id": u["id"], "price": round(u["price"], 2)} for u in chunk], "currency": CURRENCY}
        for _ in range(3):
            _rate_wait()
            r = _api_call(lambda: s.patch(f"{API}/api/store/items/update", json=payload, timeout=(10, 60)), name)
            if r is not None and r.status_code in (200, 304):
                break
            if r is None and _verify_prices_applied(s, account_id, chunk, name):
                break
            time.sleep(3)
        else:
            log(name, C.RED, f"ERROR updating prices: gave up after 3 attempts ({_why(r)})")
            return False
    return True

# the raw freeze/unfreeze call, waits out rate limits
def _set_frozen(s, account_id, name, freeze):
    r = None
    for _ in range(3):
        _freeze_rate_wait()
        r = _api_call(lambda: s.patch(f"{API}/api/lnkd-rbx-accounts/{account_id}/frozen",
                                      json={"freeze": freeze}, timeout=15), name,
                      retries=1, backoff_on_429=False)
        if r is None or r.status_code != 429:
            return r
        reset = int(r.headers.get("x-ratelimit-reset", 60))
        log(name, C.ORANGE, f"{'Freeze' if freeze else 'Unfreeze'} rate limited — waiting {reset}s")
        time.sleep(reset + 1)
    return r

# unfreeze an account, true if its no longer frozen
def unfreeze_account(s, account_id, name):
    r = _set_frozen(s, account_id, name, False)
    if r is None:
        return False
    if r.status_code in (200, 304) or _json(r).get("statusCode") == "LINKED_RBX_ACCOUNT_IS_NOT_FROZEN":
        return True
    log(name, C.RED, f"Unfreeze failed: {_why(r)}")
    return False

# freeze an account, clearing or activating if it complains
def freeze_account(s, account_id, name):
    r = _set_frozen(s, account_id, name, True)
    if r is None:
        log(name, C.RED, f"Freeze failed: {_why(r)}")
        return False
    if r.ok:
        return True

    sc = _json(r).get("statusCode", "")
    if sc == "LINKED_RBX_ACCOUNT_HAS_RESERVED_ITEMS":
        to_remove = [str(i["id"]) for i in get_listed_targets(s, account_id, name) or []]
        log(name, C.ORANGE, f"Has reserved items — withdrawing {len(to_remove)} non-reserved listing(s) first")
        for i in range(0, len(to_remove), API_BATCH):
            chunk = to_remove[i:i + API_BATCH]
            _rate_wait()
            _api_call(lambda: s.delete(f"{API}/api/store/items/remove", json={"items": chunk}, timeout=20), name)
    elif sc == "LINKED_RBX_ACCOUNT_IS_NOT_ACTIVE":
        if not activate_account(s, account_id, name):
            return False
        time.sleep(1)
    else:
        log(name, C.RED, f"Freeze failed: {_why(r)}")
        return False

    r = _set_frozen(s, account_id, name, True)
    if r and r.ok:
        return True
    log(name, C.RED, f"Freeze failed after retry ({sc}): {_why(r)}")
    return False

# list items for sale, activating and retrying as needed
def list_from_inventory(s, items, account_id, name):
    if not items:
        return 0
    payload = {
        "linkedRobloxAccountId": str(account_id),
        "items": [{"id": str(i["id"]), "price": round(float(i["price"]), 2)} for i in items],
        "currency": CURRENCY,
    }
    activated = False
    for _ in range(5):
        _rate_wait()
        r = _api_call(lambda: s.post(f"{API}/api/store/items/add", json=payload, timeout=20), name)
        if r is None:
            return 0
        if r.status_code in (200, 304):
            return len(items)
        sc = _json(r).get("statusCode", "")
        if sc == "LINKED_RBX_ACCOUNT_IS_NOT_ACTIVE":
            if not activated:
                activate_account(s, account_id, name)
                activated = True
            if get_account_state(s, account_id) in (3, 5):
                wait_for_reload(s, account_id, name, max_wait=120)
            else:
                time.sleep(5)
            continue
        if sc == "NOT_FOUND" and len(items) > 1:
            return sum(list_from_inventory(s, [item], account_id, name) for item in items)
        log(name, C.RED, f"ERROR listing: {_why(r)}")
        return 0
    log(name, C.RED, "ERROR listing: account still inactive after 5 attempts — skipping")
    return 0

# reprice all listed targets down to their floor
def tick_prices(s, account_id, name):
    listed = get_listed_targets(s, account_id, name)
    if not listed:
        return listed

    variant_decision = {}
    for item in listed:
        pid = item.get("productId")
        if pid not in variant_decision:
            floor = get_item_minimum(item)
            price = round(max(floor, MIN_LIST_PRICE), 2) if floor > 0 else 0
            variant_decision[pid] = (*check_floor_drop(pid, price), price)

    to_update = []
    no_floor = []
    skipped_drop = {}
    for item in listed:
        pid     = item.get("productId")
        current = get_current_listed_price(item)
        accepted, ref, price = variant_decision[pid]
        if price <= 0:
            no_floor.append(item.get("name", pid))
        elif not accepted:
            skipped_drop[pid] = (item_label(item), ref, price)
        elif round(current, 2) != price:
            to_update.append({"id": item["id"], "price": price, "from": current})

    for label, ref, newfloor in skipped_drop.values():
        log_throttled(name, C.YELLOW,
                      f"[drop guard] {label}: floor ${ref:.2f} → ${newfloor:.2f} "
                      f"(-${ref - newfloor:.2f} > ${max_drop_for(ref):.2f}) — keeping current price")
    if no_floor:
        log_throttled(name, C.ORANGE,
                      f"No floor price for {len(no_floor)} item(s): {', '.join(str(x) for x in no_floor[:5])}"
                      f"{'...' if len(no_floor) > 5 else ''}")

    if to_update:
        grps = defaultdict(int)
        for u in to_update:
            grps[f"${u['from']}→${u['price']}"] += 1
        summary = ", ".join(f"{n}x {k}" for k, n in grps.items())
        if update_prices(s, to_update, name, account_id):
            log(name, C.CYAN, f"Repriced {len(to_update)}/{len(listed)}: {summary}")

    return listed

# readable 'pumping name age' label for an item
def item_label(item):
    pumping = (item.get("pumping") or "normal").lower()
    age     = (item.get("age") or item.get("stage") or "").strip()
    iname   = item.get("name", "?")
    rarity  = pumping if pumping != "normal" else ""
    parts   = [p for p in [rarity, iname, age] if p]
    return " ".join(parts)

# withdraw listings by id in batches, stops as soon as were paused
def _delete_listings(s, account_id, name, ids):
    ids = [str(i) for i in ids]
    for i in range(0, len(ids), API_BATCH):
        if _paused.is_set():
            break
        _rate_wait()
        _api_call(lambda chunk=ids[i:i + API_BATCH]: s.delete(
            f"{API}/api/store/items/remove", json={"items": chunk}, timeout=20), name)

# pick which pet names to keep within the cap
def select_allowed_names(listed, inventory):
    pet_order = {_norm_item_name(pet): idx for idx, pet in enumerate(TARGET_PETS)}
    present = {_norm_item_name(i.get("name")) for i in [*listed, *inventory] if is_target(i)}
    return sorted(present, key=pet_order.get)[:PET_NAME_CAP], pet_order

# remove stray listings and list missing inventory
def reconcile_listings(s, account_id, name, listed, inventory):
    allowed, pet_order = select_allowed_names(listed, inventory)
    allowed_set        = set(allowed)

    over_name    = []
    over_variant = []
    kept_by_variant = defaultdict(int)
    for i in listed:
        if _norm_item_name(i.get("name")) not in allowed_set:
            over_name.append(i["id"])
            continue
        kept_by_variant[i.get("productId")] += 1
        if kept_by_variant[i.get("productId")] > VARIANT_CAP:
            over_variant.append(i["id"])

    to_remove = over_name + over_variant
    if to_remove:
        log(name, C.ORANGE, f"Removing {len(to_remove)} listing(s) "
                            f"({len(over_name)} over name cap, {len(over_variant)} over variant cap)")
        _delete_listings(s, account_id, name, to_remove)
        listed = get_listed_targets(s, account_id, name) or []  # refresh after removal

    if _paused.is_set():
        return

    listed_ids = {i.get("id") for i in listed}
    variant_counts = defaultdict(int)
    for i in listed:
        variant_counts[i.get("productId")] += 1

    candidates = [i for i in inventory
                  if i.get("id") not in listed_ids
                  and _norm_item_name(i.get("name")) in allowed_set]
    candidates.sort(key=lambda i: (pet_order.get(_norm_item_name(i.get("name")), 9999),
                                   -item_priority(i),
                                   -int(i.get("id") or 0)))

    to_add        = []
    guard_checked = {}
    for i in candidates:
        pid = i.get("productId")
        if variant_counts[pid] >= VARIANT_CAP:
            continue
        if pid not in guard_checked:
            price = round(max(get_item_minimum(i), MIN_LIST_PRICE), 2)
            accepted, ref = check_floor_drop(pid, price)
            if not accepted and ref:
                log_throttled(name, C.YELLOW,
                              f"[drop guard] {item_label(i)}: listing at recent floor ${ref:.2f} "
                              f"instead of dumped floor ${price:.2f}")
                price = round(max(ref, MIN_LIST_PRICE), 2)
            guard_checked[pid] = price
        to_add.append({"id": i["id"], "price": guard_checked[pid],
                       "name": i.get("name", ""), "pumping": i.get("pumping", "normal")})
        variant_counts[pid] += 1

    if not to_add:
        return

    total_listed = 0
    for i in range(0, len(to_add), API_BATCH):
        if _paused.is_set():
            break
        chunk = [{"id": it["id"], "price": it["price"]} for it in to_add[i:i + API_BATCH]]
        count = list_from_inventory(s, chunk, account_id, name)
        total_listed += count
        if count < len(chunk):
            break

    grp = defaultdict(int)
    for it in to_add:
        grp[f"{it['pumping']} {it['name']}".strip()] += 1
    log(name, C.GREEN if total_listed > 0 else C.RED,
        f"Listed {total_listed}/{len(to_add)}: " + ", ".join(f"{n}x {k}" for k, n in grp.items()))

# background: relink via cookie, wait for the account to hit state 2
def _relink_and_wait(s, account_id, name, state):
    login_name, roblox_username = name.split("/")[0], name.split("/")[-1]
    if not relink_account_by_cookie(s, roblox_username, login_name):
        state.pop("_relink_pending", None)
        state.pop("_waiting_for_state2", None)
        return
    deadline = time.monotonic() + 420
    new_state = None
    while time.monotonic() < deadline:
        time.sleep(10)
        new_state = get_account_state(s, account_id)
        if new_state == 2:
            log(name, C.GREEN, "Relinked — back to state=2")
            state.pop("_relink_pending", None)
            return
        if new_state not in (0, 1, 5, None):
            break
    log(name, C.ORANGE, f"Relink gave up waiting for state=2 (state={new_state}) — will retry next tick")
    state.pop("_relink_pending", None)
    state.pop("_waiting_for_state2", None)

# one full cycle for a linked account: fix its state, reprice, reconcile
def tick(s, account_id, name, state, known_state=None):
    acct_state = known_state if known_state is not None else get_account_state(s, account_id)
    if acct_state == 0:
        if not state.get("_relink_pending"):
            state["_relink_pending"] = True
            state["_waiting_for_state2"] = True
            log(name, C.RED, "State=0 (dead cookie) — relinking in background")
            threading.Thread(target=_relink_and_wait, args=(s, account_id, name, state),
                             daemon=True, name=f"{name}-relink").start()
        return
    if acct_state == 5:
        state["_waiting_for_state2"] = True
        return
    if acct_state == 9:
        if time.monotonic() < state.get("_state9_backoff_until", 0):
            return
        log(name, C.ORANGE, "State=9 (UNKNOWN) — activating, then polling 45s")
        activate_account(s, account_id, name)
        deadline = time.monotonic() + 45
        new_state = 9
        while time.monotonic() < deadline:
            time.sleep(5)
            new_state = get_account_state(s, account_id)
            if new_state != 9:
                break
        if new_state == 9 or new_state is None:
            log(name, C.RED, "Still state=9 after 45s — backing off 15 min")
            state["_state9_backoff_until"] = time.monotonic() + 15 * 60
            return
        acct_state = new_state
    if acct_state in (3, 4):
        if acct_state == 4 and not unfreeze_account(s, account_id, name):
            return
        activate_account(s, account_id, name)
        if click_reload(s, account_id, name):
            log(name, C.ORANGE, f"{state_label(acct_state)} — sync triggered, will relist at state=2")
            state["_waiting_for_state2"] = True
        return
    if acct_state != 2 and acct_state is not None:
        log_throttled(name, C.ORANGE, f"Not ready (state={acct_state} {state_label(acct_state)}, need state=2) — skipping")
        return

    if _paused.is_set():
        return

    listed = tick_prices(s, account_id, name)
    if listed is None or _paused.is_set():
        return

    inventory = get_fresh_inventory(s, account_id, name)

    if listed or any(is_target(i) for i in inventory):
        state.pop("_empty_reload_attempted", None)
    elif not state.get("_empty_reload_attempted"):
        log(name, C.ORANGE, "State=2 but zero items in listings and inventory — triggering reload")
        state["_empty_reload_attempted"] = True
        if click_reload(s, account_id, name):
            state["_waiting_for_state2"] = True
            return

    if _paused.is_set():
        return

    reconcile_listings(s, account_id, name, listed, inventory)

    listed_after = get_listed_targets(s, account_id, name) or []
    distinct_listed = {i.get("name") for i in listed_after}
    if len(distinct_listed) < PET_NAME_CAP:
        inv_after = get_fresh_inventory(s, account_id, name)
        if {i.get("name") for i in inv_after if is_target(i)} - distinct_listed:
            reconcile_listings(s, account_id, name, listed_after, inv_after)

# read this login's saved token out of the json
def reload_token_from_file(login_name):
    acc = next((a for a in load_accounts() if a.get("name") == login_name), None)
    return acc.get("token") if acc else None

# use the refresh token to get a fresh access token and save it
def refresh_access_token(login_name):
    try:
        accs = load_accounts()
        acc  = next((a for a in accs if a["name"] == login_name), None)
        if not acc or not acc.get("refresh_token"):
            log(login_name, C.RED, "No refresh_token in starpets_accounts.json")
            return None
        refresh_tok = acc["refresh_token"]
        for attempt in range(3):
            try:
                r = requests.get(
                    "https://users.apineural.com/api/user/refresh",
                    headers={
                        "Authorization":      f"Bearer {refresh_tok}",
                        "content-type":       "application/json",
                        "accept":             "*/*",
                        "accept-language":    "en-CA,en;q=0.9",
                        "origin":             "https://starpets.gg",
                        "referer":            "https://starpets.gg/",
                        "sec-ch-ua":          '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
                        "sec-ch-ua-mobile":   "?0",
                        "sec-ch-ua-platform": '"Windows"',
                        "sec-fetch-dest":     "empty",
                        "sec-fetch-mode":     "cors",
                        "sec-fetch-site":     "cross-site",
                        "user-agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
                    },
                    timeout=20,
                )
                if r.status_code == 429:
                    wait = int(r.headers.get("x-ratelimit-reset", 10))
                    log(login_name, C.ORANGE, f"Refresh rate limited — waiting {wait}s")
                    time.sleep(wait + 1)
                    continue
                if r.status_code != 200:
                    log(login_name, C.RED, f"Refresh failed: HTTP {r.status_code} {r.text[:120]}")
                    break
                data        = r.json()
                new_access  = data.get("access_token")  or data.get("accessToken")
                new_refresh = data.get("refresh_token") or data.get("refreshToken")
                if not new_access:
                    log(login_name, C.RED, f"Refresh response missing access_token: {data}")
                    break
                bearer = f"Bearer {new_access}"
                acc["token"] = bearer
                if new_refresh:
                    acc["refresh_token"] = new_refresh
                save_accounts(accs)
                return bearer
            except Exception as e:
                log(login_name, C.GREY, f"Refresh attempt {attempt+1} error: {e}")
                if attempt < 2:
                    time.sleep(5)
        return None
    except Exception as e:
        log(login_name, C.RED, f"refresh_access_token error: {e}")
        return None

# one login's main loop: auth, keep the token fresh, tick its accounts
def account_worker(acc, trigger_event):
    login_name = acc["name"]

    token = reload_token_from_file(login_name) or acc["token"]
    s     = make_session(token)

    while True:
        _rate_wait()
        r = _api_call(lambda: s.get(LINKED_URL, timeout=10), login_name)
        if r is not None and r.status_code in (200, 304):
            break
        log(login_name, C.YELLOW, f"Auth failed ({_why(r)}) — attempting token refresh")
        refreshed = refresh_access_token(login_name)
        if refreshed:
            s = make_session(refreshed)
            continue
        log(login_name, C.RED, "Refresh failed — update token in starpets_accounts.json, retrying in 60s")
        time.sleep(60)
        new_token = reload_token_from_file(login_name)
        if new_token:
            s = make_session(new_token)

    log(login_name, C.GREEN, "Auth OK")

    try:
        sync_accounts_with_collect(s, login_name)
    except Exception as e:
        log(login_name, C.RED, f"[startup sync] error: {e}")

    session = [s]
    _session_lock = threading.Lock()
    # background loop, refresh the token every 5 min
    def _token_refresh_loop():
        while True:
            time.sleep(5 * 60)
            new_bearer = refresh_access_token(login_name)
            if new_bearer:
                with _session_lock:
                    session[0] = make_session(new_bearer)
            else:
                log(login_name, C.ORANGE, "Token refresh failed — will retry in 5 min")

    threading.Thread(target=_token_refresh_loop, daemon=True,
                     name=f"{login_name}-token-refresh").start()

    states = {}

    # fast path to recover a dead, stale or frozen account
    def _fire_reload(rbx, cur_s):
        aid   = rbx["id"]
        rname = f"{login_name}/{rbx['name']}"
        st    = states.setdefault(aid, {})
        state_val = rbx.get("state")
        try:
            if state_val == 0:
                if st.get("_relink_pending"):
                    return
                if relink_account_by_cookie(cur_s, rbx.get("name", ""), login_name):
                    st["_relink_pending"] = True
                    st["_waiting_for_state2"] = True
                    log(rname, C.CYAN, "Dead cookie (state=0) — relinked, will relist at state=2")
                return
            if state_val == 4:
                unfreeze_account(cur_s, aid, rname)
            activate_account(cur_s, aid, rname)
            if click_reload(cur_s, aid, rname):
                st["_waiting_for_state2"] = True
        except Exception as e:
            log(rname, C.RED, f"Fast reload error: {e}")

    # run a tick across all this login's linked accounts
    def run_accounts(linked):
        _tick_running.set()
        try:
            with _tick_lock:
                if _paused.is_set():
                    return

                with _session_lock:
                    reload_s = session[0]
                to_reload = [rbx for rbx in linked
                             if rbx.get("state") in (0, 3, 4)
                             and not states.get(rbx["id"], {}).get("_waiting_for_state2")
                             and not states.get(rbx["id"], {}).get("_relink_pending")]
                fast_handled = {rbx["id"] for rbx in to_reload}
                if to_reload:
                    log_throttled(login_name, C.ORANGE,
                                  f"Recovering {len(to_reload)} not-ready account(s) (dead/outdated/frozen)")
                    with ThreadPoolExecutor(max_workers=min(RELOAD_CONCURRENCY, len(to_reload)),
                                            thread_name_prefix=f"{login_name}-reload") as pool:
                        for rbx in to_reload:
                            pool.submit(_fire_reload, rbx, reload_s)

                for rbx in linked:
                    if _paused.is_set():
                        break
                    aid   = rbx["id"]
                    rname = f"{login_name}/{rbx['name']}"
                    st    = states.setdefault(aid, {})
                    if aid in fast_handled:
                        continue
                    if st.get("_waiting_for_state2"):
                        live_state = rbx.get("state")
                        if live_state == 2:
                            log(rname, C.CYAN, "Recovered (state=2) — resuming tick")
                            st.pop("_waiting_for_state2", None)
                            st.pop("_relink_pending", None)
                        elif live_state == 3:
                            st.pop("_waiting_for_state2", None)
                        elif live_state == 0:
                            st.pop("_waiting_for_state2", None)
                            st.pop("_relink_pending", None)
                            continue
                        else:
                            log_throttled(rname, C.GREY, f"Waiting for reload (state={live_state}) — skipping tick")
                            continue
                    try:
                        with _session_lock:
                            cur_s = session[0]
                        tick(cur_s, aid, rname, st, known_state=rbx.get("state"))
                    except BaseException as e:
                        log(rname, C.RED, f"Tick error ({type(e).__name__}): {e}\n{traceback.format_exc()}")
                        if isinstance(e, (KeyboardInterrupt, SystemExit)):
                            raise
        finally:
            _tick_running.clear()

    while True:
        try:
            with _session_lock:
                s = session[0]

            if _paused.is_set():
                time.sleep(1)
                continue

            new_token = reload_token_from_file(login_name)
            if new_token and new_token != s.headers.get("authorization"):
                with _session_lock:
                    session[0] = make_session(new_token)
                s = session[0]

            _rate_wait()
            r = _api_call(lambda: s.get(LINKED_URL, timeout=15), login_name)
            if r is not None and r.status_code == 401:
                log(login_name, C.YELLOW, "401 — token expired mid-run, refreshing")
                new_bearer = refresh_access_token(login_name)
                if new_bearer:
                    with _session_lock:
                        session[0] = make_session(new_bearer)
                continue
            linked = _parse_linked(r) if r is not None and r.status_code in (200, 304) else []

            if not linked:
                log_throttled(login_name, C.ORANGE, "No active linked Roblox accounts found")
            elif trigger_event.is_set():
                trigger_event.clear()
                ready = []

                # ctrl+r handler for one account: unfreeze, activate, reload
                def _ctrl_r_one(rbx):
                    aid   = rbx["id"]
                    rname = f"{login_name}/{rbx['name']}"
                    try:
                        if rbx.get("state") in (3, 4):
                            unfreeze_account(s, aid, rname)
                            time.sleep(1)
                        activate_account(s, aid, rname)
                        time.sleep(0.5)
                        if click_reload(s, aid, rname):
                            wait_for_reload(s, aid, rname, max_wait=CTRL_R_MAX_WAIT)
                        live_state = get_account_state(s, aid)
                        if live_state == 2:
                            rbx["state"] = live_state
                            ready.append(rbx)
                        else:
                            log(rname, C.RED, f"Never reached state=2 (stuck at {live_state}) — skipping its tick")
                    except Exception as e:
                        log(rname, C.RED, f"Reload error: {e}")

                with ThreadPoolExecutor(max_workers=min(RELOAD_CONCURRENCY, len(linked)),
                                        thread_name_prefix=f"{login_name}-ctrlr") as pool:
                    for rbx in linked:
                        pool.submit(_ctrl_r_one, rbx)
                log(login_name, C.CYAN if ready else C.RED, f"[Ctrl+R] {len(ready)}/{len(linked)} account(s) ready to list")
                if ready:
                    run_accounts(ready)
            elif not _tick_running.is_set():
                threading.Thread(target=run_accounts, args=(linked,), daemon=True,
                                 name=f"{login_name}-tick").start()

        except BaseException as e:
            log(login_name, C.RED, f"Worker error ({type(e).__name__}): {e}\n{traceback.format_exc()}")
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise

        for _ in range(CHECK_INTERVAL):
            time.sleep(1)
            if trigger_event.is_set():
                break

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    import msvcrt
    print(c(C.BOLD + C.GOLD, "\n=== Starpets Auto-Relist ==="))

    accs = setup_accounts()
    print(c(C.WHITE, f"Loaded {len(accs)} starpets login(s): {', '.join(a['name'] for a in accs)}"))

    # ctrl+t: unlist everything then freeze, stays paused
    def do_freeze_all():
        _paused.set()
        with _tick_lock:
            pass

        done: set = set()

        for pass_n in range(1, UNLIST_MAX_PASSES + 1):
            remaining = 0

            for acc in accs:
                login_name = acc["name"]
                s = make_session(reload_token_from_file(login_name) or acc["token"])
                try:
                    linked = get_linked_roblox_accounts(s, login_name)
                except Exception as e:
                    log(login_name, C.RED, f"[Ctrl+T] Listing fetch error: {e}")
                    continue

                for rbx in linked:
                    aid = rbx["id"]
                    if aid in done:
                        continue
                    rname = f"{login_name}/{rbx['name']}"
                    state = rbx.get("state")

                    if state == 4:
                        done.add(aid)
                        continue

                    if state == 5:
                        remaining += 1
                        continue

                    if state == 3:
                        try:
                            activate_account(s, aid, rname)
                            click_reload(s, aid, rname)
                        except Exception as e:
                            log(rname, C.RED, f"[Ctrl+T] Refresh error: {e}")
                        remaining += 1
                        continue

                    try:
                        listed = get_listed_targets(s, aid, rname)
                    except Exception as e:
                        log(rname, C.RED, f"[Ctrl+T] Listing fetch error: {e}")
                        remaining += 1
                        continue

                    if listed is None:
                        remaining += 1
                        continue

                    if not listed:
                        if freeze_account(s, aid, rname):
                            done.add(aid)
                        else:
                            remaining += 1
                        continue

                    # bug: _paused is set here, so _delete_listings removes nothing
                    try:
                        _delete_listings(s, aid, rname, [i["id"] for i in listed])
                    except Exception as e:
                        log(rname, C.RED, f"[Ctrl+T] Remove error: {e}")
                    remaining += 1

            if not remaining:
                log("Ctrl+T", C.GOLD, f"All accounts unlisted after {pass_n} pass(es) — paused until Ctrl+V")
                return

            log_throttled("Ctrl+T", C.GREY,
                          f"{remaining} account(s) not done — re-checking every {UNLIST_POLL_SECONDS}s")
            time.sleep(UNLIST_POLL_SECONDS)

        log("Ctrl+T", C.RED,
            f"Gave up after {UNLIST_MAX_PASSES} passes — some accounts still listed, paused until Ctrl+V")

    # ctrl+g: freeze all accounts without unlisting first
    def do_freeze_only():
        _paused.set()
        with _tick_lock:
            pass
        for acc in accs:
            login_name = acc["name"]
            s = make_session(reload_token_from_file(login_name) or acc["token"])
            try:
                linked = get_linked_roblox_accounts(s, login_name)
                frozen = 0
                for rbx in linked:
                    rname = f"{login_name}/{rbx['name']}"
                    try:
                        if rbx.get("state") in (3, 4):
                            frozen += 1
                            continue
                        r = _set_frozen(s, rbx["id"], rname, True)
                        if r and r.ok:
                            frozen += 1
                        else:
                            log(rname, C.ORANGE, f"Freeze skipped: {_why(r)}")
                    except Exception as e:
                        log(rname, C.ORANGE, f"Freeze error ({e}) — skipping")
                if linked:
                    log(login_name, C.GREEN, f"[Ctrl+G] {frozen}/{len(linked)} frozen")
            except Exception as e:
                log(login_name, C.RED, f"[Ctrl+G] Login error: {e} — skipping")
        log("Ctrl+G", C.GOLD, "Paused until Ctrl+V")

    # ctrl+v: unfreeze everything and resume
    def do_unfreeze_all():
        for acc in accs:
            login_name = acc["name"]
            s = make_session(reload_token_from_file(login_name) or acc["token"])
            try:
                frozen = [rbx for rbx in get_linked_roblox_accounts(s, login_name)
                          if rbx.get("state") in (3, 4)]
                unfrozen = 0
                for rbx in frozen:
                    rname = f"{login_name}/{rbx['name']}"
                    try:
                        if unfreeze_account(s, rbx["id"], rname):
                            unfrozen += 1
                    except Exception as e:
                        log(rname, C.ORANGE, f"Unfreeze error ({e}) — skipping")
                if frozen:
                    log(login_name, C.GREEN, f"[Ctrl+V] {unfrozen}/{len(frozen)} unfrozen")
            except Exception as e:
                log(login_name, C.RED, f"[Ctrl+V] Login error: {e} — skipping")
        _paused.clear()
        log("Ctrl+V", C.GOLD, "Resuming normal operation")

    # open the price checker in its own window
    def _launch_price_checker():
        time.sleep(5)
        try:
            pricer = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "starpets_pricer.py")
            subprocess.Popen(
                ["cmd.exe", "/c", "start", "Starpets Price Checker",
                 sys.executable, pricer],
                shell=False, close_fds=True
            )
        except Exception as e:
            print(c(C.ORANGE, f"Could not launch price checker window: {e}"))

    # run account_worker and print if it crashes
    def _worker_wrapper(a, e):
        try:
            account_worker(a, e)
        except Exception:
            print(f"\n[{a['name']}] WORKER CRASHED:\n{traceback.format_exc()}", flush=True)

    triggers = []
    threads  = []
    for acc in accs:
        ev = threading.Event()
        triggers.append(ev)
        t = threading.Thread(target=_worker_wrapper, args=(acc, ev), daemon=True, name=acc["name"])
        t.start()
        threads.append(t)

    threading.Thread(target=_launch_price_checker, daemon=True, name="price-checker-launch").start()

    key_jobs = {
        '\x14': (C.ORANGE, "[Ctrl+T] Unlisting all items (will stay paused until Ctrl+V)...", do_freeze_all, "unlist"),
        '\x07': (C.ORANGE, "[Ctrl+G] Freezing all accounts (will stay paused until Ctrl+V)...", do_freeze_only, "freeze"),
        '\x16': (C.CYAN, "[Ctrl+V] Unfreezing all accounts and resuming...", do_unfreeze_all, "unfreeze"),
    }
    running = {}

    print(c(C.GOLD, "Ready — Ctrl+R: refresh+list | Ctrl+T: unlist all (pauses) | "
                     "Ctrl+G: freeze only (pauses) | Ctrl+V: unfreeze all (resumes) | "
                     "Ctrl+C: quit"))
    try:
        while True:
            if all(not t.is_alive() for t in threads):
                print(c(C.RED, "\nAll worker threads have stopped — check errors above."))
                input("Press Enter to exit...")
                break
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch == '\x12':  # Ctrl+R
                    print(c(C.GOLD, "\n[Ctrl+R] Triggering all accounts..."))
                    for ev in triggers:
                        ev.set()
                elif ch in key_jobs and not (ch in running and running[ch].is_alive()):
                    color, msg, job, thread_name = key_jobs[ch]
                    print(c(color, "\n" + msg))
                    running[ch] = threading.Thread(target=job, daemon=True, name=thread_name)
                    running[ch].start()
            time.sleep(0.05)
    except KeyboardInterrupt:
        print(c(C.GREY, "\nStopped."))

# lighter mode: just keep repricing listed items
def price_checker_main():
    print(c(C.BOLD + C.GOLD, "\n=== Starpets Price Checker ==="))
    accs = setup_accounts()
    print(c(C.WHITE, f"Loaded {len(accs)} login(s)"))

    login_sessions = {}

    # get or refresh this login's session and linked list
    def _ensure_session(acc):
        name = acc["name"]
        now  = time.monotonic()
        ls   = login_sessions.get(name)

        disk_token = reload_token_from_file(name) or acc["token"]
        if ls is None or ls["session"].headers.get("authorization") != disk_token:
            ls = {"session": make_session(disk_token), "linked": [], "linked_at": 0}
            login_sessions[name] = ls

        if now - ls["linked_at"] >= LINKED_REFRESH:
            ls["linked"]    = get_linked_roblox_accounts(ls["session"], name)
            ls["linked_at"] = now

        return ls["session"], ls["linked"]

    while True:
        for acc in accs:
            name = acc["name"]
            try:
                s, linked = _ensure_session(acc)
                for rbx in linked:
                    if rbx.get("state") != 2:
                        continue
                    rname = f"{name}/{rbx['name']}"
                    try:
                        tick_prices(s, rbx["id"], rname)
                    except Exception as e:
                        log(rname, C.RED, f"Price tick error: {e}\n{traceback.format_exc()}")
            except Exception as e:
                log(name, C.RED, f"Session error: {e}\n{traceback.format_exc()}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    try:
        if "--price-only" in sys.argv:
            price_checker_main()
        else:
            main()
    except KeyboardInterrupt:
        print(c(C.GREY, "\nStopped."))
    except Exception:
        print("\nCRASH:\n" + traceback.format_exc())
        input("Press Enter to exit...")
