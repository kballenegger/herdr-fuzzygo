# herdr-fuzzygo

**Fuzzy goto for [herdr](https://herdr.dev).** One popup, every pane in the
session — agents and plain shells alike — with live state. Type a few letters,
hit Enter, you're there.

```
 > fek█                                                       4/22  go
 !1  ●3  ▸7  ○5  ·6

 ▸ Feke animation smoothness         feke-video-a  feke/video    ~/D/d/azure/feke-app   18h01m
 ○ Fiki demo video production        feke-video-b  feke/video    ~/D/d/azure/feke-app   23h11m
 ▸ feke-pm                           feke-pm       feke/feke-pm  ~/D/d/azure/feke-app   2m     ◂
 · zsh                               zsh           feke/feke-pm  ~/D/d/azure/feke-app
```

It replaces two things at once:

- **the built-in `goto`** — instead of a menu to arrow through, a fuzzy
  finder over titles, agent names, workspace and tab labels, directories,
  process names, pane ids and states. Space-separated terms must all match
  (`feke video`, `caret working`, `zsh play`). Smart case.
- **the agent inbox popup** — every row carries herdr's state language
  (`!` blocked, `●` finished, `▸` working, `○` idle, `·` shell), in your
  theme's colours, with the session title and time-in-state when the
  [herdr-agent-inbox](https://github.com/douglascorrea/herdr-agent-inbox)
  plugin is installed.

Two orderings, toggled with `tab`:

- **go** (default for `prefix+g`): the pane you came from is the top row, then
  the panes you jumped to most recently, then everything else by attention.
  So `prefix+g` `enter` flips back to where you were, like alt-tab.
- **inbox** (default for `prefix+i`): attention first — blocked, finished,
  working, idle, shells. The pane you are already in always sorts last, so
  Enter on the top row always moves you somewhere.

Panes on your saved SSH machines (`herdr machine list`) are in the list too,
prefixed with the machine label (`klaw:kenneth-bot/operations`), searchable
by that label, refreshed every few seconds. See [Other machines](#other-machines).

Stdlib Python only. Talks to herdr through its public socket API, CLI and
plugin manifest; nothing is patched.

## Keys

| Key | Action |
|---|---|
| type | filter |
| `enter` / double-click | jump to the selected pane |
| `↑` `↓` `ctrl+j` `ctrl+k` `ctrl+n` `ctrl+p` | move |
| `pgup` `pgdn` `home` `end` | page / first / last |
| `tab` | toggle go / inbox ordering |
| `ctrl+u` / `ctrl+w` | clear query / delete a word |
| `esc` `ctrl+c` | close |

## Install

Requires herdr ≥ 0.9 and `python3` (3.9+) on PATH.

```sh
herdr plugin install kballenegger/herdr-fuzzygo
```

Install it on **every machine you view through herdr**, with the key
bindings below in each machine's `config.toml`. A key binding runs on the
server the client is currently looking at, and herdr plugins live per
server, so a plugin that is only installed locally does nothing while you
are viewing a saved SSH machine (the key either falls through or herdr
reports "custom command is not available on this endpoint").

Then in `~/.config/herdr/config.toml` unbind the built-in goto and point
`prefix+g` / `prefix+i` at the plugin:

```toml
[keys]
goto = ""   # replaced by herdr-fuzzygo

[[keys.command]]
key = "prefix+g"
type = "shell"
description = "fuzzy goto pane (recent first)"
command = "herdr plugin pane open --plugin herdr-fuzzygo --entrypoint go"

[[keys.command]]
key = "prefix+i"
type = "shell"
description = "panes & agents inbox (attention first)"
command = "herdr plugin pane open --plugin herdr-fuzzygo --entrypoint inbox"
```

and `herdr server reload-config`.

## How it works

`fuzzygo.py` is the whole plugin. On open it calls `pane.list`,
`workspace.list`, `tab.list` and `agent.list` over the herdr socket, merges
them into one row per pane, and asks `pane.process_info` for the foreground
command of each plain shell (in parallel; ~150 ms for 20 panes). Rows refresh
every second while the popup is open. Enter calls `pane.focus`, falling back
to `workspace.focus` → `tab.focus` → `pane.focus` if that did not land, and
the popup closes when the process exits.

Matching is fzf-v1 style per field (forward scan, backward tighten; bonuses
for word boundaries and runs, penalties for gaps and late starts), with
field weights so an agent *name* beats a *title* beats a *path*. Matched
characters are highlighted.

The recent-jump list lives in `<herdr config dir>/fuzzygo-state/mru.json`.

### Other machines

herdr's socket only reaches the server the popup runs in, so enabled saved
SSH machines are listed through the CLI's forwarding: one
`herdr --machine <id> api snapshot` per machine (plus `pane process-info`
per plain shell) on a worker thread, so the local rows paint at once and the
remote ones join a few hundred milliseconds later. Unreachable machines show
a dim note on the status line instead of blocking the popup.

Enter on a remote row focuses the pane on its own server: `agent focus` for
agent panes; for plain shells `workspace focus`, `tab focus`, then
`pane zoom --on` (which moves focus to the pane) and `--off` again, because
no forwarded command focuses a pane by id. One limit herdr imposes: a plugin
cannot switch which machine the herdr *client* is looking at — only the
sidebar, the workspace picker (`prefix+w`) and notification targets do that.
So when the pane lives on a machine other than the one you are viewing, the
popup focuses it there and shows a toast asking you to pick that machine in
the sidebar; it is then the focused pane when you arrive. The missing API is
requested upstream in herdr discussions
[#3749](https://github.com/herdrdev/herdr/discussions/3749) and
[#4007](https://github.com/herdrdev/herdr/discussions/4007); once herdr
honours a machine-qualified focus, Enter will land there in one step.

The MRU list keys remote panes as `<machine id>/<pane id>`; local panes keep
their bare id.

The popup itself always runs on the machine you are viewing, so it must be
installed there (see Install). On a remote machine it lists that server's
panes plus whatever machines *that* server has saved. The herdr server does
not carry your login shell's PATH (a launchd-started server on macOS sees
only `/usr/bin:/bin:/usr/sbin:/sbin`), so `python3` is usually the system
one; the plugin runs on 3.9 and reads the theme without `tomllib` there.

### Developing

```sh
python3 fuzzygo.py --demo              # popup with canned data, no herdr
python3 fuzzygo.py --dump [query]      # print the ranked rows for a query
herdr plugin link "$PWD"               # register this checkout with a live herdr
FUZZYGO_DRY_RUN=/tmp/choice python3 fuzzygo.py   # Enter writes the pane key instead of focusing
```

`--dump` waits for the saved machines and prints their panes as
`<label>/<pane id>`; machine errors go to stderr as `# <label>: <error>`.

```sh
```

## License

MIT. Theme palettes are the ones herdr ships; the approach of reading them
from `[theme]` follows herdr-agent-inbox.
