#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Capture a full tmux state snapshot to a directory.

Layout:
    <output_dir>/
      snapshot.json          structured metadata for every session/window/pane
      scrollback/<s>_<w>_<p>.raw   raw bytes from `tmux capture-pane -e` (colors/escapes preserved)

Usage:
    save.py <output_dir> <scrollback_lines>

Restore policy (B-command set on save, run on restore):
    claude          → claude -r <session_id>
    watch           → original argv
    caffeinate      → original argv
    http_server     → original argv (python -m http.server …)
    tail_f          → original argv (tail -f …)
    journalctl_f    → original argv
    docker_logs_f   → original argv
    kubectl_logs_f  → original argv
    kubectl_watch   → original argv (kubectl get … -w)
    monitor         → original argv (htop/btop/top/atop)
    ssh / mosh      → original argv
    db_repl         → original argv (psql/mysql/redis-cli/sqlite3/bq)
    pager           → original argv (less/bat/more/man <file>)

Anything else (vim, npm test, build steps, mid-flight wizards, …) is captured
in scrollback only — not relaunched.
"""
import argparse, json, os, re, subprocess, sys, pathlib, datetime, shlex
from collections import Counter


def tmux(*args):
    return subprocess.run(['tmux', *args], capture_output=True, text=True, check=True).stdout


def ps_tree():
    """Return {ppid: [(pid, full_command), ...]} for the whole system."""
    out = subprocess.run(['ps', '-A', '-o', 'pid=,ppid=,command='],
                         capture_output=True, text=True, check=True).stdout
    tree = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, cmd = int(parts[0]), int(parts[1]), parts[2]
        tree.setdefault(ppid, []).append((pid, cmd))
    return tree


def descendants(tree, root_pid):
    out = []
    stack = [root_pid]
    while stack:
        cur = stack.pop()
        for pid, cmd in tree.get(cur, []):
            out.append((pid, cmd))
            stack.append(pid)
    return out


def lookup_claude_session(pid):
    f = pathlib.Path.home() / '.claude' / 'sessions' / f'{pid}.json'
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


# Foreground argv → (label, restorable?). Order matters — first match wins.
# Each regex is anchored at start of argv. Restorable entries → b_command = argv.
RESTORE_POLICIES = [
    ('watch',          r'watch\b'),
    ('caffeinate',     r'caffeinate\b'),
    ('http_server',    r'(python\S*|uv\s+run)\s+(-m\s+)?http\.server\b'),
    ('tail_f',         r'tail\s+(-\w*f\w*|.+\s-f)\b'),
    ('journalctl_f',   r'journalctl\b.*\s-f\b'),
    ('docker_logs_f',  r'docker(\s+compose)?\s+logs\b.*\s-f\b'),
    ('kubectl_logs_f', r'kubectl\s+logs\b.*\s-f\b'),
    ('kubectl_watch',  r'kubectl\s+get\b.*\s-w\b'),
    ('monitor',        r'(htop|btop|atop|top)\b'),
    ('ssh',            r'(ssh|mosh)\b'),
    ('db_repl',        r'(psql|mysql|redis-cli|sqlite3|bq)\b'),
    ('pager',          r'(less|bat|more|man)\b'),
]
RESTORE_POLICIES = [(label, re.compile(rx)) for label, rx in RESTORE_POLICIES]

NON_RESTORE_LABELS = {  # commands we recognize but choose NOT to restore.
    'editor': re.compile(r'(vim?|nvim|emacs|nano|hx|helix)\b'),
}

SHELL_BASES = {'zsh', 'bash', '-zsh', '-bash', 'sh', 'fish'}


def find_foreground(pane_pid, current_command, tree):
    """Walk descendants of pane_pid and return (pid, full_argv) for the
    likely foreground process. Falls back to (pane_pid, current_command).
    """
    descs = descendants(tree, pane_pid)
    # Prefer the deepest non-shell descendant whose head command matches
    # current_command. tmux's #{pane_current_command} is the foreground.
    if current_command:
        cc_base = current_command.split()[0]
        for pid, cmd in descs:
            head = cmd.split()[0] if cmd.split() else ''
            base = os.path.basename(head)
            if (base == cc_base or head == cc_base
                    or cmd.startswith(current_command + ' ')
                    or cmd == current_command):
                return pid, cmd
    # Fallback: deepest non-shell descendant.
    for pid, cmd in reversed(descs):
        head = cmd.split()[0] if cmd.split() else ''
        base = os.path.basename(head)
        if base not in SHELL_BASES:
            return pid, cmd
    return pane_pid, current_command or ''


def classify(pane_pid, current_command, tree):
    """Return dict with role, label, b_command, fg_argv, claude (or None)."""
    descs = descendants(tree, pane_pid)

    # Claude takes priority — current_command is often the version string.
    for pid, cmd in descs:
        head = cmd.split()[0] if cmd.split() else ''
        if os.path.basename(head) == 'claude':
            sess = lookup_claude_session(pid)
            claude_block = None
            if sess:
                claude_block = {
                    'pid': pid, 'argv': cmd,
                    'session_id': sess.get('sessionId'),
                    'cwd': sess.get('cwd'),
                    'version': sess.get('version'),
                    'name': sess.get('name'),
                    'status': sess.get('status'),
                }
            b = (f'claude -r {claude_block["session_id"]}'
                 if claude_block and claude_block.get('session_id') else None)
            return {
                'role': 'claude', 'label': 'claude', 'b_command': b,
                'fg_argv': cmd, 'restorable': bool(b), 'claude': claude_block,
            }

    fg_pid, fg_argv = find_foreground(pane_pid, current_command, tree)

    parts = fg_argv.split() if fg_argv else []
    head = parts[0] if parts else ''
    base = os.path.basename(head).lstrip('-')
    # Normalized argv: replace possibly-absolute head with its basename so
    # regexes don't have to match the full /usr/local/bin/foo path.
    norm_argv = ' '.join([base, *parts[1:]]) if parts else ''

    # Idle shell: foreground is just a shell with no other args, OR no
    # foreground at all.
    if base in SHELL_BASES and len(parts) <= 1:
        return {'role': 'shell', 'label': 'idle_shell', 'b_command': None,
                'fg_argv': fg_argv, 'restorable': True, 'claude': None}

    def _match(rx):
        return rx.match(norm_argv) or rx.match(fg_argv) or rx.match(base)

    # Restore policies.
    for label, rx in RESTORE_POLICIES:
        if _match(rx):
            return {'role': label, 'label': label, 'b_command': norm_argv,
                    'fg_argv': fg_argv, 'restorable': True, 'claude': None}

    # Recognized-but-not-restored.
    for label, rx in NON_RESTORE_LABELS.items():
        if _match(rx):
            return {'role': label, 'label': label, 'b_command': None,
                    'fg_argv': fg_argv, 'restorable': False, 'claude': None}

    # Unknown / arbitrary command.
    return {'role': 'other', 'label': 'other', 'b_command': None,
            'fg_argv': fg_argv, 'restorable': False, 'claude': None}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('output_dir')
    ap.add_argument('scrollback_lines', type=int,
                    help='Lines of scrollback to capture per pane (required)')
    args = ap.parse_args()

    out = pathlib.Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    sb_dir = out / 'scrollback'
    sb_dir.mkdir(exist_ok=True)

    tree = ps_tree()

    sessions_raw = tmux('list-sessions', '-F',
                        '#{session_id}\t#{session_name}\t#{session_attached}'
                        ).strip().splitlines()

    snapshot = {
        'captured_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'scrollback_lines': args.scrollback_lines,
        'tmux_version': tmux('-V').strip(),
        'host': os.uname().nodename,
        'sessions': [],
    }

    restored_counts = Counter()
    idle_shells = 0
    unrestored = []  # list of {address, fg_argv, title, role}

    for line in sessions_raw:
        sid, sname, attached = line.split('\t')
        sess = {'id': sid, 'name': sname, 'attached': attached == '1', 'windows': []}

        wins_raw = tmux('list-windows', '-t', sid, '-F',
                        '\t'.join([
                            '#{window_index}', '#{window_name}', '#{window_active}',
                            '#{window_width}', '#{window_height}', '#{window_layout}',
                        ])).strip().splitlines()
        for w in wins_raw:
            wi, wn, wa, ww, wh, wl = w.split('\t')
            win = {
                'index': int(wi), 'name': wn, 'active': wa == '1',
                'width': int(ww), 'height': int(wh), 'layout': wl,
                'panes': [],
            }
            panes_raw = tmux('list-panes', '-t', f'{sid}:{wi}', '-F',
                             '\t'.join([
                                 '#{pane_id}', '#{pane_index}', '#{pane_active}',
                                 '#{pane_pid}', '#{pane_current_command}',
                                 '#{pane_current_path}',
                                 '#{pane_left}', '#{pane_top}',
                                 '#{pane_width}', '#{pane_height}',
                                 '#{pane_title}',
                             ])).strip().splitlines()
            for p in panes_raw:
                pid_, pidx, pact, ppid_, pcmd, pcwd, pl_, pt_, pw_, ph_, ptitle = p.split('\t')

                # Capture scrollback with escape sequences.
                sb_name = f'{sname}_{wi}_{pidx}.raw'
                sb_path = sb_dir / sb_name
                cap = subprocess.run(
                    ['tmux', 'capture-pane', '-e', '-J', '-p',
                     '-S', f'-{args.scrollback_lines}', '-t', pid_],
                    capture_output=True, check=True
                ).stdout
                sb_path.write_bytes(cap)

                info = classify(int(ppid_), pcmd, tree)

                pane = {
                    'id': pid_,
                    'index': int(pidx),
                    'active': pact == '1',
                    'pid': int(ppid_),
                    'current_command': pcmd,
                    'cwd': pcwd,
                    'left': int(pl_), 'top': int(pt_),
                    'width': int(pw_), 'height': int(ph_),
                    'title': ptitle,
                    'role': info['role'],
                    'restore_label': info['label'],
                    'b_command': info['b_command'],
                    'foreground': info['fg_argv'],
                    'scrollback_file': str(sb_path.relative_to(out)),
                    'claude': info['claude'],
                }
                win['panes'].append(pane)

                # Tally.
                addr = f'{sname}:{wi}.{pidx}'
                if info['label'] == 'idle_shell':
                    idle_shells += 1
                elif info['restorable']:
                    restored_counts[info['label']] += 1
                else:
                    unrestored.append({
                        'address': addr, 'fg_argv': info['fg_argv'],
                        'title': ptitle, 'role': info['role'], 'cwd': pcwd,
                    })

            sess['windows'].append(win)
        snapshot['sessions'].append(sess)

    snapshot['stats'] = {
        'restored_counts': dict(restored_counts),
        'idle_shells': idle_shells,
        'unrestored': unrestored,
    }

    (out / 'snapshot.json').write_text(json.dumps(snapshot, indent=2))

    # Summary report.
    n_panes = sum(len(w['panes']) for s in snapshot['sessions'] for w in s['windows'])
    n_wins = sum(len(s['windows']) for s in snapshot['sessions'])
    n_sessions = len(snapshot['sessions'])

    print(f'Saved {n_sessions} session(s), {n_wins} window(s), {n_panes} pane(s) → {out}')
    print()
    print('Restored process types:')
    if restored_counts:
        width = max(len(k) for k in restored_counts) + 1
        for label in sorted(restored_counts, key=lambda k: -restored_counts[k]):
            print(f'  {label:<{width}}  {restored_counts[label]}')
    else:
        print('  (none)')
    print(f'Idle shells: {idle_shells}')
    print()
    n_unrestored = len(unrestored)
    print(f'Unrestored panes ({n_unrestored}):')
    if unrestored:
        for u in unrestored:
            argv = u['fg_argv'] or '(unknown)'
            if len(argv) > 90:
                argv = argv[:87] + '…'
            print(f'  {u["address"]:<14} [{u["role"]}]  {argv}')
            if u['title'] and u['title'] != argv:
                t = u['title'][:90] + ('…' if len(u['title']) > 90 else '')
                print(f'  {"":<14}   title: {t}')
    else:
        print('  (none)')


if __name__ == '__main__':
    main()
