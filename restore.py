#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Restore a tmux state snapshot produced by save.py.

For each pane:
  A. cat the captured scrollback (raw escape sequences → colors preserved),
     then print a marker so you can see where restored content ends.
  B. (optional) `claude -r <session_id>` for claude panes; the original
     `watch ...` command for watch panes; nothing otherwise.
  C. `exec zsh` so once B exits (or by default if absent) you land in a fresh
     shell at the original cwd.

Layout sizes are scaled proportionally if the current terminal differs from
the captured size.

Usage:
    restore.py <input_dir> [--target-width W] [--target-height H]
"""
import argparse, json, os, subprocess, sys, pathlib, shlex


def tmux(*args, check=True):
    r = subprocess.run(['tmux', *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f'tmux {args!r} failed: {r.stderr.strip()}')
    return r.stdout


# ---------- tmux layout string parser/serializer ----------

def layout_csum(s: str) -> str:
    c = 0
    for ch in s.encode():
        c = ((c >> 1) | ((c & 1) << 15)) & 0xFFFF
        c = (c + ch) & 0xFFFF
    return f'{c:04x}'


def parse_layout(s):
    # strip "<csum>,"
    assert s[4] == ','
    cell, end = _parse_cell(s, 5)
    assert end == len(s), f'trailing junk in layout: {s[end:]!r}'
    return cell


def _parse_cell(s, i):
    j = s.index('x', i); w = int(s[i:j])
    k = s.index(',', j); h = int(s[j+1:k])
    m = s.index(',', k+1); x = int(s[k+1:m])
    n = m + 1
    while n < len(s) and s[n] not in ',{}[]':
        n += 1
    y = int(s[m+1:n])
    cell = {'w': w, 'h': h, 'x': x, 'y': y, 'kind': 'leaf',
            'children': [], 'paneid': None}
    if n >= len(s) or s[n] in '}],':
        if n < len(s) and s[n] == ',':
            # Could be sibling separator OR the leaf-pane-id separator.
            # Distinguish: leaf-pane-id when followed by digits-only until
            # closer/comma/end.
            tail_start = n + 1
            tail_end = tail_start
            while tail_end < len(s) and s[tail_end].isdigit():
                tail_end += 1
            tail_terminator = s[tail_end] if tail_end < len(s) else ''
            if (tail_end > tail_start and tail_terminator in ',}]'):
                cell['paneid'] = int(s[tail_start:tail_end])
                return cell, tail_end
            if tail_end > tail_start and tail_terminator == '':
                cell['paneid'] = int(s[tail_start:tail_end])
                return cell, tail_end
        return cell, n
    if s[n] in '{[':
        cell['kind'] = 'h' if s[n] == '{' else 'v'
        close = '}' if s[n] == '{' else ']'
        n += 1
        while True:
            child, n = _parse_cell(s, n)
            cell['children'].append(child)
            if s[n] == ',':
                n += 1
                continue
            if s[n] == close:
                n += 1
                break
        return cell, n
    raise ValueError(f'parse error at {n}: {s[n:n+10]!r}')


def serialize_cell(cell):
    s = f'{cell["w"]}x{cell["h"]},{cell["x"]},{cell["y"]}'
    if cell['kind'] == 'leaf':
        if cell['paneid'] is not None:
            s += f',{cell["paneid"]}'
        return s
    open_, close_ = ('{', '}') if cell['kind'] == 'h' else ('[', ']')
    return s + open_ + ','.join(serialize_cell(c) for c in cell['children']) + close_


def scale_tree(cell, sx, sy, base_x=0, base_y=0):
    """Scale dimensions while keeping siblings flush."""
    cell['x'] = base_x
    cell['y'] = base_y
    cell['w'] = max(1, round(cell['w'] * sx))
    cell['h'] = max(1, round(cell['h'] * sy))
    if cell['kind'] == 'leaf':
        return
    n = len(cell['children'])
    if cell['kind'] == 'h':
        # Distribute width; last child absorbs remainder so they line up.
        total = cell['w']
        used = 0
        for i, c in enumerate(cell['children']):
            if i == n - 1:
                cw = total - used
            else:
                cw = max(1, round(c['w'] * sx))
                used += cw
            c['w'] = cw
            scale_tree(c, sx, sy, base_x + (used - cw if i < n - 1 else used - cw if False else (sum(cell['children'][k]['w'] for k in range(i)) + base_x if False else 0)), base_y)
        # Re-walk to set correct x offsets cleanly
        off = base_x
        for c in cell['children']:
            scale_tree(c, 1, 1, off, base_y) if False else None
            c['x'] = off
            c['y'] = base_y
            c['h'] = cell['h']
            # children of c need their own scaling already applied — redo:
            if c['kind'] != 'leaf':
                _propagate(c)
            off += c['w']
    else:  # vertical
        total = cell['h']
        used = 0
        for i, c in enumerate(cell['children']):
            if i == n - 1:
                ch = total - used
            else:
                ch = max(1, round(c['h'] * sy))
                used += ch
            c['h'] = ch
        off = base_y
        for c in cell['children']:
            c['x'] = base_x
            c['y'] = off
            c['w'] = cell['w']
            if c['kind'] != 'leaf':
                _propagate(c)
            off += c['h']


def _propagate(cell):
    """Recompute children x/y/w-or-h to fit `cell` (post-scaling tidy-up)."""
    if cell['kind'] == 'leaf':
        return
    n = len(cell['children'])
    if cell['kind'] == 'h':
        total = cell['w']
        # Distribute by current ratios of children w
        ws = [c['w'] for c in cell['children']]
        s = sum(ws) or 1
        used = 0
        for i, c in enumerate(cell['children']):
            if i == n - 1:
                c['w'] = total - used
            else:
                c['w'] = max(1, round(ws[i] / s * total))
                used += c['w']
            c['h'] = cell['h']
        off = cell['x']
        for c in cell['children']:
            c['x'] = off
            c['y'] = cell['y']
            _propagate(c)
            off += c['w']
    else:
        total = cell['h']
        hs = [c['h'] for c in cell['children']]
        s = sum(hs) or 1
        used = 0
        for i, c in enumerate(cell['children']):
            if i == n - 1:
                c['h'] = total - used
            else:
                c['h'] = max(1, round(hs[i] / s * total))
                used += c['h']
            c['w'] = cell['w']
        off = cell['y']
        for c in cell['children']:
            c['y'] = off
            c['x'] = cell['x']
            _propagate(c)
            off += c['h']


def rescale_layout(layout: str, target_w: int, target_h: int) -> str:
    root = parse_layout(layout)
    sx = target_w / root['w']
    sy = target_h / root['h']
    # First pass: scale leaf dimensions naively, then tidy-up siblings.
    _scale_naive(root, sx, sy)
    root['w'] = target_w
    root['h'] = target_h
    root['x'] = 0
    root['y'] = 0
    _propagate(root)
    body = serialize_cell(root)
    return f'{layout_csum(body)},{body}'


def _scale_naive(cell, sx, sy):
    cell['w'] = max(1, round(cell['w'] * sx))
    cell['h'] = max(1, round(cell['h'] * sy))
    for c in cell['children']:
        _scale_naive(c, sx, sy)


# ---------- main restore ----------

def make_init_script(init_dir, key, cwd, sb_path, b_command):
    path = init_dir / f'{key}.sh'
    sh = ['#!/bin/bash']
    sh.append(f'cd {shlex.quote(cwd)} 2>/dev/null || true')
    # Print scrollback (raw bytes; escape sequences will be honored).
    sh.append(f'cat {shlex.quote(str(sb_path))} 2>/dev/null')
    # Marker, dim style.
    sh.append(r'printf "\n\033[2;3m=== restored scrollback above ===\033[0m\n"')
    if b_command:
        sh.append(b_command)
    # Always finish in zsh at original cwd.
    sh.append(f'cd {shlex.quote(cwd)} 2>/dev/null || true')
    sh.append('exec zsh')
    path.write_text('\n'.join(sh) + '\n')
    path.chmod(0o755)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input_dir')
    ap.add_argument('--target-width', type=int, default=None,
                    help='Override target tmux client width (cells).')
    ap.add_argument('--target-height', type=int, default=None,
                    help='Override target tmux client height (cells).')
    args = ap.parse_args()

    in_dir = pathlib.Path(args.input_dir).resolve()
    snap = json.loads((in_dir / 'snapshot.json').read_text())

    # Determine target client size.
    if args.target_width and args.target_height:
        tw, th = args.target_width, args.target_height
    else:
        try:
            sz = os.get_terminal_size()
            tw = args.target_width or sz.columns
            th = args.target_height or sz.lines
        except OSError:
            tw = args.target_width or 200
            th = args.target_height or 50

    init_dir = in_dir / 'init'
    init_dir.mkdir(exist_ok=True)

    for sess in snap['sessions']:
        sname = sess['name']
        # Skip if the target session already exists.
        existing = subprocess.run(['tmux', 'has-session', '-t', f'={sname}'],
                                  capture_output=True).returncode == 0
        if existing:
            print(f'Session {sname!r} already exists; skipping.', file=sys.stderr)
            continue

        first_window = True
        for win in sess['windows']:
            wname = win['name']
            wi = win['index']
            first_pane = win['panes'][0]

            if first_window:
                tmux('new-session', '-d', '-s', sname, '-n', wname,
                     '-x', str(tw), '-y', str(th),
                     '-c', first_pane['cwd'])
                # Tmux uses base-index for the first window; force our index.
                actual = tmux('list-windows', '-t', sname, '-F',
                              '#{window_index}').strip().split()[0]
                if actual != str(wi):
                    tmux('move-window', '-s', f'{sname}:{actual}',
                         '-t', f'{sname}:{wi}')
                first_window = False
            else:
                tmux('new-window', '-t', f'{sname}:{wi}', '-n', wname,
                     '-c', first_pane['cwd'])

            target_window = f'{sname}:{wi}'

            # Create remaining panes (split direction is irrelevant; we'll
            # call select-layout afterwards to fix sizes/positions).
            for pane in win['panes'][1:]:
                tmux('split-window', '-t', target_window, '-c', pane['cwd'])

            # Apply scaled layout. Fall back to original on failure.
            try:
                scaled = rescale_layout(win['layout'], tw, th)
                tmux('select-layout', '-t', target_window, scaled)
            except Exception as e:
                print(f'  layout scale failed ({e}); using original', file=sys.stderr)
                try:
                    tmux('select-layout', '-t', target_window, win['layout'])
                except Exception:
                    pass

            # Per-pane init.
            for i, pane in enumerate(win['panes']):
                pane_target = f'{target_window}.{i}'
                key = f'{sname}_{wi}_{i}'
                sb_path = (in_dir / pane['scrollback_file']).resolve()
                init_path = make_init_script(init_dir, key, pane['cwd'],
                                             sb_path, pane.get('b_command'))
                # exec replaces the just-spawned shell, so no stray prompt
                # accumulates beyond a single line.
                tmux('send-keys', '-t', pane_target,
                     f'exec bash {shlex.quote(str(init_path))}', 'Enter')

            # Restore active pane.
            for i, pane in enumerate(win['panes']):
                if pane['active']:
                    tmux('select-pane', '-t', f'{target_window}.{i}')
                    break

        # Restore active window for this session.
        for win in sess['windows']:
            if win['active']:
                tmux('select-window', '-t', f'{sname}:{win["index"]}')
                break

    sname0 = snap['sessions'][0]['name'] if snap['sessions'] else ''
    print(f'Restored. Attach with:  tmux attach -t {sname0}')


if __name__ == '__main__':
    main()
