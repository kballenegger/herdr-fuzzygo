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

Stdlib Python only. Talks to herdr through its public socket API and plugin
manifest; nothing is patched.

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

Requires herdr ≥ 0.9 and `python3` (3.11+ for theme colours) on PATH.

```sh
herdr plugin install kballenegger/herdr-fuzzygo
```

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

### Developing

```sh
python3 fuzzygo.py --demo              # popup with canned data, no herdr
python3 fuzzygo.py --dump [query]      # print the ranked rows for a query
herdr plugin link "$PWD"               # register this checkout with a live herdr
FUZZYGO_DRY_RUN=/tmp/choice python3 fuzzygo.py   # Enter writes the pane id instead of focusing
```

## License

MIT. Theme palettes are the ones herdr ships; the approach of reading them
from `[theme]` follows herdr-agent-inbox.
