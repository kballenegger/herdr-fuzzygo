#!/usr/bin/env python3
"""herdr-fuzzygo — fuzzy goto popup for herdr.

Type to fuzzy-filter every pane in the session (agents and plain shells
alike), see each one's live state, and jump to it with Enter. Panes on
enabled saved SSH machines (`herdr machine list`) are listed too, prefixed
with the machine label.

Keys:
  type           filter (smart case, space-separated terms all must match)
  up/down        move selection        ctrl+j/k, ctrl+n/p   same
  pgup/pgdn      page                  home/end             first/last
  enter          jump to the selected pane (closes the popup)
  tab            toggle ordering: go (recent first) / inbox (attention first)
  ctrl+u         clear the query       ctrl+w               delete a word
  esc / ctrl+c   close
  click selects, double click jumps

Environment:
  FUZZYGO_MODE=go|inbox   initial ordering (default go)
  FUZZYGO_DRY_RUN=<path>  write the chosen pane key to <path> instead of focusing
                          (pane id, or <machine id>/<pane id> on a saved machine)
  HERDR_SOCKET_PATH       herdr session socket (set by herdr in managed panes)
  HERDR_BIN_PATH          herdr binary, for `--machine` forwarding (default: herdr)

CLI:
  python3 fuzzygo.py                open the popup
  python3 fuzzygo.py --dump [q]     print the ranked rows for query q and exit
  python3 fuzzygo.py --demo         run against canned fixtures (no herdr)
"""

import curses
import json
import os
import re
import socket
import subprocess
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

DEMO = "--demo" in sys.argv
MODES = ("go", "inbox")

# ---------------------------------------------------------------- herdr I/O


def socket_path():
    return os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser(
        "~/.config/herdr/herdr.sock")


def state_dir():
    d = os.path.join(os.path.dirname(socket_path()), "fuzzygo-state")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
    except OSError:
        pass
    return d


