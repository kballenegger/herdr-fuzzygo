# CLAUDE.md

A herdr plugin: a fuzzy-finder popup over every pane in the session, with
live agent state, replacing herdr's built-in goto and the agent-inbox popup.
Stdlib Python only. Everything goes through herdr's public socket API and the
plugin manifest; verify against the installed CLI (`herdr --help`,
`herdr api schema --json`), never assume internals.

## Commands

```sh
python3 -m py_compile fuzzygo.py          # syntax gate
python3 fuzzygo.py --demo                 # popup with fixtures, no herdr needed
python3 fuzzygo.py --dump [query]         # ranked rows as text against the live herdr
herdr plugin link "$PWD"                  # (re)register; manifest changes need a re-link
FUZZYGO_DRY_RUN=/tmp/c python3 fuzzygo.py # Enter writes the chosen pane id to /tmp/c
```

Test the curses UI headlessly by driving it in a `pty` (see git history for
the harness used: fork a pty, set 120x30, write keys, read `FUZZYGO_DRY_RUN`).

## Shape

- `load_rows()` merges `pane.list` + `workspace.list` + `tab.list` +
  `agent.list`, then `pane.process_info` for shells (thread pool). A pane
  whose foreground argv ends in `fuzzygo.py` is the popup itself and is
  dropped.
- `rank()` = `base_order()` (go: MRU then tiers; inbox: tiers; focused pane
  last) filtered and scored by `match_row()` → `fuzzy()` per field with
  `FIELDS` weights. Highlights are per field, keyed by field name.
- Rendering goes through `Painter.seg/seg_hl` — display-width aware
  (`_wtrunc`), never past the window edge. Colours follow herdr's sidebar
  language (blocked red, working yellow, done teal, idle green, shells dim)
  via `theme_palette()`.
- `focus_pane()` uses `pane.focus`, verifies `focused`, and walks
  workspace → tab → pane if needed. `herdr_request` raises only
  `RuntimeError`/`OSError`; keep it that way.
- State dir is `<socket dir>/fuzzygo-state/` (mru.json, crash.log), derived
  from `HERDR_SOCKET_PATH` so the CLI and popup agree.
