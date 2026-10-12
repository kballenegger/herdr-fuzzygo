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
python3 fuzzygo.py --dump [query]         # ranked rows as text against the live herdr (waits for saved machines)
herdr plugin link "$PWD"                  # (re)register; manifest changes need a re-link
FUZZYGO_DRY_RUN=/tmp/c python3 fuzzygo.py # Enter writes the chosen pane id to /tmp/c
```

Test the curses UI headlessly by driving it in a `pty` (see git history for
the harness used: fork a pty, set 120x30, write keys, read `FUZZYGO_DRY_RUN`).

## Shape

- `load_rows()` returns `(rows, machines, errors)`: `load_local_rows()`
  merges `pane.list` + `workspace.list` + `tab.list` + `agent.list` over the
  socket, then `pane.process_info` for shells (thread pool). A pane whose
  foreground argv ends in `fuzzygo.py` is the popup itself and is dropped.
  Saved SSH machines (`herdr machine list --json`, enabled only) come from
  `Remote`: a worker thread runs `herdr --machine <id> api snapshot` (and
  `pane process-info` per shell) through `HERDR_BIN_PATH`, cached and
  refreshed every `REMOTE_REFRESH` seconds, so the popup never blocks on
  SSH. Every row has `machine` (label, "" locally), `machine_id` and `key`
  (`pane_id`, or `<machine id>/<pane id>`); MRU, selection, the focused
  marker and dry runs all use `key`, never `pane_id`.
- herdr has no API to change which machine the client views (only sidebar
  clicks, `prefix+w` and notification targets do; verified in herdr 0.9.3
  source, `src/client/shell/endpoint_navigation.rs`). `focus_pane(row,
  current_machine)` focuses the pane on its own server and, when that is
  not the viewed machine, sends a `notification.show` toast telling the
  user to switch. Remote focus: `agent focus <pane id>` for agents; shells
  need `workspace focus` + `tab focus` + `pane zoom --on` (zoom moves focus)
  then `--off` unless the tab was zoomed, since the forwarded `pane focus`
  is directional only.
- `rank()` = `base_order()` (go: MRU then tiers; inbox: tiers; focused pane
  last) filtered and scored by `match_row()` → `fuzzy()` per field with
  `FIELDS` weights. Highlights are per field, keyed by field name.
- Rendering goes through `Painter.seg/seg_hl` — display-width aware
  (`_wtrunc`), never past the window edge. Colours follow herdr's sidebar
  language (blocked red, working yellow, done teal, idle green, shells dim)
  via `theme_palette()`, resolved by `Style.attr(role, selected)`, which
  keeps one pair per role on the default background and one on the
  selection background. Never OR two `color_pair()` values together: the
  pair bits add up to an undefined pair that renders black on black (this
  was the "right columns invisible on the selected row" bug in 0.1.0).
- `focus_pane()` uses `pane.focus`, verifies `focused`, and walks
  workspace → tab → pane if needed. `herdr_request` raises only
  `RuntimeError`/`OSError`; keep it that way.
- State dir is `<socket dir>/fuzzygo-state/` (mru.json, crash.log), derived
  from `HERDR_SOCKET_PATH` so the CLI and popup agree.
