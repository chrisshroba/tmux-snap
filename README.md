# tmux-snap

Capture, restore, and manage snapshots of tmux state — sessions, windows,
panes, layouts, cwds, scrollback, and the foreground process running in each
pane.

Claude Code conversations are detected by walking each pane's process tree and
stored by session UUID, so they can be `claude -r`-resumed on restore. Other
recognized commands (`watch`, `caffeinate`, `python -m http.server`, `tail -f`,
`htop`, `ssh`, `less`, …) are restored from their captured argv. Anything else
is captured as scrollback only.

## Install

Single-file [uv script](https://docs.astral.sh/uv/guides/scripts/). Symlink it
onto your `PATH`:

```sh
ln -s "$PWD/tmux-snap" ~/.local/bin/tmux-snap
```

## Usage

```sh
tmux-snap save                         # auto-named timestamped snapshot
tmux-snap save --name before-reboot
tmux-snap list
tmux-snap inspect --latest
tmux-snap inspect --pane 0:12.1
tmux-snap restore --latest --dry-run
tmux-snap restore --name before-reboot --ask
tmux-snap watch --interval 10m --keep 24
```

Snapshots live in `$TMUX_SNAP_DIR`, falling back to
`$XDG_DATA_HOME/tmux-snap`, falling back to `~/.local/share/tmux-snap`.

Run `tmux-snap --help` or `tmux-snap <subcommand> --help` for details.