def herdr_request(method, params=None, timeout=4.0):
    """One request over the herdr socket. Raises only RuntimeError/OSError."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(socket_path())
        s.sendall((json.dumps({"id": "fuzzygo", "method": method,
                               "params": params or {}}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    if not buf:
        raise RuntimeError("herdr closed the connection without replying")
    try:
        resp = json.loads(buf)
    except ValueError as e:
        raise RuntimeError("bad response from herdr: %s" % e)
    if "error" in resp:
        raise RuntimeError(str(resp["error"]))
    return resp.get("result") or {}


# Saved SSH machines live behind their own herdr servers; the socket API only
# reaches the one we run in. The CLI forwards `--machine <id> ...` over SSH.
HERDR_BIN = os.environ.get("HERDR_BIN_PATH") or "herdr"


def herdr_cli(args, timeout=8.0):
    """Run `herdr <args>` and return its parsed JSON. Raises only RuntimeError/OSError."""
    try:
        p = subprocess.run([HERDR_BIN] + list(args), capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("timed out after %gs" % timeout)
    out = p.stdout.strip()
    if not out:
        err = (p.stderr.strip().splitlines() or ["no output"])[-1]
        raise RuntimeError(err[:160])
    try:
        resp = json.loads(out)                      # one document (may be pretty-printed)
    except ValueError:
        try:
            resp = json.loads(out.splitlines()[-1])  # a JSON line after chatter
        except ValueError as e:
            raise RuntimeError("bad CLI output: %s" % e)
    if isinstance(resp, dict) and "error" in resp:
        err = resp["error"]
        raise RuntimeError(err.get("message") if isinstance(err, dict) else str(err))
    return resp


def _cli_result(args, timeout=8.0):
    resp = herdr_cli(args, timeout)
    return (resp.get("result") or {}) if isinstance(resp, dict) else {}


def list_machines():
    """Enabled saved SSH machines: [{"id", "label", "selected"}]. [] when none.

    `selected` is the machine the herdr client is viewing (none: Local)."""
    try:
        v = herdr_cli(["machine", "list", "--json"], timeout=5.0)
    except (OSError, RuntimeError):
        return []
    if not isinstance(v, list):
        return []
    return [{"id": m["id"],
             "label": m.get("label") or m.get("target") or m["id"],
             "selected": bool(m.get("selected"))}
            for m in v if isinstance(m, dict) and m.get("enabled") and m.get("id")]


# -------------------------------------------------------------------- rows

# Attention tiers, matching herdr's sidebar language.
TIER = {"blocked": 0, "done": 1, "working": 2, "idle": 3, "unknown": 4,
        "shell": 5}
ICON = {"blocked": "!", "done": "●", "working": "▸", "idle": "○",
        "unknown": "?", "shell": "·"}


def _short_cwd(cwd):
    home = os.path.expanduser("~")
    if cwd == home:
        return "~"
    if cwd.startswith(home + "/"):
        cwd = "~" + cwd[len(home):]
    parts = cwd.split("/")
    if len(parts) <= 3:
        return cwd
    # keep the last two components whole, abbreviate the rest to one char
    head = [(p[:2] if p.startswith(".") else p[:1]) for p in parts[:-2]]
    return "/".join(head + parts[-2:])


DEMO_MACHINES = [{"id": "demo-klaw", "label": "klaw", "selected": False}]


def _demo_rows():
    def mk(i, ws, wsl, tab, tabl, cwd, agent, name, status, title, since,
           focused=False, proc="", machine=None):
        pid = "%s:p%d" % (ws, i)
        mid = machine["id"] if machine else ""
        return {
            "pane_id": pid, "tab_id": "%s:%s" % (ws, tab),
            "terminal_id": "t%d" % i, "workspace_id": ws, "ws_label": wsl,
            "ws_number": int(ws[1:]), "tab_label": tabl, "tab_number": 1,
            "agent": agent, "name": name, "status": status, "title": title,
            "cwd": cwd, "short_cwd": _short_cwd(cwd), "proc": proc,
            "age": since, "since": since, "flag": "", "focused": focused,
            "seq": 100 - i, "machine": machine["label"] if machine else "",
            "machine_id": mid, "key": "%s/%s" % (mid, pid) if mid else pid,
            "zoomed": False,
        }
    klaw = DEMO_MACHINES[0]
    return [
        mk(1, "w1", "webapp", "t1", "1", "/Users/dev/webapp", "claude", "oauth",
           "blocked", "Add OAuth login flow with refresh tokens", "12m"),
        mk(2, "w2", "api", "t1", "hotfix", "/Users/dev/api", "codex", "",
           "done", "Fix flaky payment webhook tests", "1h04m"),
        mk(3, "w1", "webapp", "t1", "1", "/Users/dev/webapp", "claude", "db",
           "working", "Migrate database to Postgres 17", "26m"),
        mk(4, "w3", "dotfiles", "t2", "zsh", "/Users/dev/dotfiles", "codex", "",
           "working", "Refactor zsh prompt into modules", "8m"),
        mk(5, "w4", "blog", "t1", "1", "/Users/dev/blog", "claude", "writer",
           "idle", "Write post: terminal multiplexers in 2026", "2h11m",
           focused=True),
        mk(6, "w2", "api", "t1", "hotfix", "/Users/dev/api", None, "", "shell",
           "", "", proc="zsh"),
        mk(7, "w2", "api", "t2", "logs", "/Users/dev/api", None, "", "shell",
           "", "", proc="tail"),
        mk(8, "w4", "blog", "t1", "1", "/Users/dev/blog", None, "", "shell",
           "", "", proc="nvim"),
        mk(1, "w1", "kenneth-bot", "t1", "operations", "/Users/bot", "claude",
           "", "idle", "klaw", "3h", machine=klaw),
        mk(3, "w1", "kenneth-bot", "t1", "operations", "/Users/bot/klaw-mcp",
           "claude", "", "working", "Cswap keychain cleanup", "4m", machine=klaw),
        mk(2, "w1", "kenneth-bot", "t1", "operations", "/Users/bot/klaw-mcp",
           None, "", "shell", "", "", proc="zsh", machine=klaw),
    ]


def _process_name(pane_id, machine_id=""):
    try:
        if machine_id:
            info = _cli_result(["--machine", machine_id, "pane", "process-info",
                                "--pane", pane_id], timeout=6.0)
        else:
            info = herdr_request("pane.process_info", {"pane_id": pane_id},
                                 timeout=1.5)
        info = info.get("process_info") or {}
    except (OSError, RuntimeError):
        return "", []
    procs = info.get("foreground_processes") or []
    if not procs:
        return "", []
    p = procs[-1]
    name = p.get("argv0") or p.get("name") or ""
    return os.path.basename(name).lstrip("-"), p.get("argv") or []


def _build_rows(panes, ws_info, tabs, agents, machine=None, zoomed_tabs=()):
    """Rows for one server. `machine` is None for the server we run in."""
    mid = machine["id"] if machine else ""
    rows = []
    for p in panes:
        ws = ws_info.get(p.get("workspace_id")) or {}
        tab = tabs.get(p.get("tab_id")) or {}
        ag = agents.get(p.get("pane_id")) or {}
        tokens = p.get("tokens") or {}
        agent = p.get("agent")
        status = p.get("agent_status") or "unknown"
        if not agent:
            status = "shell"
        title = (p.get("title") or tokens.get("title")
                 or p.get("terminal_title_stripped") or "")
        cwd = p.get("foreground_cwd") or p.get("cwd") or ""
        pid = p.get("pane_id")
        rows.append({
            "pane_id": pid,
            "tab_id": p.get("tab_id"),
            "terminal_id": p.get("terminal_id"),
            "workspace_id": p.get("workspace_id"),
            "ws_label": ws.get("label") or p.get("workspace_id") or "",
            "ws_number": ws.get("number") or 0,
            "tab_label": tab.get("label") or str(tab.get("number") or ""),
            "tab_number": tab.get("number") or 0,
            "agent": agent,
            "name": ag.get("name") or "",
            "status": status,
            "title": title,
            "label": p.get("label") or "",
            "cwd": cwd,
            "short_cwd": _short_cwd(cwd),
            "proc": "",
            "age": tokens.get("age") or "",
            "since": tokens.get("since") or "",
            "flag": tokens.get("flag") or "",
            "focused": bool(p.get("focused")),
            "seq": ag.get("state_change_seq") or 0,
            # machine label/id ("" here), and the identity used for MRU,
            # selection and dry runs: pane id, or "<machine id>/<pane id>"
            "machine": machine["label"] if machine else "",
            "machine_id": mid,
            "key": "%s/%s" % (mid, pid) if mid else pid,
            "zoomed": p.get("tab_id") in zoomed_tabs,
        })
    return rows


def _finish_rows(rows):
    """Foreground process names for plain shells; drop our own popup pane."""
    shells = [r for r in rows if not r["agent"]]
    if shells:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for r, (name, argv) in zip(shells, ex.map(
                    lambda r: _process_name(r["pane_id"], r["machine_id"]), shells)):
                r["proc"] = name
                if not r["machine_id"] and any(a.endswith("fuzzygo.py") for a in argv):
                    r["self"] = True
    rows = [r for r in rows if not r.get("self")]
    for r in rows:
        if not r["title"]:
            r["title"] = r["label"] or r["proc"] or r["agent"] or ""
    return rows


def load_local_rows():
    """Panes of the server we run in, merged with agent names and labels."""
    panes = herdr_request("pane.list", {}).get("panes") or []
    ws_info = {}
    try:
        for ws in herdr_request("workspace.list", {}).get("workspaces") or []:
            ws_info[ws.get("workspace_id")] = ws
    except (OSError, RuntimeError):
        pass
    agents = {}
    try:
        for a in herdr_request("agent.list", {}).get("agents") or []:
            agents[a.get("pane_id")] = a
    except (OSError, RuntimeError):
        pass
    tabs = {}
    for ws_id in ws_info:
        try:
            for t in herdr_request("tab.list", {"workspace_id": ws_id}).get("tabs") or []:
                tabs[t.get("tab_id")] = t
        except (OSError, RuntimeError):
            pass
    return _finish_rows(_build_rows(panes, ws_info, tabs, agents))


def load_machine_rows(machine):
    """Panes of one saved SSH machine, from a single forwarded snapshot."""
    snap = _cli_result(["--machine", machine["id"], "api", "snapshot"],
                       timeout=10.0).get("snapshot") or {}
    ws_info = {w.get("workspace_id"): w for w in snap.get("workspaces") or []}
    tabs = {t.get("tab_id"): t for t in snap.get("tabs") or []}
    agents = {a.get("pane_id"): a for a in snap.get("agents") or []}
    zoomed = {l.get("tab_id") for l in snap.get("layouts") or [] if l.get("zoomed")}
    return _finish_rows(_build_rows(snap.get("panes") or [], ws_info, tabs,
                                    agents, machine, zoomed))


REMOTE_REFRESH = 4.0   # seconds between saved-machine refreshes


class Remote:
    """Rows from saved SSH machines, refreshed on a worker thread.

    Listing a machine costs SSH round trips (a snapshot, then process info
    per shell), so the popup paints local rows at once and remote rows join
    when they arrive, then refresh every REMOTE_REFRESH seconds."""

    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.at = 0.0
        self.machines = None     # None until listed once
        self.rows = []
        self.errors = {}         # machine label -> message

    @staticmethod
    def _one(m):
        try:
            return load_machine_rows(m)
        except (OSError, RuntimeError) as e:
            return e

    def _fetch(self):
        machines = list_machines()
        rows, errors = [], {}
        if machines:
            with ThreadPoolExecutor(max_workers=4) as ex:
                for m, res in zip(machines, ex.map(self._one, machines)):
                    if isinstance(res, Exception):
                        errors[m["label"]] = str(res)
                    else:
                        rows.extend(res)
        return machines, rows, errors

    def pending(self):
        return self.future is not None and self.at == 0.0

    def poll(self, wait=False):
        """(machines, rows, errors); starts a refresh when the last is stale."""
        if self.machines is None:
            self.machines = list_machines()      # local and cheap; seeds `selected`
        if self.future is None and time.time() - self.at >= REMOTE_REFRESH:
            self.future = self.pool.submit(self._fetch)
        if self.future is not None and (wait or self.future.done()):
            try:
                self.machines, self.rows, self.errors = self.future.result()
            except Exception as e:                      # worker bug: show, don't crash
                self.errors = {"saved machines": str(e)}
            self.future = None
            self.at = time.time()
        return self.machines, self.rows, self.errors


REMOTE = Remote()


def load_rows(wait=False):
    """All panes: this server's plus every enabled saved SSH machine's.

    Returns (rows, machines, errors). Remote rows lag behind local ones
    unless `wait` (see Remote)."""
    if DEMO:
        return _demo_rows(), list(DEMO_MACHINES), {}
    rows = load_local_rows()
    machines, remote_rows, errors = REMOTE.poll(wait)
    return rows + remote_rows, machines, errors


def selected_machine(machines):
    """Id of the machine the herdr client is viewing ("" for Local)."""
    return next((m["id"] for m in machines if m.get("selected")), "")


# --------------------------------------------------------------------- MRU


def _mru_path():
    return os.path.join(state_dir(), "mru.json")


def load_mru():
    try:
        with open(_mru_path()) as f:
            v = json.load(f)
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def save_mru(mru):
    mru = mru[:64]
    p = _mru_path()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(mru, f)
        os.replace(tmp, p)
    except OSError:
        pass


def mru_touch(mru, pane_id):
    if not pane_id:
        return mru
    return [pane_id] + [x for x in mru if x != pane_id]


# ------------------------------------------------------------- fuzzy match


def _is_boundary(s, i):
    if i == 0:
        return True
    prev, cur = s[i - 1], s[i]
    if not prev.isalnum():
        return True
    return prev.islower() and cur.isupper()


def fuzzy(term, text, case_sensitive):
    """fzf-v1 style: forward scan, then backward tighten; returns (score, positions)."""
    if not term:
        return 0, []
    hay = text if case_sensitive else text.lower()
    # forward pass: find the end of the first subsequence match
    ti = 0
    end = -1
    for i, ch in enumerate(hay):
        if ch == term[ti]:
            ti += 1
            if ti == len(term):
                end = i
                break
    if end < 0:
        return None, []
    # backward pass from `end`: latest possible start, tightest window
    pos = []
    ti = len(term) - 1
    for i in range(end, -1, -1):
        if hay[i] == term[ti]:
            pos.append(i)
            ti -= 1
            if ti < 0:
                break
    pos.reverse()
    score = 0
    prev = -2
    for p in pos:
        score += 16
        if _is_boundary(text, p):
            score += 10
        if p == prev + 1:
            score += 6
        prev = p
    score -= (pos[-1] - pos[0] + 1 - len(pos)) * 2   # gaps inside the window
    score -= min(pos[0], 10)                          # late start
    return score, pos


# Fields searched, with weights; highlights are keyed by field name.
FIELDS = (("name", 1.3), ("title", 1.15), ("ws_label", 1.1), ("tab_label", 1.0),
          ("machine", 1.0), ("proc", 1.0), ("short_cwd", 0.85), ("agent", 0.9),
          ("pane_id", 0.9), ("status", 0.8))


def match_row(row, terms, case_sensitive):
    """Every term must match some field. Returns (score, {field: positions})."""
    total = 0
    hl = {}
    for term in terms:
        best = None
        for field, weight in FIELDS:
            text = row.get(field) or ""
            if not text:
                continue
            sc, pos = fuzzy(term, text, case_sensitive)
            if sc is None:
                continue
            sc *= weight
            if best is None or sc > best[0]:
                best = (sc, field, pos)
        if best is None:
            return None, {}
        total += best[0]
        hl.setdefault(best[1], set()).update(best[2])
    return total, hl


def base_order(rows, mode, mru, focused_id):
    """Order with an empty query. Returns list of rows."""
    def tier_key(r):
        return (TIER.get(r["status"], 4), -r["seq"], r["machine"],
                r["ws_number"], r["tab_number"], r["pane_id"])
    rest = sorted(rows, key=tier_key)
    if mode == "go":
        pos = {k: i for i, k in enumerate(mru)}
        recent = sorted([r for r in rest if r["key"] in pos and
                         r["key"] != focused_id],
                        key=lambda r: pos[r["key"]])
        seen = {r["key"] for r in recent}
        rest = recent + [r for r in rest if r["key"] not in seen]
    # the pane you are already in goes last — Enter on the top row always moves
    return ([r for r in rest if r["key"] != focused_id]
            + [r for r in rest if r["key"] == focused_id])


def rank(rows, query, mode, mru, focused_id):
    ordered = base_order(rows, mode, mru, focused_id)
    terms = [t for t in query.split() if t]
    if not terms:
        return [(r, {}) for r in ordered]
    case_sensitive = any(c.isupper() for c in query)
    out = []
    for idx, r in enumerate(ordered):
        sc, hl = match_row(r, terms, case_sensitive)
        if sc is None:
            continue
        out.append((sc, idx, r, hl))
    out.sort(key=lambda t: (-t[0], t[1]))
    return [(r, hl) for _, _, r, hl in out]


# ------------------------------------------------------------------- focus


def _focus_local(row):
    pid = row["pane_id"]
    res = herdr_request("pane.focus", {"pane_id": pid})
    if (res.get("pane") or {}).get("focused"):
        return
    # belt and braces: walk the hierarchy if a bare pane.focus did not land
    herdr_request("workspace.focus", {"workspace_id": row["workspace_id"]})
    herdr_request("tab.focus", {"tab_id": row["tab_id"]})
    herdr_request("pane.focus", {"pane_id": pid})


def _focus_remote(row):
    m = ["--machine", row["machine_id"]]
    if row["agent"]:
        _cli_result(m + ["agent", "focus", row["pane_id"]])
        return
    # No forwarded CLI command focuses a plain pane by id (`pane focus` is
    # directional), so land in its workspace and tab, then zoom it: zoom moves
    # focus to the pane. Zoom back off unless the tab was zoomed already.
    _cli_result(m + ["workspace", "focus", row["workspace_id"]])
    _cli_result(m + ["tab", "focus", row["tab_id"]])
    _cli_result(m + ["pane", "zoom", row["pane_id"], "--on"])
    if not row.get("zoomed"):
        _cli_result(m + ["pane", "zoom", row["pane_id"], "--off"])


def focus_pane(row, current_machine=""):
    """Focus the pane on its server. `current_machine` is the machine the
    herdr client is viewing ("" for Local); herdr offers no API to switch the
    client's machine, so when they differ the pane is focused where it lives
    and a toast tells the user to pick that machine in the sidebar."""
    dry = os.environ.get("FUZZYGO_DRY_RUN")
    if dry:
        with open(dry, "w") as f:
            f.write(row["key"] + "\n")
        return None
    if DEMO:
        return None
    try:
        if row["machine_id"]:
            _focus_remote(row)
        else:
            _focus_local(row)
    except (OSError, RuntimeError) as e:
        return "focus failed: %s" % e
    if row["machine_id"] != current_machine:
        target = row["machine"] or "Local"
        try:
            herdr_request("notification.show", {
                "title": "Switch to %s in the sidebar" % target,
                "body": "%s is focused on %s. Click the machine or use "
                        "prefix+w; plugins cannot switch machines." % (
                            _wtrunc(row["title"] or row["proc"] or row["pane_id"], 40),
                            target)})
        except (OSError, RuntimeError):
            pass
    return None


# ------------------------------------------------------------------- theme

# Palettes extracted from herdr's own themes (src/app/state.rs), as the
# herdr-agent-inbox plugin does; [theme.custom] hex overrides win.
_PALETTES = {
    "catppuccin": {"accent": (137, 180, 250), "red": (243, 139, 168), "yellow": (249, 226, 175), "green": (166, 227, 161), "teal": (148, 226, 213), "blue": (137, 180, 250), "mauve": (203, 166, 247), "overlay0": (108, 112, 134), "surface0": (49, 50, 68)},
    "catppuccin-latte": {"accent": (30, 102, 245), "red": (210, 15, 57), "yellow": (223, 142, 29), "green": (64, 160, 43), "teal": (23, 146, 153), "blue": (30, 102, 245), "mauve": (136, 57, 239), "overlay0": (156, 160, 176), "surface0": (204, 208, 218)},
    "tokyo-night": {"accent": (122, 162, 247), "red": (247, 118, 142), "yellow": (224, 175, 104), "green": (158, 206, 106), "teal": (125, 207, 255), "blue": (122, 162, 247), "mauve": (187, 154, 247), "overlay0": (86, 95, 137), "surface0": (36, 40, 59)},
    "tokyo-night-day": {"accent": (46, 125, 233), "red": (245, 42, 101), "yellow": (140, 108, 62), "green": (88, 117, 57), "teal": (17, 140, 116), "blue": (46, 125, 233), "mauve": (120, 71, 189), "overlay0": (137, 144, 179), "surface0": (196, 200, 218)},
    "dracula": {"accent": (189, 147, 249), "red": (255, 85, 85), "yellow": (241, 250, 140), "green": (80, 250, 123), "teal": (139, 233, 253), "blue": (139, 233, 253), "mauve": (255, 121, 198), "overlay0": (98, 114, 164), "surface0": (68, 71, 90)},
    "nord": {"accent": (136, 192, 208), "red": (191, 97, 106), "yellow": (235, 203, 139), "green": (163, 190, 140), "teal": (143, 188, 187), "blue": (129, 161, 193), "mauve": (180, 142, 173), "overlay0": (76, 86, 106), "surface0": (59, 66, 82)},
    "gruvbox": {"accent": (215, 153, 33), "red": (251, 73, 52), "yellow": (250, 189, 47), "green": (184, 187, 38), "teal": (142, 192, 124), "blue": (131, 165, 152), "mauve": (211, 134, 155), "overlay0": (146, 131, 116), "surface0": (60, 56, 54)},
    "gruvbox-light": {"accent": (7, 102, 120), "red": (157, 0, 6), "yellow": (181, 118, 20), "green": (121, 116, 14), "teal": (66, 123, 88), "blue": (7, 102, 120), "mauve": (143, 63, 113), "overlay0": (146, 131, 116), "surface0": (235, 219, 178)},
    "one-dark": {"accent": (97, 175, 239), "red": (224, 108, 117), "yellow": (229, 192, 123), "green": (152, 195, 121), "teal": (86, 182, 194), "blue": (97, 175, 239), "mauve": (198, 120, 221), "overlay0": (92, 99, 112), "surface0": (44, 49, 58)},
    "one-light": {"accent": (64, 120, 242), "red": (228, 86, 73), "yellow": (193, 132, 1), "green": (80, 161, 79), "teal": (1, 132, 188), "blue": (64, 120, 242), "mauve": (166, 38, 164), "overlay0": (160, 161, 167), "surface0": (240, 240, 241)},
    "solarized": {"accent": (38, 139, 210), "red": (220, 50, 47), "yellow": (181, 137, 0), "green": (133, 153, 0), "teal": (42, 161, 152), "blue": (38, 139, 210), "mauve": (211, 54, 130), "overlay0": (88, 110, 117), "surface0": (7, 54, 66)},
    "solarized-light": {"accent": (38, 139, 210), "red": (220, 50, 47), "yellow": (181, 137, 0), "green": (133, 153, 0), "teal": (42, 161, 152), "blue": (38, 139, 210), "mauve": (211, 54, 130), "overlay0": (147, 161, 161), "surface0": (238, 232, 213)},
    "kanagawa": {"accent": (126, 156, 216), "red": (195, 64, 67), "yellow": (192, 163, 110), "green": (118, 148, 106), "teal": (127, 180, 202), "blue": (126, 156, 216), "mauve": (149, 127, 184), "overlay0": (114, 113, 105), "surface0": (42, 42, 55)},
    "kanagawa-lotus": {"accent": (77, 105, 155), "red": (200, 64, 83), "yellow": (119, 113, 63), "green": (111, 137, 78), "teal": (78, 140, 162), "blue": (77, 105, 155), "mauve": (98, 76, 131), "overlay0": (160, 156, 172), "surface0": (220, 213, 172)},
    "rose-pine": {"accent": (196, 167, 231), "red": (235, 111, 146), "yellow": (246, 193, 119), "green": (49, 116, 143), "teal": (156, 207, 216), "blue": (49, 116, 143), "mauve": (196, 167, 231), "overlay0": (110, 106, 134), "surface0": (31, 29, 46)},
    "rose-pine-dawn": {"accent": (144, 122, 169), "red": (180, 99, 122), "yellow": (234, 157, 52), "green": (40, 105, 131), "teal": (86, 148, 159), "blue": (40, 105, 131), "mauve": (144, 122, 169), "overlay0": (152, 147, 165), "surface0": (242, 233, 225)},
    "vesper": {"accent": (255, 199, 153), "red": (255, 128, 128), "yellow": (255, 199, 153), "green": (153, 255, 228), "teal": (102, 221, 204), "blue": (176, 176, 176), "mauve": (255, 209, 168), "overlay0": (92, 92, 92), "surface0": (35, 35, 35)},
}


def _theme_from_toml_lines(text):
    """Pick `[theme] name` and `[theme.custom]` strings out of a config without
    tomllib (Python < 3.11, e.g. the macOS system python3 that a launchd-started
    herdr server finds on its minimal PATH). Only quoted string values are read,
    which is all the theme tables hold."""
    theme, section = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line[1:line.find("]")].strip() if "]" in line else None
            continue
        m = re.match(r'([A-Za-z0-9_-]+)\s*=\s*"((?:[^"\\]|\\.)*)"', line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if section == "theme":
            theme[key] = val
        elif section == "theme.custom":
            theme.setdefault("custom", {})[key] = val
    return theme


def theme_palette():
    path = os.environ.get("HERDR_CONFIG_PATH") or os.path.expanduser(
        "~/.config/herdr/config.toml")
    name, custom = None, {}
    try:
        try:
            import tomllib
        except ImportError:
            tomllib = None
        if tomllib:
            with open(path, "rb") as f:
                theme = tomllib.load(f).get("theme") or {}
        else:
            with open(path, encoding="utf-8", errors="replace") as f:
                theme = _theme_from_toml_lines(f.read())
        name = theme.get("name")
        custom = theme.get("custom") or {}
    except (OSError, ValueError):
        pass
    pal = dict(_PALETTES.get(name or "catppuccin") or _PALETTES["catppuccin"])
    for key, val in custom.items():
        if isinstance(val, str) and val.startswith("#") and len(val) == 7:
            try:
                pal[key] = tuple(int(val[i:i + 2], 16) for i in (1, 3, 5))
            except ValueError:
                pass
    return pal


def _rgb_to_256(r, g, b):
    def cube(v):
        return 0 if v < 48 else 1 if v < 115 else (v - 35) // 40
    qr, qg, qb = cube(r), cube(g), cube(b)
    steps = [0, 95, 135, 175, 215, 255]
    cube_idx = 16 + 36 * qr + 6 * qg + qb
    cube_dist = (steps[qr] - r) ** 2 + (steps[qg] - g) ** 2 + (steps[qb] - b) ** 2
    gray = max(0, min(23, (sum((r, g, b)) // 3 - 8) // 10))
    gv = 8 + gray * 10
    gray_dist = (gv - r) ** 2 + (gv - g) ** 2 + (gv - b) ** 2
    return (232 + gray) if gray_dist < cube_dist else cube_idx


# ---------------------------------------------------------------- display


def _cw(ch):
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def _ww(s):
    return sum(_cw(ch) for ch in s)


def _wtrunc(s, cols, ellipsis="…"):
    if _ww(s) <= cols:
        return s
    if cols <= 1:
        return ellipsis[:cols]
    out, used = [], 0
    for ch in s:
        cw = _cw(ch)
        if used + cw > cols - 1:
            break
        out.append(ch)
        used += cw
    return "".join(out) + ellipsis


def _pad(s, cols):
    return s + " " * max(0, cols - _ww(s))


class Painter:
    """Width-aware segment writer that never writes past the window edge."""

    def __init__(self, win, width):
        self.win = win
        self.width = width

    def seg(self, y, x, s, attr):
        if x >= self.width or not s:
            return x
        s = _wtrunc(s, self.width - x, ellipsis="")
        try:
            self.win.addstr(y, x, s, attr)
        except curses.error:
            pass
        return x + _ww(s)

    def seg_hl(self, y, x, s, attr, hl_attr, positions):
        """Like seg, but characters at `positions` get hl_attr."""
        if not positions:
            return self.seg(y, x, s, attr)
        s = _wtrunc(s, self.width - x, ellipsis="…") if x < self.width else ""
        cx = x
        for i, ch in enumerate(s):
            if cx >= self.width:
                break
            try:
                self.win.addstr(y, cx, ch, hl_attr if i in positions else attr)
            except curses.error:
                pass
            cx += _cw(ch)
        return cx


def _loc(r):
    """Location column: workspace/tab, prefixed by the machine label if remote."""
    loc = "%s/%s" % (r["ws_label"], r["tab_label"])
    return "%s:%s" % (r["machine"], loc) if r["machine"] else loc


def counts_summary(rows):
    c = {}
    for r in rows:
        c[r["status"]] = c.get(r["status"], 0) + 1
    parts = []
    for st in ("blocked", "done", "working", "idle", "unknown", "shell"):
        if c.get(st):
            parts.append((st, "%s%d" % (ICON[st], c[st])))
    return parts


# Escape sequences a terminal may send for keys when it ignores keypad mode
# (normal-mode CSI arrows) or that terminfo lacks. curses hands us a bare
# ESC for those; we finish the parse ourselves so ESC alone still closes.
_CSI_KEYS = {
    "[A": curses.KEY_UP, "[B": curses.KEY_DOWN, "[C": curses.KEY_RIGHT,
    "[D": curses.KEY_LEFT, "[H": curses.KEY_HOME, "[F": curses.KEY_END,
    "OA": curses.KEY_UP, "OB": curses.KEY_DOWN, "OC": curses.KEY_RIGHT,
    "OD": curses.KEY_LEFT, "OH": curses.KEY_HOME, "OF": curses.KEY_END,
    "[1~": curses.KEY_HOME, "[4~": curses.KEY_END, "[5~": curses.KEY_PPAGE,
    "[6~": curses.KEY_NPAGE, "[3~": curses.KEY_DC,
}


def read_escape(stdscr):
    """Called after a bare ESC: return the key it introduces, or "\x1b"."""
    stdscr.nodelay(True)
    seq = ""
    try:
        deadline = time.time() + 0.05
        while time.time() < deadline and len(seq) < 8:
            try:
                c = stdscr.get_wch()
            except curses.error:
                if seq:
                    break
                time.sleep(0.005)
                continue
            if not isinstance(c, str):
                return c
            seq += c
            if seq in _CSI_KEYS:
                return _CSI_KEYS[seq]
            if seq[-1:] == "~" or (len(seq) >= 2 and seq[0] in "[O" and seq[-1].isalpha()):
                break
    finally:
        stdscr.nodelay(False)
        stdscr.timeout(1000)
    return "\x1b" if not seq else None   # unknown sequence: ignore it


# Colour roles -> (basic curses colour, theme palette key). Each role gets two
# pairs: one on the default background and one on the selection background.
# Never OR two colour pairs together: the pair bits add up to an undefined
# pair, which renders black on black.
_ROLES = (
    ("blocked", curses.COLOR_RED, "red"),
    ("working", curses.COLOR_YELLOW, "yellow"),
    ("done", curses.COLOR_CYAN, "teal"),
    ("idle", curses.COLOR_GREEN, "green"),
    ("name", curses.COLOR_BLUE, "blue"),
    ("ws", curses.COLOR_MAGENTA, "mauve"),
    ("dim", curses.COLOR_WHITE, "overlay0"),
    ("match", curses.COLOR_CYAN, "accent"),
    ("text", -1, None),
)
_EXTRA = {"blocked": curses.A_BOLD, "done": curses.A_BOLD, "match": curses.A_BOLD}


class Style:
    """attr(role, selected) -> curses attribute with the right pair."""

    def __init__(self):
        self.pairs = {}        # (role, selected) -> pair number or None
        self.reverse_sel = True
        self.setup()

    def setup(self):
        try:
            curses.use_default_colors()
        except curses.error:
            return
        pal = theme_palette()
        full = curses.COLORS >= 256
        sel_bg = -1
        if full:
            bg = pal.get("selection_bg") or pal.get("active_row_bg") or pal.get("surface0")
            if bg:
                sel_bg = _rgb_to_256(*bg)
                self.reverse_sel = False
        n = 1
        for role, basic, key in _ROLES:
            fg = basic
            if full and key and key in pal:
                fg = _rgb_to_256(*pal[key])
            for selected in (False, True):
                if selected and self.reverse_sel:
                    continue
                try:
                    curses.init_pair(n, fg, sel_bg if selected else -1)
                    self.pairs[(role, selected)] = n
                except curses.error:
                    pass
                n += 1

    def attr(self, role, selected=False):
        role = role if any(r[0] == role for r in _ROLES) else "text"
        if role in ("unknown", "shell"):
            role = "dim"
        extra = _EXTRA.get(role, 0)
        if selected and self.reverse_sel:
            pair = self.pairs.get((role, False))
            return (curses.color_pair(pair) if pair else 0) | extra | curses.A_REVERSE
        pair = self.pairs.get((role, selected))
        if pair is None:
            pair = self.pairs.get((role, False))
        return (curses.color_pair(pair) if pair else 0) | extra


def run(stdscr):
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    st = Style()

    def A(role, selected=False):
        if role in ("unknown", "shell"):
            role = "dim"
        return st.attr(role, selected)

    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    stdscr.keypad(True)
    stdscr.timeout(1000)

    mode = os.environ.get("FUZZYGO_MODE", "go")
    if mode not in MODES:
        mode = "go"
    query = ""
    sel = 0
    sel_pid = None
    top = 0
    status_msg = ""
    error = ""
    rows = []
    machines = []
    remote_errors = {}
    current = ""          # machine the client is viewing ("" = Local)
    mru = load_mru()
    focused_id = None
    last_load = 0.0
    last_click = (0.0, -1)

    def reload():
        nonlocal rows, machines, remote_errors, current, error, last_load, focused_id, mru
        try:
            rows, machines, remote_errors = load_rows()
            error = ""
        except (OSError, RuntimeError) as e:
            error = "herdr unreachable: %s" % e
        last_load = time.time()
        current = selected_machine(machines)
        f = [r for r in rows if r["focused"] and r["machine_id"] == current]
        if f and focused_id is None:
            focused_id = f[0]["key"]
            mru = mru_touch(mru, focused_id)
            save_mru(mru)

    reload()

    while True:
        ranked = rank(rows, query, mode, mru, focused_id)
        n = len(ranked)
        # keep the selection on the same pane while rows move underneath
        if sel_pid is not None:
            for i, (r, _) in enumerate(ranked):
                if r["key"] == sel_pid:
                    sel = i
                    break
        sel = max(0, min(sel, n - 1)) if n else 0
        sel_pid = ranked[sel][0]["key"] if n else None

        h, w = stdscr.getmaxyx()
        P = Painter(stdscr, w)
        stdscr.erase()
        list_top, list_bottom = 2, h - 1
        visible = max(1, list_bottom - list_top)
        if sel < top:
            top = sel
        if sel >= top + visible:
            top = sel - visible + 1
        top = max(0, min(top, max(0, n - visible)))

        # line 0: prompt
        x = P.seg(0, 0, " > ", A("match"))
        qx = P.seg(0, x, query, curses.A_BOLD)
        right = "%d/%d  %s" % (n, len(rows), mode)
        P.seg(0, max(qx + 2, w - _ww(right) - 1), right, A("dim"))

        # line 1: state counts
        x = P.seg(1, 1, "", 0)
        for stt, txt in counts_summary(rows):
            x = P.seg(1, x, txt, A(stt))
            x = P.seg(1, x, "  ", 0)
        if error:
            P.seg(1, x, error, A("blocked"))
        elif status_msg:
            P.seg(1, x, status_msg, A("dim"))
        elif remote_errors:
            P.seg(1, x, "; ".join("%s: %s" % kv for kv in sorted(remote_errors.items())),
                  A("dim"))
        elif machines and not DEMO and REMOTE.pending():
            P.seg(1, x, "loading %s…" % ", ".join(m["label"] for m in machines), A("dim"))

        # column widths for the right-hand side
        since_w = max([_ww(r["since"]) for r, _ in ranked] + [0])
        since_w = min(since_w, 8)
        cwd_w = min(max([_ww(r["short_cwd"]) for r, _ in ranked] + [0]), max(12, w // 4))
        loc_w = min(max([_ww(_loc(r)) for r, _ in ranked] + [0]), max(10, w // 5))
        name_w = min(max([_ww(r["name"] or r["agent"] or r["proc"] or "") for r, _ in ranked] + [0]), 16)

        for i in range(top, min(n, top + visible)):
            r, hl = ranked[i]
            y = list_top + (i - top)
            sel_row = (i == sel)
            blank = A("text", sel_row)
            if sel_row:
                P.seg(y, 0, " " * w, blank)
            stt = r["status"]
            x = P.seg(y, 1, ICON.get(stt, "?"), A(stt, sel_row))
            x = P.seg(y, x, " ", blank)
            # title (flex)
            fixed = 2 + name_w + 2 + loc_w + 2 + cwd_w + (2 + since_w if since_w else 0) + 2
            title_w = max(8, w - x - fixed)
            title = r["title"] or r["proc"] or "(shell)"
            if r["key"] == focused_id:
                title_attr = A("dim", sel_row)
            else:
                title_attr = A("text", sel_row) | (curses.A_BOLD if stt in ("blocked", "done") else 0)
            shown = _wtrunc(title, title_w)
            x = P.seg_hl(y, x, shown, title_attr, A("match", sel_row), hl.get("title"))
            x = P.seg(y, x, " " * (title_w - _ww(shown)) + "  ", blank)
            # name column: agent name, else agent kind, else process
            if r["name"]:
                ntxt, nkey, nrole = r["name"], "name", "name"
            elif r["agent"]:
                ntxt, nkey, nrole = r["agent"], "agent", "dim"
            else:
                ntxt, nkey, nrole = r["proc"], "proc", "dim"
            ntxt_t = _wtrunc(ntxt, name_w)
            x = P.seg_hl(y, x, ntxt_t, A(nrole, sel_row), A("match", sel_row), hl.get(nkey))
            x = P.seg(y, x, " " * (name_w - _ww(ntxt_t)) + "  ", blank)
            # [machine:]workspace/tab
            wsl, tbl = r["ws_label"], r["tab_label"]
            loc = _wtrunc(_loc(r), loc_w)
            off = len(r["machine"]) + 1 if r["machine"] else 0
            m_pos = set(hl.get("machine") or ())
            ws_pos = {off + p for p in (hl.get("ws_label") or set())}
            tb_pos = {off + len(wsl) + 1 + p for p in (hl.get("tab_label") or set())}
            x = P.seg_hl(y, x, loc[:off], A("name", sel_row), A("match", sel_row), m_pos)
            x = P.seg_hl(y, x, loc[off:], A("ws", sel_row), A("match", sel_row),
                         {p - off for p in ws_pos | tb_pos})
            x = P.seg(y, x, " " * (loc_w - _ww(loc)) + "  ", blank)
            # cwd
            cwd = _wtrunc(r["short_cwd"], cwd_w)
            x = P.seg_hl(y, x, cwd, A("dim", sel_row), A("match", sel_row), hl.get("short_cwd"))
            x = P.seg(y, x, " " * (cwd_w - _ww(cwd)), blank)
            # since
            if since_w:
                x = P.seg(y, x, "  ", blank)
                x = P.seg(y, x, _pad(r["since"], since_w), A("dim", sel_row))
            if r["key"] == focused_id:
                P.seg(y, x, " ◂", A("dim", sel_row))

        # footer
        help_line = (" enter:jump  tab:%s  ctrl+u:clear  esc:close" %
                     ("inbox order" if mode == "go" else "recent order"))
        P.seg(h - 1, 0, _wtrunc(help_line, w - 1), A("dim"))
        try:
            stdscr.move(0, min(qx, w - 1))
        except curses.error:
            pass
        stdscr.refresh()

        try:
            ch = stdscr.get_wch()
        except curses.error:
            ch = None
        if ch == "\x1b":
            ch = read_escape(stdscr)
        if ch is None:
            if time.time() - last_load >= 1.0:
                reload()
            continue
        status_msg = ""

        def jump(idx):
            nonlocal status_msg, mru
            if not n or idx >= n:
                return False
            row = ranked[idx][0]
            err = focus_pane(row, current)
            if err:
                status_msg = err
                return False
            mru = mru_touch(mru, row["key"])
            save_mru(mru)
            return True

        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except curses.error:
                continue
            if list_top <= my < list_top + visible:
                idx = top + (my - list_top)
                if idx < n and bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED
                                         | curses.BUTTON1_DOUBLE_CLICKED):
                    now = time.time()
                    double = (bstate & curses.BUTTON1_DOUBLE_CLICKED) or (
                        last_click[1] == idx and now - last_click[0] < 0.4)
                    last_click = (now, idx)
                    sel = idx
                    sel_pid = ranked[idx][0]["key"]
                    if double and jump(idx):
                        return
            continue
        if ch == curses.KEY_RESIZE:
            continue
        if isinstance(ch, str):
            code = ord(ch)
            if ch == "\x1b" or code == 3 or code == 7:          # esc, ctrl+c, ctrl+g
                return
            if ch in ("\n", "\r"):
                if jump(sel):
                    return
                continue
            if ch == "\t":
                mode = MODES[(MODES.index(mode) + 1) % len(MODES)]
                sel, sel_pid, top = 0, None, 0
                continue
            if code == 21:                                      # ctrl+u
                query, sel, sel_pid, top = "", 0, None, 0
                continue
            if code == 23:                                      # ctrl+w
                query = query.rstrip()
                query = query[:query.rfind(" ") + 1] if " " in query else ""
                sel, sel_pid, top = 0, None, 0
                continue
            if code in (8, 127):                                # backspace
                query = query[:-1]
                sel, sel_pid, top = 0, None, 0
                continue
            if code in (10, 14):                                # ctrl+j, ctrl+n
                sel, sel_pid = min(sel + 1, max(n - 1, 0)), None
                continue
            if code in (11, 16):                                # ctrl+k, ctrl+p
                sel, sel_pid = max(sel - 1, 0), None
                continue
            if ch.isprintable():
                query += ch
                sel, sel_pid, top = 0, None, 0
            continue
        # special keys
        if ch in (curses.KEY_BACKSPACE,):
            query = query[:-1]
            sel, sel_pid, top = 0, None, 0
        elif ch == curses.KEY_DOWN:
            sel, sel_pid = min(sel + 1, max(n - 1, 0)), None
        elif ch == curses.KEY_UP:
            sel, sel_pid = max(sel - 1, 0), None
        elif ch == curses.KEY_NPAGE:
            sel, sel_pid = min(sel + visible, max(n - 1, 0)), None
        elif ch == curses.KEY_PPAGE:
            sel, sel_pid = max(sel - visible, 0), None
        elif ch == curses.KEY_HOME:
            sel, sel_pid = 0, None
        elif ch == curses.KEY_END:
            sel, sel_pid = max(n - 1, 0), None
        elif ch == curses.KEY_ENTER:
            if jump(sel):
                return


# -------------------------------------------------------------------- main


def dump(query):
    rows, machines, errors = load_rows(wait=True)
    for label, msg in sorted(errors.items()):
        print("# %s: %s" % (label, msg), file=sys.stderr)
    mode = os.environ.get("FUZZYGO_MODE", "go")
    current = selected_machine(machines)
    focused = next((r["key"] for r in rows if r["focused"] and r["machine_id"] == current), None)
    for r, hl in rank(rows, query, mode if mode in MODES else "go", load_mru(), focused):
        shown_id = "%s/%s" % (r["machine"], r["pane_id"]) if r["machine"] else r["pane_id"]
        print("%s %-12s %-7s %-40s %-14s %-24s %-30s %s%s" % (
            ICON[r["status"]], shown_id, r["status"],
            _wtrunc(r["title"] or r["proc"], 40),
            r["name"] or r["agent"] or r["proc"],
            _wtrunc(_loc(r), 24),
            _wtrunc(r["short_cwd"], 30), r["since"],
            "  ◂" if r["key"] == focused else ""))


def main():
    os.umask(0o077)
    args = [a for a in sys.argv[1:] if a != "--demo"]
    if args and args[0] == "--dump":
        dump(" ".join(args[1:]))
        return 0
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(run)
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        try:
            with open(os.path.join(state_dir(), "crash.log"), "a") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S\n"))
                traceback.print_exc(file=f)
        except OSError:
            pass
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
