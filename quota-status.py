#!/usr/bin/env python3
# Kimi Code statusline: model | Ctx% | Kimi plan quota (5h/week[/month]) | cwd | git
#
# Kimi Code caps status_line commands at 300ms, so the render path only reads a
# local cache; when the cache is stale a detached `--refresh` child refills it
# (network fetch + atomic replace). Colors: green <60%, yellow 60-84%, red >=85%.
import json
import os
import sys
import time
from datetime import datetime, timezone

HOME = os.path.expanduser('~/.kimi-code')
DIR = os.path.join(HOME, 'scripts')
CACHE = os.path.join(DIR, 'quota-cache')
def _env_int(name, default, lo, hi):
    # Optional numeric knob from the environment (same pattern as QUOTA_DEBUG):
    # unset or unparsable -> default; otherwise clamped into [lo, hi].
    try:
        return max(lo, min(hi, int(os.environ.get(name, ''))))
    except ValueError:
        return default


TTL = _env_int('QUOTA_TTL', 300, 15, 3600)  # seconds a cached quota string is fresh
RETRY = 30         # seconds before retrying after a failed/in-flight refresh


def col(p):
    return '31' if p >= 85 else '33' if p >= 60 else '32'


def seg(name, p, reset_dt=None):
    r = ''
    if reset_dt:
        dt = reset_dt.astimezone()
        now = datetime.now(timezone.utc).astimezone()
        f = '%H:%M' if dt.date() == now.date() else '%m/%d %H:%M'
        r = f' (rst {dt.strftime(f)})'
    return f'\033[{col(p)}m{name} {p:.0f}%{r}\033[0m'


def parse_iso(s):
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None


def load_token():
    # OAuth access token (kept fresh by the running CLI)
    try:
        d = json.load(open(os.path.join(HOME, 'credentials', 'kimi-code.json')))
        if d.get('access_token') and d.get('expires_at', 0) > time.time() + 30:
            return d['access_token']
    except Exception:
        pass
    # Fall back to a plain api_key in config.toml
    try:
        import tomllib
        with open(os.path.join(HOME, 'config.toml'), 'rb') as f:
            cfg = tomllib.load(f)
        for prov in (cfg.get('providers') or {}).values():
            if 'api.kimi.com/coding' in (prov.get('base_url') or ''):
                key = prov.get('api_key') or ''
                if key:
                    return key
    except Exception:
        pass
    return None


def fetch_quota():
    import urllib.request  # deferred: only the background refresher needs it
    key = load_token()
    if not key:
        return None
    req = urllib.request.Request(
        'https://api.kimi.com/coding/v1/usages',
        headers={'Authorization': f'Bearer {key}', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=8) as r:
        d = json.load(r)
    parts = []
    lims = d.get('limits') or []
    five = next(
        (l for l in lims
         if (l.get('window') or {}).get('duration') == 300
         and (l.get('window') or {}).get('timeUnit') == 'TIME_UNIT_MINUTE'),
        lims[0] if lims else None)  # fall back to the first window
    if five:
        det = five.get('detail') or {}
        lim = float(det.get('limit', 0)) or 1
        parts.append(seg('5h', float(det.get('used', 0)) / lim * 100,
                         parse_iso(det.get('resetTime'))))
    u = d.get('usage') or {}
    if u:
        lim = float(u.get('limit', 0)) or 1
        parts.append(seg('week', float(u.get('used', 0)) / lim * 100,
                         parse_iso(u.get('resetTime'))))
    t = d.get('totalQuota') or {}
    if t.get('limit'):
        lim = float(t.get('limit', 0)) or 1
        parts.append(seg('month', float(t.get('used', 0)) / lim * 100,
                         parse_iso(t.get('resetTime'))))
    return ' \033[90m·\033[0m '.join(parts) if parts else None


def thinking_seg(model_name):
    """Render the thinking level from config.toml (not in the stdin snapshot)."""
    try:
        import tomllib
        with open(os.path.join(HOME, 'config.toml'), 'rb') as f:
            cfg = tomllib.load(f)
    except Exception:
        return None
    th = cfg.get('thinking') or {}
    if th.get('enabled') is False:
        return '\033[90moff\033[0m'
    eff = th.get('effort')
    if not eff and model_name:
        for m in (cfg.get('models') or {}).values():
            if model_name in (m.get('display_name'), m.get('model')):
                eff = (m.get('overrides') or {}).get('default_effort') \
                    or m.get('default_effort')
                break
    return f'\033[36m{eff}\033[0m' if eff else None


def refresh():
    try:
        text = fetch_quota()
    except Exception:
        return
    if text:
        tmp = CACHE + '.tmp'
        with open(tmp, 'w') as f:
            f.write(text)
        os.replace(tmp, CACHE)


def maybe_refresh():
    try:
        age = time.time() - os.path.getmtime(CACHE)
    except OSError:
        open(CACHE, 'a').close()
        age = TTL + 1
    if age < TTL:
        return
    # Back the mtime off to "almost stale" so concurrent renders don't spawn a
    # storm, and a failed refresh is retried after RETRY seconds.
    t = time.time() - TTL + RETRY
    os.utime(CACHE, (t, t))
    import subprocess  # deferred: keep the render path under the 300ms cap
    subprocess.Popen([sys.executable, os.path.abspath(__file__), '--refresh'],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def read_cache():
    try:
        return open(CACHE).read().strip() or None
    except Exception:
        return None


def dig(d, *path):
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def main():
    if '--refresh' in sys.argv:
        refresh()
        return
    # Windows pipes default to the locale codepage; the TUI expects UTF-8.
    # 'replace' keeps stray lone surrogates (mojibake paths) from crashing print.
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    try:
        d = json.load(sys.stdin)
    except Exception:
        d = {}
    # Keep the last snapshot around so the schema is easy to inspect/debug.
    # Off by default (the statusline renders once per second); opt in with
    # QUOTA_DEBUG=1.
    if os.environ.get('QUOTA_DEBUG') == '1':
        try:
            with open(os.path.join(DIR, 'last-stdin.json'), 'w') as f:
                json.dump(d, f)
        except Exception:
            pass

    parts = []

    mode = d.get('permissionMode')
    if isinstance(mode, str) and mode:
        mc = {'yolo': '31', 'auto': '33', 'manual': '32'}.get(mode, '37')
        parts.append(f'\033[{mc}m{mode}\033[0m')

    model = d.get('model')
    if isinstance(model, dict):
        model = model.get('display_name') or model.get('id') or model.get('name')
    if model:
        parts.append(f'\033[36m{model}\033[0m')

    t = thinking_seg(model if isinstance(model, str) else None)
    if t:
        parts.append(t)

    q = read_cache()
    maybe_refresh()
    if q:
        parts.append(q)

    cwd = d.get('cwd')
    if cwd:
        home = os.path.expanduser('~')
        parts.append(cwd.replace(home, '~', 1) if cwd.startswith(home) else cwd)

    git = d.get('git') or d.get('gitBranch') or dig(d, 'git', 'branch')
    if isinstance(git, dict):
        git = git.get('branch')
    if git:
        parts.append(f'\033[35m{git}\033[0m')

    print(' \033[90m|\033[0m '.join(parts), end='')


main()
