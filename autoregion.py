#!/usr/bin/env python3

import subprocess, time, sys, os, urllib.parse, getpass, shlex
import hashlib, base64, json, threading
from datetime import datetime

try:
    import requests
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False

ROBLOX_PACKAGE  = "com.roblox.client"
SERVER_LINK     = ""
ROBLOX_COOKIE   = ""

CHECK_INTERVAL  = 3
REJOIN_DELAY    = 4
WAIT_TIMEOUT    = 90
RETRY_WAIT      = 15

PAUSE_FILE  = "/tmp/roblox_pause"
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".roblox_config.json")

class C:
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    CYAN   = "\033[96m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RESET  = "\033[0m"

def log(msg, color=C.RESET):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{C.DIM}[{ts}]{C.RESET} {color}{msg}{C.RESET}", flush=True)


_SENTINEL = "__CMD_DONE_7f3a9b__"
_shell: subprocess.Popen | None = None
_shell_lock = threading.Lock()


def _start_shell() -> bool:
    global _shell
    try:
        _shell = subprocess.Popen(
            ["su"],
            stdin  = subprocess.PIPE,
            stdout = subprocess.PIPE,
            stderr = subprocess.STDOUT,
            text   = True,
            bufsize= 1,
        )
        out = _su_exec("id", timeout=8)
        if "uid=0" not in out:
            log("Root shell has no root privileges.", C.RED)
            return False
        return True
    except Exception as e:
        log(f"Failed to open root shell: {e}", C.RED)
        return False


def _su_exec(cmd: str, timeout: float = 8) -> str:
    global _shell
    with _shell_lock:
        if _shell is None or _shell.poll() is not None:
            log("Shell died, restarting...", C.YELLOW)
            try:
                _shell = subprocess.Popen(
                    ["su"],
                    stdin  = subprocess.PIPE,
                    stdout = subprocess.PIPE,
                    stderr = subprocess.STDOUT,
                    text   = True,
                    bufsize= 1,
                )
                time.sleep(0.5)
            except Exception as e:
                log(f"Failed to restart shell: {e}", C.RED)
                _shell = None
                return ""

        try:
            _shell.stdin.write(f"{cmd}; echo '{_SENTINEL}'\n")
            _shell.stdin.flush()

            lines    = []
            deadline = time.time() + timeout
            while time.time() < deadline:
                line = _shell.stdout.readline()
                if not line:
                    break
                line = line.rstrip("\n")
                if _SENTINEL in line:
                    break
                lines.append(line)

            return "\n".join(lines).strip()

        except Exception:
            try:
                _shell.kill()
            except Exception:
                pass
            _shell = None
            return ""


def su(cmd: str, timeout: float = 8) -> str:
    return _su_exec(cmd, timeout)


def is_roblox_running() -> bool:
    pkg = ROBLOX_PACKAGE

    out = su(f"pidof '{pkg}' 2>/dev/null", timeout=5)
    if out.strip() and any(p.strip().isdigit() for p in out.split()):
        return True

    out = su(f"ps -A 2>/dev/null | grep '{pkg}' | grep -v grep", timeout=6)
    if pkg in out:
        return True

    out = su(f"dumpsys activity processes 2>/dev/null | grep '{pkg}'", timeout=8)
    if pkg in out:
        return True

    return False


def is_roblox_window_visible() -> bool:
    pkg = ROBLOX_PACKAGE

    out = su(f"dumpsys window windows 2>/dev/null | grep -E 'mCurrentFocus|mFocusedApp'", timeout=8)
    if pkg in out:
        return True

    out = su(f"dumpsys activity activities 2>/dev/null | grep 'mResumedActivity'", timeout=8)
    if pkg in out:
        return True

    return False


def get_roblox_state() -> str:
    if not is_roblox_running():
        return "not_running"
    if is_roblox_window_visible():
        return "running_foreground"
    return "running_background"


def _device_key() -> bytes:
    aid = su("settings get secure android_id", timeout=5).strip()
    if not aid or aid in ("null", ""):
        aid = su("getprop ro.serialno", timeout=5).strip() or "fallback_key_v1"
    return hashlib.sha256(f"roblox_rejoin:{aid}".encode()).digest()

def _xor(data: bytes, key: bytes) -> bytes:
    ks = (key * (len(data) // len(key) + 1))[:len(data)]
    return bytes(a ^ b for a, b in zip(data, ks))

def encrypt_cookie(s: str) -> str:
    return base64.b64encode(_xor(s.encode(), _device_key())).decode()

def decrypt_cookie(s: str) -> str:
    return _xor(base64.b64decode(s.encode()), _device_key()).decode()


def save_config(link: str, cookie: str):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump({
                "server_link":      link,
                "cookie_encrypted": encrypt_cookie(cookie) if cookie else "",
                "saved_at":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }, f, indent=2)
        os.chmod(CONFIG_FILE, 0o600)
        log("Config saved.", C.GREEN)
    except Exception as e:
        log(f"Failed to save config: {e}", C.YELLOW)

def load_config() -> tuple[str, str]:
    if not os.path.exists(CONFIG_FILE):
        return "", ""
    try:
        cfg    = json.load(open(CONFIG_FILE))
        link   = cfg.get("server_link", "")
        enc    = cfg.get("cookie_encrypted", "")
        cookie = ""
        if enc:
            try:
                cookie = decrypt_cookie(enc)
            except Exception:
                log("Failed to decrypt cookie — please re-enter.", C.YELLOW)
        return link, cookie
    except Exception:
        return "", ""


def parse_link(link: str) -> dict:
    link = link.strip()
    r = {"raw": link, "type": None, "placeId": None,
         "gameInstanceId": None, "privateServerLinkCode": None}

    if link.startswith("roblox://"):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        r.update(type="direct_uri",
                 placeId=q.get("placeId", [None])[0],
                 gameInstanceId=q.get("gameInstanceId", [None])[0],
                 privateServerLinkCode=q.get("linkCode", [None])[0])
        return r

    p     = urllib.parse.urlparse(link)
    q     = urllib.parse.parse_qs(p.query)
    parts = [x for x in p.path.split("/") if x]
    if "games" in parts:
        i = parts.index("games")
        if i + 1 < len(parts):
            r["placeId"] = parts[i + 1]
    r["privateServerLinkCode"] = q.get("privateServerLinkCode", [None])[0]
    r["gameInstanceId"]        = q.get("gameInstanceId",        [None])[0]
    r["type"] = "web_link"
    return r


def resolve_private_server(place_id, link_code) -> str | None:
    if not REQUESTS_OK:
        return None
    hdrs = {"User-Agent": "Roblox/Android", "Content-Type": "application/json"}
    cks  = {".ROBLOSECURITY": ROBLOX_COOKIE} if ROBLOX_COOKIE else {}
    try:
        items = requests.get(
            "https://games.roblox.com/v1/private-servers",
            params={"privateServerLinkCode": link_code},
            headers=hdrs, cookies=cks, timeout=10,
        ).json().get("data") or []
        if items and items[0].get("id"):
            return str(items[0]["id"])
    except Exception:
        pass
    try:
        js = requests.post(
            "https://gamejoin.roblox.com/v1/join-private-game",
            json={"placeId": int(place_id), "linkCode": link_code},
            headers={**hdrs, "Referer": "https://www.roblox.com/"},
            cookies=cks, timeout=10,
        ).json().get("joinScript") or {}
        gid = js.get("GameId") or js.get("gameId")
        if gid:
            return str(gid)
    except Exception:
        pass
    return None


def build_uri(place_id, instance_id=None, link_code=None) -> str:
    p = {"placeId": place_id}
    if instance_id: p["gameInstanceId"] = instance_id
    elif link_code: p["linkCode"]       = link_code
    return "roblox://experiences/start?" + urllib.parse.urlencode(p)


def launch_uri(uri: str) -> bool:
    q_uri = shlex.quote(uri)
    q_act = shlex.quote(f"{ROBLOX_PACKAGE}/com.roblox.client.ActivityProtocolLaunch")

    for cmd in [
        f"am start -a android.intent.action.VIEW -d {q_uri} -n {q_act} --activity-brought-to-front",
        f"am start -a android.intent.action.VIEW -d {q_uri}",
    ]:
        out = su(cmd, timeout=12)
        if "error" not in out.lower() and "exception" not in out.lower():
            return True

    log("Failed to launch Roblox.", C.RED)
    return False


def join_game(parsed: dict) -> bool:
    if parsed["type"] == "direct_uri":
        return launch_uri(parsed["raw"])

    place_id  = parsed.get("placeId")
    inst_id   = parsed.get("gameInstanceId")
    link_code = parsed.get("privateServerLinkCode")

    if not place_id:
        log("No PlaceID found!", C.RED)
        return False

    if link_code and not inst_id:
        inst_id = resolve_private_server(place_id, link_code)

    return launch_uri(build_uri(place_id, inst_id, link_code if not inst_id else None))


def force_stop_roblox():
    su(f"am force-stop {ROBLOX_PACKAGE}", timeout=8)
    time.sleep(1)


def wait_if_paused():
    if not os.path.exists(PAUSE_FILE):
        return
    log(f"Paused — remove {PAUSE_FILE} to resume.", C.YELLOW)
    while os.path.exists(PAUSE_FILE):
        time.sleep(2)
    log("Resumed.", C.GREEN)


def monitor(parsed: dict):
    log(f"Monitoring... (Ctrl+C to stop)", C.CYAN)
    log(f"  Pause: touch {PAUSE_FILE}  |  Resume: rm {PAUSE_FILE}\n", C.DIM)

    prev_state   = None
    need_rejoin  = False
    attempt      = 0
    joined_at    = None
    never_joined = True

    while True:
        wait_if_paused()

        try:
            state = get_roblox_state()
        except Exception as e:
            log(f"Detection error: {e}", C.YELLOW)
            time.sleep(CHECK_INTERVAL)
            continue

        if state == "running_foreground":
            if prev_state != "running_foreground":
                log("Roblox is running (foreground).", C.GREEN)
                joined_at = time.time()
            prev_state   = state
            need_rejoin  = False
            attempt      = 0
            never_joined = False
            time.sleep(CHECK_INTERVAL)
            continue

        if state == "running_background":
            if prev_state == "running_foreground":
                session_sec = time.time() - (joined_at or time.time())
                log(f"Game window closed (after {session_sec:.0f}s). Force stopping + rejoining in {REJOIN_DELAY}s...", C.YELLOW)
                force_stop_roblox()
                time.sleep(REJOIN_DELAY)
                need_rejoin = True
                attempt     = 0
            elif prev_state == "running_background":
                if not need_rejoin:
                    log("Game window still closed. Marking for rejoin.", C.YELLOW)
                    force_stop_roblox()
                    need_rejoin = True
                    attempt     = 0
            prev_state = state

        elif state == "not_running":
            if prev_state in ("running_foreground", "running_background") and not need_rejoin:
                session_sec = time.time() - (joined_at or time.time())
                log(f"Roblox stopped (after {session_sec:.0f}s). Rejoining in {REJOIN_DELAY}s...", C.YELLOW)
                time.sleep(REJOIN_DELAY)
                need_rejoin = True
                attempt     = 0
            elif never_joined and not need_rejoin:
                need_rejoin = True
                attempt     = 0
            prev_state = state

        if need_rejoin:
            wait_if_paused()
            attempt += 1
            log(f"Rejoin attempt {attempt}...", C.CYAN)

            if not join_game(parsed):
                log(f"Failed to launch game. Retrying in {RETRY_WAIT}s...", C.RED)
                time.sleep(RETRY_WAIT)
                continue

            log("  Waiting for game to load...", C.DIM)
            deadline = time.time() + WAIT_TIMEOUT
            loaded   = False
            while time.time() < deadline:
                time.sleep(3)
                cur_state = get_roblox_state()
                if cur_state in ("running_foreground", "running_background"):
                    if cur_state == "running_foreground":
                        loaded = True
                        break
                    time.sleep(5)
                    if get_roblox_state() == "running_foreground":
                        loaded = True
                        break

            if loaded:
                log("Joined game!", C.GREEN)
                prev_state   = "running_foreground"
                need_rejoin  = False
                attempt      = 0
                never_joined = False
                joined_at    = time.time()
            else:
                log(f"Game did not load after {WAIT_TIMEOUT}s. Retrying in {RETRY_WAIT}s...", C.RED)
                prev_state  = "not_running"
                need_rejoin = True
                force_stop_roblox()
                time.sleep(RETRY_WAIT)

            continue

        time.sleep(CHECK_INTERVAL)


def main():
    global ROBLOX_COOKIE

    print(f"""\
{C.CYAN}{C.BOLD}╔══════════════════════════════════════════════╗
║      Roblox Auto Rejoin — Android ROOT       ║
╚══════════════════════════════════════════════╝{C.RESET}
""")

    log("Starting root shell...", C.CYAN)
    if not _start_shell():
        log("No root access! Requires Magisk / KernelSU.", C.RED)
        sys.exit(1)
    log("Root shell OK.", C.GREEN)

    if not REQUESTS_OK:
        log("Missing library: pip install requests", C.YELLOW)

    saved_link, saved_cookie = load_config()
    link = ""

    if saved_link:
        link          = saved_link
        ROBLOX_COOKIE = saved_cookie
        log(f"Using saved config: {saved_link[:60]}{'...' if len(saved_link) > 60 else ''}", C.CYAN)

    if not link:
        link = SERVER_LINK.strip() or (" ".join(sys.argv[1:]).strip() if len(sys.argv) >= 2 else "")

    if not link:
        print(f"\n{C.CYAN}Enter server link:{C.RESET}")
        print(f"{C.DIM}e.g. https://www.roblox.com/games/12345/Game?privateServerLinkCode=XXXX{C.RESET}")
        link = input(f"\n{C.CYAN}Link > {C.RESET}").strip()

    if not link:
        log("No link provided.", C.RED)
        sys.exit(1)

    parsed = parse_link(link)
    log(f"🔗  PlaceID: {parsed['placeId']}  |  LinkCode: {'✅' if parsed['privateServerLinkCode'] else '❌'}  |  InstID: {'✅' if parsed['gameInstanceId'] else '❌'}", C.CYAN)

    if not ROBLOX_COOKIE:
        if parsed.get("privateServerLinkCode"):
            log("Private server requires a cookie.", C.YELLOW)
        try:
            ROBLOX_COOKIE = getpass.getpass(f"{C.CYAN}Cookie .ROBLOSECURITY (hidden input): {C.RESET}").strip()
        except Exception:
            ROBLOX_COOKIE = input(f"{C.CYAN}Cookie: {C.RESET}").strip()
        if ROBLOX_COOKIE:
            log("Cookie OK." if len(ROBLOX_COOKIE) > 50 else "Cookie looks too short.", C.GREEN)

    save_config(link, ROBLOX_COOKIE)

    if os.path.exists(PAUSE_FILE):
        os.remove(PAUSE_FILE)

    print()
    try:
        monitor(parsed)
    except KeyboardInterrupt:
        print(f"\n{C.YELLOW}Stopped.{C.RESET}")
        if os.path.exists(PAUSE_FILE):
            os.remove(PAUSE_FILE)
        if _shell:
            try:
                _shell.stdin.write("exit\n")
                _shell.stdin.flush()
            except Exception:
                pass


if __name__ == "__main__":
    main()
