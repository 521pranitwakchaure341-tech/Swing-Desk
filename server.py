#!/usr/bin/env python3
"""Swing Desk, local version.  Pulls REAL prices from free sources, finds swing-trade ideas,
runs a real backtest, and serves the website at http://127.0.0.1:8765

Uses only Python's built-in libraries (Python 3.9 or newer). Nothing to install.
It only SUGGESTS. It never places orders and never asks for broker passwords.
Optional: set the environment variable ANTHROPIC_API_KEY to turn on the Claude (Haiku) features.
"""
import csv, datetime as dt, io, json, math, os, sys, threading, time, urllib.parse, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

BASE = os.path.dirname(os.path.abspath(__file__))
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
PORT = int(os.environ.get('SWING_PORT', '8765'))
MOCK = os.environ.get('SWING_MOCK') == '1'          # testing only: makes up prices. Never set this for real use.
DATA = os.path.join(BASE, 'data_mock' if MOCK else 'data'); PRICES = os.path.join(DATA, 'prices')
os.makedirs(PRICES, exist_ok=True)
HISTORY_RANGE = '10y'
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36', 'Accept': '*/*'}
# Claude cost guard. ASSUMED prices in US$ per million tokens and an assumed exchange rate: check current pricing and edit.
HAIKU_MODEL = 'claude-haiku-5-5'; PRICE_IN, PRICE_OUT, USD_INR = 1.0, 5.0, 88.0
CAPITAL = 1_000_000; RISK = {1: 2.0, 2: 1.0, 3: 0.25}; SMALL_CAP_RISK = 2.0   # used only for the bot's own paper trades
MIN_TV_CR = {'Large': 5.0, 'Mid': 3.0, 'Small': 1.5}                       # minimum average daily traded value, crore rupees
UNI_URLS = {
    'Large': ['https://archives.nseindia.com/content/indices/ind_nifty200list.csv', 'https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv'],
    'Mid':   ['https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv', 'https://www.niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv'],
    'Small': ['https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv', 'https://www.niftyindices.com/IndexConstituent/ind_niftysmallcap250list.csv']}
STRAT_WK = {'brk': 6, 'pb': 3, 'mr': 2}
USE_NSE = False                                     # the phone build sets True: prices come from NSE's own daily files (Yahoo blocks GitHub's servers)
NSE_START = dt.date(2016, 11, 1)                    # how far back to build history (it deepens a little on every run)
BUDGET_MIN = None                                   # set by the phone build: stop downloading after this many minutes and carry on next run
LOCK = threading.Lock()
STATUS = {'running': False, 'msg': 'Idle', 'done': 0, 'total': 0}
CACHE = {'state': None, 'ind': {}, 'bars': {}, 'meta': {}, 'nifty': None, 'vix': None}

def now(): return dt.datetime.now(IST)
def log(*a): print(time.strftime('%H:%M:%S'), *a, flush=True)
def jload(p, d=None):
    try:
        with open(p, encoding='utf-8') as f: return json.load(f)
    except Exception: return d
def jsave(p, o):
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f: json.dump(o, f)
    os.replace(tmp, p)

def http_get(url, tries=3, timeout=25, headers=None):
    last = None
    for k in range(tries):
        try:
            u = url.replace('query1.finance.yahoo.com', 'query2.finance.yahoo.com') if k % 2 and 'query1.finance.yahoo.com' in url else url   # try Yahoo's second address too
            req = urllib.request.Request(u, headers=dict(UA, **(headers or {})))
            with urllib.request.urlopen(req, timeout=timeout) as r: return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (404, 403): break
            time.sleep(10 * (k + 1) if e.code == 429 else 1.5 * (k + 1))
        except Exception as e:
            last = e; time.sleep(1.5 * (k + 1))
    raise last

# ---------------------------------------------------------------- universe
def load_universe():
    uni, errs = {}, []
    if MOCK:
        import random; r = random.Random(5)
        for seg, n in (('Large', 60), ('Mid', 40), ('Small', 40)):
            for i in range(n):
                s = f'MOCK{seg[0]}{i}'; uni[s] = {'sym': s, 'name': f'Mock {seg} {i}', 'sec': r.choice(['Metals', 'IT', 'Pharma', 'Auto', 'Banks']), 'seg': seg}
        return uni, errs, False
    for seg in ('Large', 'Mid', 'Small'):
        got = False
        for u in UNI_URLS[seg]:
            try:
                lp = os.path.join(DATA, 'lists', os.path.basename(u))          # a list you saved by hand wins if the download is blocked
                txt = None
                try: txt = http_get(u).decode('utf-8-sig', 'replace')
                except Exception:
                    if os.path.exists(lp): txt = open(lp, encoding='utf-8-sig').read()
                    else: raise
                rows = list(csv.DictReader(io.StringIO(txt)))
                for row in rows:
                    sym = (row.get('Symbol') or '').strip()
                    if sym and sym not in uni: uni[sym] = {'sym': sym, 'name': (row.get('Company Name') or sym).strip(), 'sec': (row.get('Industry') or '').strip().title(), 'seg': seg}
                got = len(rows) > 20
                if got: break
            except Exception as e: errs.append(f'{seg}: {e}')
        if not got: errs.append(f'Could not read the {seg} list from NSE')
    cached = False
    if len(uni) < 100:
        old = jload(os.path.join(DATA, 'universe.json'))
        if old: uni, cached = old, True
    else: jsave(os.path.join(DATA, 'universe.json'), uni)
    return uni, errs, cached

# ---------------------------------------------------------------- prices
def parse_yahoo(raw):
    j = json.loads(raw); res = j['chart']['result'][0]
    ts = res.get('timestamp') or []; q = res['indicators']['quote'][0]; out = {'d': [], 'o': [], 'h': [], 'l': [], 'c': [], 'v': []}
    for i, t in enumerate(ts):
        o, h, l, c, v = (q['open'][i], q['high'][i], q['low'][i], q['close'][i], q['volume'][i])
        if None in (o, h, l, c): continue
        d = dt.datetime.fromtimestamp(t, IST)
        if d.date() == now().date() and now().hour < 16: continue            # drop a part-finished day
        out['d'].append(d.date().isoformat()); out['o'].append(round(o, 2)); out['h'].append(round(h, 2)); out['l'].append(round(l, 2)); out['c'].append(round(c, 2)); out['v'].append(int(v or 0))
    return out

def mock_bars(sym, n=2600):
    import random; r = random.Random(sym); px = 100 + r.random() * 900; d = dt.date(2016, 11, 21); out = {'d': [], 'o': [], 'h': [], 'l': [], 'c': [], 'v': []}
    mu = r.uniform(0.0002, 0.0008); end = now().date()
    while d <= end:
        if d.weekday() < 5:
            o = px; px *= 1 + mu + r.gauss(0, 0.017); hi = max(o, px) * (1 + abs(r.gauss(0, .006))); lo = min(o, px) * (1 - abs(r.gauss(0, .006)))
            out['d'].append(d.isoformat()); out['o'].append(round(o, 2)); out['h'].append(round(hi, 2)); out['l'].append(round(lo, 2)); out['c'].append(round(px, 2)); out['v'].append(int(2e5 * (1 + abs(r.gauss(0, .5)))))
        d += dt.timedelta(days=1)
    return out

def yahoo_symbol(sym):
    return {'NIFTY': '^NSEI', 'VIX': '^INDIAVIX'}.get(sym) or urllib.parse.quote(sym, safe='') + '.NS'

def get_bars(sym):
    path = os.path.join(PRICES, sym.replace('&', '_and_') + '.json'); old = jload(path)
    if MOCK:
        b = mock_bars(sym if sym not in ('NIFTY', 'VIX') else 'idx' + sym)
        if sym == 'VIX': b['c'] = [14 + 4 * math.sin(i / 90) + 2 for i in range(len(b['c']))]; b['o'] = b['h'] = b['l'] = b['c']
        return b, 'full'
    ys = yahoo_symbol(sym); base = 'https://query1.finance.yahoo.com/v8/finance/chart/' + ys + '?interval=1d&range='
    if old and old['d'] and (dt.date.fromisoformat(old['d'][-1]) >= now().date() - dt.timedelta(days=40)):
        new = parse_yahoo(http_get(base + '3mo'))                         # only the newest days
        idx = {d: i for i, d in enumerate(old['d'])}; bad = 0
        for i, d in enumerate(new['d']):
            if d in idx and abs(old['c'][idx[d]] / new['c'][i] - 1) > 0.02: bad += 1
        if bad == 0 or not new['d']:
            for i, d in enumerate(new['d']):
                if d in idx:
                    for k in 'ohlcv': old[k][idx[d]] = new[k][i]
            have = set(idx)
            for i, d in enumerate(new['d']):
                if d not in have:
                    for k in 'dohlcv': old[k].append(new[k][i])
            if len(old['d']) and old['d'] == sorted(old['d']): jsave(path, old); return old, 'update'
        # prices disagree with what we saved (probably a split or bonus): download everything again
    full = parse_yahoo(http_get(base + HISTORY_RANGE)); jsave(path, full); return full, 'full'

# ---------------------------------------------------------------- indicators
def sma(a, n):
    out = [None] * len(a); s = 0.0
    for i, v in enumerate(a):
        s += v
        if i >= n: s -= a[i - n]
        if i >= n - 1: out[i] = s / n
    return out
def rsi(c, n):
    out = [None] * len(c)
    if len(c) <= n: return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]; g += max(d, 0); l += max(-d, 0)
    g /= n; l /= n; out[n] = 100 if l == 0 else 100 - 100 / (1 + g / l)
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]; g = (g * (n - 1) + max(d, 0)) / n; l = (l * (n - 1) + max(-d, 0)) / n
        out[i] = 100 if l == 0 else 100 - 100 / (1 + g / l)
    return out
def atr(b, n=14):
    out = [None] * len(b['c']); tr = [0.0] * len(b['c'])
    for i in range(len(tr)):
        pc = b['c'][i - 1] if i else b['c'][0]; tr[i] = max(b['h'][i] - b['l'][i], abs(b['h'][i] - pc), abs(b['l'][i] - pc))
    if len(tr) <= n: return out
    a = sum(tr[1:n + 1]) / n; out[n] = a
    for i in range(n + 1, len(tr)): a = (a * (n - 1) + tr[i]) / n; out[i] = a
    return out
def rollmax(a, n, lag=0):                       # max of the n values ending `lag` bars before i
    out = [None] * len(a)
    for i in range(len(a)):
        lo = i - lag - n + 1
        if lo >= 0: out[i] = max(a[lo:i - lag + 1])
    return out
def prep(b):
    n = len(b['c']); tv = [b['c'][i] * b['v'][i] for i in range(n)]
    return {'s20': sma(b['c'], 20), 's50': sma(b['c'], 50), 's200': sma(b['c'], 200), 'atr': atr(b), 'r14': rsi(b['c'], 14), 'r3': rsi(b['c'], 3),
            'hh50': rollmax(b['h'], 50, 1), 'hh20': rollmax(b['h'], 20, 0), 'va20': sma(b['v'], 20), 'tv20': sma(tv, 20)}

def clampd(dist, c, lo, hi): return min(max(dist, lo * c), hi * c)

def signal(b, ind, i, strat, reg, nret):
    """Returns an idea (dict) if `strat` fires on bar i, else None. Same code feeds live ideas and the backtest."""
    if i < 210: return None
    c = b['c'][i]; s20, s50, s200, a, r14, r3 = (ind[k][i] for k in ('s20', 's50', 's200', 'atr', 'r14', 'r3'))
    if None in (s20, s50, s200, a, r14, r3) or c <= 0: return None
    if reg == 2: return None                                  # weak or volatile market: no new ideas
    trend = c > s50 > s200 and s200 > (ind['s200'][i - 20] or 1e18)
    va = ind['va20'][i] or 0; vr = b['v'][i] / va if va else 0
    if strat == 'brk':
        hh = ind['hh50'][i]
        if reg != 0 or hh is None or not (c > hh and c > s200): return None
        dist = clampd(2 * a, c, .03, .10); entry, stop, target, why = c, c - dist, c + 2.5 * dist, [f'Closed at ₹{c:,.2f}, above its 50-day high of ₹{hh:,.2f}, on {vr:.1f}x its normal volume']
    elif strat == 'pb':
        h20 = ind['hh20'][i]
        if reg != 0 or h20 is None or not (c > s200 and s50 > s200 and c <= 0.95 * h20 and r14 <= 48 and c > b['o'][i] and c > b['c'][i - 1] and c >= 0.97 * s50): return None
        dist = clampd(c - (min(b['l'][i - 2:i + 1]) - 0.3 * a), c, .03, .09); entry, stop, target = c, c - dist, c + 2 * dist
        why = [f'Pulled back {(1 - c / h20) * 100:.0f}% from its 20-day high to near its 50-day average (RSI {r14:.0f})', 'Last session closed higher than it opened and than the day before: a bounce candle']
    elif strat == 'mr':
        if reg != 1 or not (r3 < 15 and c > s200 and c <= 0.96 * s20): return None
        dist = clampd(1.5 * a, c, .03, .07); entry, stop, target = c, c - dist, s20
        if target - c < 1.2 * dist: return None
        why = [f'Oversold: 3-day RSI at {r3:.0f} and {(1 - c / s20) * 100:.1f}% below its 20-day average', f'Target is the 20-day average at ₹{s20:,.2f}']
    else: return None
    rr = (target - entry) / (entry - stop); tv = (ind['tv20'][i] or 0) / 1e7
    rs = (b['c'][i] / b['c'][i - 63] - 1) * 100 - nret if i >= 63 and nret is not None else 0
    fit = 20 if (reg == 0 and strat in ('brk', 'pb')) or (reg == 1 and strat == 'mr') else 5
    sc = fit + (15 if trend else 8 if c > s200 else 0) + max(0, min(rs / 15, 1)) * 15 + min(rr / 3, 1) * 15
    sc += (min(vr / 2.5, 1) * 15 if strat == 'brk' else 10 if (strat == 'pb' and vr <= 1.2) else 5 if strat == 'pb' else 8) + min(tv / 50, 1) * 10 + (10 if 3 <= (entry - stop) / entry * 100 <= 8 else 5)
    return {'entry': round(entry, 2), 'stop': round(stop, 2), 'target': round(target, 2), 'wk': STRAT_WK[strat], 'score': round(sc), 'vr': vr, 'rs': rs, 'trend': trend, 'tv': tv, 'rr': rr, 'why': why, 'rsi3': r3}

# ---------------------------------------------------------------- market regime
def regime_series(nb, ni, vb):
    vix = {d: c for d, c in zip(vb['d'], vb['c'])} if vb else {}; out = {}; last_v = 15.0
    for i, d in enumerate(nb['d']):
        last_v = vix.get(d, last_v); c, s50, s200 = nb['c'][i], ni['s50'][i], ni['s200'][i]
        if s200 is None: out[d] = (1, last_v); continue
        out[d] = (2 if (c < s200 or last_v > 22) else 0 if (c > s50 > s200 and last_v <= 20) else 1, last_v)
    return out

# ---------------------------------------------------------------- tiers, vetoes, ideas
def tier_for(seg, score, reg):
    t = 1 if score >= 72 else 2 if score >= 55 else 3 if score >= 40 else 0
    if not t: return 0
    if seg == 'Small': t = 3
    return t
def veto_reason(meta, b, ind, i, market_last):
    c = b['c'][i]; tv = (ind['tv20'][i] or 0) / 1e7
    if dt.date.fromisoformat(b['d'][i]) < dt.date.fromisoformat(market_last) - dt.timedelta(days=6): return 'Price data is older than 6 days; the stock may be suspended or the download failed.'
    if tv < MIN_TV_CR[meta['seg']]: return f'Average daily traded value is ₹{tv:.1f} crore, below the ₹{MIN_TV_CR[meta["seg"]]:.1f} crore minimum for a {meta["seg"].lower()}cap. Exiting could be slow.'
    if c < 20: return f'Price is ₹{c:.2f}, below the ₹20 minimum.'
    if abs(c / b['c'][i - 1] - 1) > 0.15: return 'Moved more than 15% in one day, which usually means a news event that cannot be checked here yet.'
    return None

def build_ideas(uni, bars, inds, regimes, nb, asof):
    ri, vix = regimes[asof]; nret = (nb['c'][-1] / nb['c'][-64] - 1) * 100; ideas, vetoed, skipped = [], [], 0
    for sym, meta in uni.items():
        b, ind = bars.get(sym), inds.get(sym)
        if not b or len(b['c']) < 215: skipped += 1; continue
        i = len(b['c']) - 1
        for st in ('brk', 'pb', 'mr'):
            s = signal(b, ind, i, st, ri, nret)
            if not s: continue
            vr = veto_reason(meta, b, ind, i, asof)
            if vr: vetoed.append({'t': sym, 'n': meta['name'], 'why': vr}); continue
            tier = tier_for(meta['seg'], s['score'], ri)
            if not tier: continue
            good, bad = [], []
            (good if s['trend'] else bad).append('Trend filter (above rising 50- and 200-day averages)')
            if st == 'brk': (good if s['vr'] >= 1.5 else bad).append('Volume at least 1.5x normal')
            (good if s['rs'] > 0 else bad).append('Beating the Nifty over 3 months')
            (good if s['tv'] >= 2 * MIN_TV_CR[meta['seg']] else bad).append('Comfortably liquid')
            (good if s['rr'] >= 2 else bad).append('Reward at least 2x the risk')
            if tier == 1 and (not s['trend'] or len(bad) > 1): tier = 2
            why = s['why'] + [f"{s['rs']:+.1f}% against the Nifty over 3 months; average daily traded value ₹{s['tv']:.1f} crore"]
            ideas.append({'t': sym, 'n': meta['name'], 'sec': meta['sec'] or '—', 'seg': meta['seg'], 'st': st, 'tier': tier, 'entry': s['entry'], 'stop': s['stop'], 'target': s['target'], 'wk': s['wk'],
                          'score': s['score'], 'vol': f"{s['vr']:.1f}x", 'rs': f"{s['rs']:+.1f}%".replace('-', '−'), 'why': why, 'chk': good, 'bad': bad})
    ideas.sort(key=lambda x: (x['tier'], -x['score']))
    return ideas[:24], vetoed[:30], skipped

# ---------------------------------------------------------------- paper tracking (the bot follows its own Tier 1/2 ideas)
def costs_for(qty, price): return {'brok': qty * price * .0006, 'stt': qty * price * .0021, 'other': qty * price * .0004, 'slip': qty * price * .0012}
def run_paper(ideas, bars, nb, regimes, asof):
    P = jload(os.path.join(DATA, 'paper.json'), {'open': [], 'pending': [], 'closed': [], 'seen': []})
    last_n = nb['d'][-1]
    def bar_after(sym, d):
        b = bars.get(sym); 
        if not b: return None
        for i, x in enumerate(b['d']):
            if x > d: return i
        return None
    # 1) fill pending orders at the next session's open
    pend = []
    for p in P['pending']:
        i = bar_after(p['t'], p['sd']); b = bars.get(p['t'])
        if i is None: pend.append(p); continue
        o = b['o'][i]
        if o <= p['stop'] or o >= p['target'] or o > p['entry0'] * 1.03: continue          # gapped away from the plan: skipped
        risk_pct = RISK[p['tier']] if p['seg'] != 'Small' else min(RISK[p['tier']], SMALL_CAP_RISK)
        qty = min(int(CAPITAL * risk_pct / 100 / (o - p['stop'])), int(CAPITAL * .2 / o))
        if qty < 1: continue
        P['open'].append({'t': p['t'], 'n': p['n'], 'st': p['st'], 'tier': p['tier'], 'seg': p['seg'], 'entry': o, 'qty': qty, 'stop': p['stop'], 'target': p['target'], 'wk': p['wk'], 'ed': b['d'][i], 'regime': p['regime'],
                         'riskAmt': qty * (o - p['stop']), 'mae': 0.0, 'mfe': 0.0, 'last': o, 'ei': i})
    P['pending'] = pend
    # 2) walk open positions forward
    still = []
    for o in P['open']:
        b = bars.get(o['t']); 
        if not b: still.append(o); continue
        ei = b['d'].index(o['ed']) if o['ed'] in b['d'] else None
        if ei is None: still.append(o); continue
        done = None; per = o['entry'] - o['stop']
        for j in range(ei, len(b['d'])):
            hi, lo, op, cl = b['h'][j], b['l'][j], b['o'][j], b['c'][j]
            o['mae'] = min(o['mae'], (lo - o['entry']) / per); o['mfe'] = max(o['mfe'], (hi - o['entry']) / per)
            if j > ei and op <= o['stop']: done = (b['d'][j], op, 'Stop hit (gapped)'); break
            if lo <= o['stop']: done = (b['d'][j], o['stop'], 'Stop hit'); break
            if hi >= o['target']: done = (b['d'][j], o['target'], 'Target reached'); break
            if j - ei >= o['wk'] * 5: done = (b['d'][j], cl, 'Time-stop'); break
            o['last'] = cl
        if done:
            xd, xp, why = done; gross = o['qty'] * (xp - o['entry']); c = costs_for(o['qty'], o['entry']); cost = sum(c.values())
            ni = nb['d'].index(o['ed']) if o['ed'] in nb['d'] else None; xi = nb['d'].index(xd) if xd in nb['d'] else None
            bench = (nb['c'][xi] / nb['c'][ni] - 1) * 100 if ni is not None and xi is not None else 0
            P['closed'].append({'id': f"{o['t']}-{o['ed']}", 'date': o['ed'], 'exit': xd, 't': o['t'], 'n': o['n'], 'st': o['st'], 'tier': o['tier'], 'regime': o['regime'], 'seg': o['seg'], 'entry': o['entry'], 'qty': o['qty'],
                                'R': (xp - o['entry']) / per, 'gross': gross, 'costs': cost, **{k: v for k, v in c.items()}, 'net': gross - cost, 'days': max(1, (dt.date.fromisoformat(xd) - dt.date.fromisoformat(o['ed'])).days), 'riskAmt': o['riskAmt'],
                                'exitWhy': why, 'mae': o['mae'], 'mfe': o['mfe'], 'bench': bench})
        else: still.append(o)
    P['open'] = still
    # 3) queue today's new Tier 1 and 2 ideas
    held = {x['t'] for x in P['open']} | {x['t'] for x in P['pending']}
    for d in ideas:
        key = f"{d['t']}|{asof}|{d['st']}"
        if d['tier'] <= 2 and d['t'] not in held and key not in P['seen']:
            P['pending'].append({'t': d['t'], 'n': d['n'], 'st': d['st'], 'tier': d['tier'], 'seg': d['seg'], 'stop': d['stop'], 'target': d['target'], 'entry0': d['entry'], 'wk': d['wk'], 'sd': asof, 'regime': regimes[asof][0]})
            P['seen'].append(key); held.add(d['t'])
    P['seen'] = P['seen'][-3000:]; jsave(os.path.join(DATA, 'paper.json'), P)
    return P

# ---------------------------------------------------------------- real backtest
def backtest(p):
    uni, bars, inds, regs, nb = CACHE['meta'], CACHE['bars'], CACHE['ind'], CACHE['regimes'], CACHE['nifty']
    final = bool(p.get('final')); d0 = '2025-01-01' if final else f"{int(p['from'])}-01-01"; d1 = '2100-01-01' if final else f"{min(int(p['to']), 2024)}-12-31"
    strat, maxopen = p['strat'], 9; cost = float(p.get('cost', .35)); slip = float(p.get('slip', .12)); risk = float(p.get('risk', 1)); segs = {'all': ('Large', 'Mid', 'Small'), 'n200': ('Large',), 'mid': ('Mid',), 'small': ('Small',)}[p.get('uni', 'all')]
    nidx = {d: i for i, d in enumerate(nb['d'])}; cands = {}
    for sym, meta in uni.items():
        if meta['seg'] not in segs: continue
        b, ind = bars.get(sym), inds.get(sym)
        if not b or len(b['c']) < 215: continue
        for i in range(210, len(b['c']) - 2):
            d = b['d'][i]
            if d < d0 or d > d1 or d not in regs: continue
            ni = nidx.get(d)
            if ni is None or ni < 64: continue
            ri = regs[d][0]
            if (strat == 'mr') != (ri == 1): continue
            s = signal(b, ind, i, strat, ri, (nb['c'][ni] / nb['c'][ni - 63] - 1) * 100)
            if not s or (ind['tv20'][i] or 0) / 1e7 < MIN_TV_CR[meta['seg']] or b['c'][i] < 20: continue
            e = i + 1; o = b['o'][e]
            if o <= s['stop'] or o >= s['target'] or o > s['entry'] * 1.03: continue
            ex = None
            for j in range(e, len(b['c'])):
                if j > e and b['o'][j] <= s['stop']: ex = (j, b['o'][j]); break
                if b['l'][j] <= s['stop']: ex = (j, s['stop']); break
                if b['h'][j] >= s['target']: ex = (j, s['target']); break
                if j - e >= s['wk'] * 5: ex = (j, b['c'][j]); break
            if not ex: continue
            if b['d'][ex[0]] > d1 and not final: continue
            R = (ex[1] - o) / (o - s['stop']); sp = (o - s['stop']) / o * 100
            cands.setdefault(b['d'][e], []).append((s['score'], b['d'][ex[0]], R - (cost + slip) / sp, sym))
    trades, openx = [], []
    for d in sorted(cands):
        openx = [x for x in openx if x > d]
        for sc, xd, nr, sym in sorted(cands[d], reverse=True):
            if len(openx) >= maxopen: break
            openx.append(xd); trades.append((xd, nr, d, sym))
    trades.sort()
    if not trades: return {'n': 0}
    eq = 100.0; eqs, dates, yrs, w, sw, sl, cl, pk, dd = [100.0], [trades[0][2]], {}, 0, 0.0, 0.0, 0, 100.0, 0.0
    for xd, nr, ed, sym in trades:
        st0 = eq; eq *= 1 + risk / 100 * nr; eqs.append(eq); dates.append(xd); pk = max(pk, eq); dd = max(dd, (pk - eq) / pk)
        Y = yrs.setdefault(int(xd[:4]), {'n': 0, 'w': 0, 'R': 0.0, 's': st0, 'e': eq}); Y['n'] += 1; Y['R'] += nr; Y['e'] = eq
        if nr > 0: Y['w'] += 1; w += 1; sw += nr
        else: sl += -nr; cl += 1
    def nclose(d):
        k = max((x for x in nb['d'] if x <= d), default=nb['d'][0]); return nb['c'][nidx[k]]
    base = nclose(dates[0]); nfs = [nclose(d) / base * 100 for d in dates]; n = len(trades)
    years = [{'y': y, 'n': v['n'], 'win': v['w'] / v['n'] * 100, 'exp': v['R'] / v['n'], 'ret': (v['e'] / v['s'] - 1) * 100} for y, v in sorted(yrs.items())]
    return {'n': n, 'win': w / n * 100, 'avgW': sw / w if w else 0, 'avgL': -sl / cl if cl else 0, 'exp': sum(t[1] for t in trades) / n, 'pf': sw / sl if sl else 99, 'dd': dd * 100, 'ret': (eq / 100 - 1) * 100,
            'nifty': (nfs[-1] / 100 - 1) * 100, 'years': years, 'eqs': eqs, 'nfs': nfs, 'dates': dates, 'posYears': sum(1 for y in years if y['ret'] > 0) / len(years),
            'note': 'Uses today\'s index members, so stocks that dropped out or were delisted are missing (survivorship bias). Results are flattering. Daily prices only, no intraday detail.'}



# ---------------------------------------------------------------- NSE's own daily files (used when Yahoo blocks the server)
def nse_dir(): d = os.path.join(DATA, 'nse'); os.makedirs(d, exist_ok=True); return d
def bhav_url(d): return 'https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_%s.csv' % d.strftime('%d%m%Y')
def idx_url(d): return 'https://archives.nseindia.com/content/indices/ind_close_all_%s.csv' % d.strftime('%d%m%Y')
def _num(x):
    try: return float(str(x).replace(',', '').strip())
    except Exception: return None
def fetch_day(d, wanted):
    """Returns None for a market holiday, else {'s': {symbol: [o,h,l,c,prevclose,vol]}, 'n': [o,h,l,c] or None, 'v': vix or None}."""
    try: raw = http_get(bhav_url(d), tries=2, timeout=40).decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        if e.code in (403, 404): return None
        raise
    rows = csv.reader(io.StringIO(raw)); hdr = [h.strip().upper() for h in next(rows)]; ix = {h: i for i, h in enumerate(hdr)}
    need = ('SYMBOL', 'SERIES', 'PREV_CLOSE', 'OPEN_PRICE', 'HIGH_PRICE', 'LOW_PRICE', 'CLOSE_PRICE', 'TTL_TRD_QNTY')
    if any(k not in ix for k in need): raise RuntimeError('The NSE price file has a new layout: ' + ','.join(hdr)[:120])
    out = {}
    for r in rows:
        if len(r) < len(hdr): continue
        sym = r[ix['SYMBOL']].strip()
        if sym not in wanted or r[ix['SERIES']].strip() != 'EQ': continue
        v = [_num(r[ix[k]]) for k in ('OPEN_PRICE', 'HIGH_PRICE', 'LOW_PRICE', 'CLOSE_PRICE', 'PREV_CLOSE', 'TTL_TRD_QNTY')]
        if None in v or v[3] <= 0: continue
        out[sym] = [round(v[0], 2), round(v[1], 2), round(v[2], 2), round(v[3], 2), round(v[4], 2), int(v[5])]
    n = vix = None
    try:
        r2 = list(csv.reader(io.StringIO(http_get(idx_url(d), tries=2, timeout=40).decode('utf-8', 'replace')))); h2 = [h.strip().upper() for h in r2[0]]
        ni = h2.index('INDEX NAME'); oi, hi, li, ci = (next(i for i, h in enumerate(h2) if h.startswith(p)) for p in ('OPEN INDEX', 'HIGH INDEX', 'LOW INDEX', 'CLOSING INDEX'))
        for r in r2[1:]:
            if len(r) <= ci: continue
            nm = r[ni].strip().upper()
            if nm == 'NIFTY 50': q = [_num(r[k]) for k in (oi, hi, li, ci)]; n = q if None not in q else None
            elif nm == 'INDIA VIX': vix = _num(r[ci])
    except Exception as e: log('Index file problem for', d, str(e)[:80])
    return {'s': out, 'n': n, 'v': vix}

def nse_load(uni, deadline_min=None):
    wanted = set(uni); folder = nse_dir(); hol_path = os.path.join(folder, '_holidays.json'); hol = set(jload(hol_path, [])); today = now().date(); t_end = time.time() + deadline_min * 60 if deadline_min else None
    months = {}
    def month_key(d): return d.strftime('%Y-%m')
    def load_month(k):
        if k not in months: months[k] = jload(os.path.join(folder, k + '.json'), {})
        return months[k]
    # day list, newest first
    days = []; d = today
    while d >= NSE_START:
        if d.weekday() < 5: days.append(d)
        d -= dt.timedelta(days=1)
    todo = [d for d in days if d.isoformat() not in load_month(month_key(d)) and d.isoformat() not in hol]
    STATUS.update(total=len(todo), done=0, msg='Downloading NSE daily files…'); got = 0; errors = []; consec = 0
    def work(d):
        if t_end and time.time() > t_end: return d, 'late'
        try: return d, fetch_day(d, wanted)
        except Exception as e: return d, ('err', str(e)[:120])
    CH = 40
    for i in range(0, len(todo), CH):
        chunk = todo[i:i + CH]
        with ThreadPoolExecutor(max_workers=4) as ex: res = list(ex.map(work, chunk))
        for d, r in res:
            STATUS['done'] += 1; k = month_key(d)
            if r == 'late': continue
            if isinstance(r, tuple): errors.append((d, r[1])); consec += 1; continue
            consec = 0
            if r is None:
                if (today - d).days > 3: hol.add(d.isoformat())
                continue
            load_month(k)[d.isoformat()] = r; got += 1
        for k in {month_key(d) for d in chunk}: jsave(os.path.join(folder, k + '.json'), months[k])
        jsave(hol_path, sorted(hol))
        if consec >= 12: log('NSE keeps failing:', errors[-1]); break
        if t_end and time.time() > t_end: log('Time limit reached; history will deepen on the next run'); break
    # assemble
    alld = {}
    for f in sorted(os.listdir(folder)):
        if f.endswith('.json') and not f.startswith('_'): alld.update(jload(os.path.join(folder, f), {}))
    dates = sorted(alld)
    if len(dates) < 120:
        why = errors[0][1] if errors else 'no files could be read'
        raise RuntimeError(f'Only {len(dates)} trading days of NSE history are available so far ({why}). NSE may be blocking this server. Run again later.')
    nb = {'d': [], 'o': [], 'h': [], 'l': [], 'c': [], 'v': []}; vb = {'d': [], 'o': [], 'h': [], 'l': [], 'c': [], 'v': []}
    per = {}
    for dd in dates:
        rec = alld[dd]
        if rec.get('n'):
            for k, v in zip('ohlc', rec['n']): nb[k].append(v)
            nb['d'].append(dd); nb['v'].append(0)
        if rec.get('v'):
            vb['d'].append(dd)
            for k in 'ohlc': vb[k].append(rec['v'])
            vb['v'].append(0)
        for sym, v in rec['s'].items(): per.setdefault(sym, []).append((dd, v))
    if len(nb['d']) < 120: raise RuntimeError('The NSE index files for the Nifty could not be read (only %d days).' % len(nb['d']))
    bars = {}; failed = []; adj = 0
    for sym in uni:
        rows = per.get(sym)
        if not rows or len(rows) < 30: failed.append((sym, 'no NSE price history')); continue
        # splits and bonuses: NSE's "previous close" is already adjusted, so a gap against yesterday's close marks one; scale the older prices
        fac = 1.0; n = len(rows); o = [0.0] * n; h = [0.0] * n; l = [0.0] * n; c = [0.0] * n; v = [0] * n
        for i in range(n - 1, -1, -1):
            if i < n - 1:
                pc = rows[i + 1][1][4]; cl = rows[i][1][3]
                if cl > 0 and pc > 0 and abs(pc / cl - 1) > 0.04: fac *= pc / cl; adj += 1
            r = rows[i][1]; o[i], h[i], l[i], c[i] = (round(x * fac, 3) for x in r[:4]); v[i] = int(r[5] / fac) if fac > 0 else r[5]
        bars[sym] = {'d': [x[0] for x in rows], 'o': o, 'h': h, 'l': l, 'c': c, 'v': v}
    info = f"{len(dates)} trading days from {dates[0]} to {dates[-1]}; {adj} split/bonus adjustments; {len(bars)} of {len(uni)} stocks have history"
    if not bars: raise RuntimeError('No stock history could be built from the NSE files.')
    return bars, nb, (vb if len(vb['d']) > 60 else None), failed, info

def collect_trades(strat):
    """Every trade the rules would have taken, over the whole history, before costs or position limits.
    The phone version downloads this list and does the cost / risk / date maths in the browser."""
    uni, bars, inds, regs, nb = CACHE['meta'], CACHE['bars'], CACHE['ind'], CACHE['regimes'], CACHE['nifty']
    nidx = {d: i for i, d in enumerate(nb['d'])}; out = []; segi = {'Large': 0, 'Mid': 1, 'Small': 2}
    for sym, meta in uni.items():
        b, ind = bars.get(sym), inds.get(sym)
        if not b or len(b['c']) < 215: continue
        for i in range(210, len(b['c']) - 2):
            d = b['d'][i]; ni = nidx.get(d)
            if d not in regs or ni is None or ni < 64: continue
            ri = regs[d][0]
            if (strat == 'mr') != (ri == 1): continue
            s = signal(b, ind, i, strat, ri, (nb['c'][ni] / nb['c'][ni - 63] - 1) * 100)
            if not s or (ind['tv20'][i] or 0) / 1e7 < MIN_TV_CR[meta['seg']] or b['c'][i] < 20: continue
            e = i + 1; o = b['o'][e]
            if o <= s['stop'] or o >= s['target'] or o > s['entry'] * 1.03: continue
            ex = None
            for j in range(e, len(b['c'])):
                if j > e and b['o'][j] <= s['stop']: ex = (j, b['o'][j]); break
                if b['l'][j] <= s['stop']: ex = (j, s['stop']); break
                if b['h'][j] >= s['target']: ex = (j, s['target']); break
                if j - e >= s['wk'] * 5: ex = (j, b['c'][j]); break
            if not ex: continue
            out.append([d, b['d'][e], b['d'][ex[0]], s['score'], round((ex[1] - o) / (o - s['stop']), 4), round((o - s['stop']) / o * 100, 3), segi[meta['seg']]])
    out.sort(key=lambda t: (t[1], -t[3]))
    return out

# ---------------------------------------------------------------- the daily job
def refresh():
    with LOCK:
        if STATUS['running']: return
        STATUS.update(running=True, msg='Reading the NSE index lists…', done=0, total=0)
    try:
        t0 = time.time(); inbox, failed = [], []
        uni, uerr, cached = load_universe()
        if not uni: raise RuntimeError('Could not get the stock lists from NSE, and none are saved from before. ' + '; '.join(uerr))
        syms = list(uni.keys()); STATUS.update(total=len(syms) + 2, msg='Downloading prices…', abort=False)
        deadline = time.time() + BUDGET_MIN * 60 if BUDGET_MIN else None
        def yahoo_load():
            # The Nifty index comes first. Without it nothing else can be worked out, so fail early and clearly.
            nb = None; last_err = None
            for attempt in range(3):
                try: nb = get_bars('NIFTY')[0]; break
                except Exception as e:
                    last_err = e; log('Nifty download failed:', e)
                    if attempt < 2: time.sleep(30)
            if nb is None: raise RuntimeError('Yahoo Finance refused the request for the Nifty index (' + str(last_err)[:100] + '). If this runs on GitHub, Yahoo is probably blocking GitHub\'s servers.')
            try: vb = get_bars('VIX')[0]
            except Exception: vb = None
            run = {'consec': 0}
            def one(s):
                if STATUS.get('abort'): return s, None, 'skipped: Yahoo is refusing requests'
                if deadline and time.time() > deadline: return s, None, 'skipped: time limit reached, will continue next run'
                try: b, mode = get_bars(s); return s, b, None
                except Exception as e: return s, None, str(e)[:120]
            bars = {}
            with ThreadPoolExecutor(max_workers=2 if not MOCK else 8) as ex:
                for s, b, err in ex.map(one, syms):
                    STATUS['done'] += 1
                    if b and len(b['d']) > 0: bars[s] = b; run['consec'] = 0
                    else:
                        failed.append((s, err))
                        if err and '429' in err:
                            run['consec'] += 1
                            if run['consec'] >= 12: STATUS['abort'] = True
                    time.sleep(0.3 if not MOCK else 0)
            if len(bars) < 20: raise RuntimeError(f'Only {len(bars)} stocks could be downloaded. Yahoo Finance is refusing most requests (' + (failed[0][1] if failed else 'unknown') + ').')
            return bars, nb, vb, failed

        info = ''
        if USE_NSE:
            bars, nb, vb, failed, info = nse_load(uni, deadline_min=BUDGET_MIN)
        else:
            bars, nb, vb, failed = yahoo_load()
        STATUS.update(msg='Working out ideas…')
        asof = nb['d'][-1]; ni = prep(nb); inds = {s: prep(b) for s, b in bars.items()}
        regs = regime_series(nb, ni, vb); ri, vix = regs[asof]
        ideas, vetoed, skipped = build_ideas(uni, bars, inds, regs, nb, asof)
        CACHE.update(meta=uni, bars=bars, ind=inds, regimes=regs, nifty=nb)
        P = run_paper(ideas, bars, nb, regs, asof)
        STATUS.update(msg='Testing the strategies on history…')
        tested = {}
        for k in ('brk', 'pb', 'mr'):
            r = backtest({'strat': k, 'from': 2017, 'to': 2024, 'uni': 'all'}); tested[k] = round(r.get('exp', 0), 3)
        # source checks
        nse_chk = cross_check(bars, asof) if not USE_NSE else {'ok': True, 'n': len(bars), 'text': info, 'mismatch': [], 'date': asof}; tz = now().strftime('%d %b, %H:%M')
        H = {'nse_eod': ['good' if nse_chk['ok'] else 'warn', 'Healthy' if nse_chk['ok'] else 'Unavailable' if nse_chk['n'] == 0 else 'Check', tz, nse_chk['text']] if not MOCK else ['off', 'Mock', tz, 'Mock mode'],
             'yahoo': (['off', 'Not used', '—', 'Yahoo blocks GitHub servers, so NSE daily files are used'] if USE_NSE else ['good' if len(failed) < 0.05 * len(syms) else 'warn', 'Healthy' if len(failed) < 0.05 * len(syms) else 'Partial', tz, f'{len(bars)} of {len(syms)} stocks downloaded' + (f', {len(failed)} failed' if failed else '')]),
             'nse_ann': ['off', 'Not built yet', '—', 'Results dates and corporate announcements are not read yet'], 'rss': ['off', 'Not built yet', '—', 'News headlines are not read yet'],
             'gdelt': ['off', 'Not built yet', '—', 'Planned'], 'rbi': ['off', 'Not built yet', '—', 'Planned'], 'screener': ['off', 'Off', 'never', 'Terms not yet confirmed']}
        age = (now().date() - dt.date.fromisoformat(asof)).days
        if age > 4: inbox.append(('crit', 'Prices look old', f'The newest Nifty price is from {asof}, {age} days ago.', 'Showed the ideas, but they are based on old prices.', 'Check your internet connection and press Refresh data.'))
        if failed: inbox.append(('warn', f'{len(failed)} stocks could not be downloaded', 'Examples: ' + ', '.join(s for s, _ in failed[:8]) + '. First error: ' + (failed[0][1] or 'unknown'), 'Left them out of today\'s ideas.', 'Usually temporary. Press Refresh data later. If it repeats, Yahoo may be limiting requests.'))
        if cached or uerr: inbox.append(('warn', 'NSE stock lists', 'Some index lists could not be read from NSE: ' + '; '.join(uerr[:3]), 'Used the lists saved last time.' if cached else 'Used what could be read.', 'Check the Data health tab.'))
        for m in nse_chk['mismatch'][:5]: inbox.append(('warn', f"Two price sources disagree for {m[0]}", f'Yahoo shows ₹{m[1]:.2f}, the NSE file shows ₹{m[2]:.2f} for {nse_chk["date"]}. A split or bonus may not be applied yet.', 'Kept it in the list but treat its numbers with care.', 'Check the price in your broker app before acting.'))
        inbox.append(('crit', 'Not checked: results dates, announcements, news', 'This version does not yet read NSE or BSE announcements or news. A stock can show a clean setup while its quarterly results are due in a few days.', 'Nothing. These checks are not built yet.', 'Before acting on any idea, look up its results date and latest news yourself.'))
        inbox.append(('info', 'Backtests are flattering', 'Backtests use today\'s index members, so companies that fell out of the index or were delisted are missing. Real results are likely lower.', '—', 'Treat the Backtest Lab as a screening tool, not a promise.'))
        if P['pending']: inbox.append(('info', f"{len(P['pending'])} bot trades waiting to start", 'The bot follows its own Tier 1 and 2 ideas on paper. They start at the next session\'s opening price.', '—', 'Nothing.'))
        nd = nb['d']; last160 = nd[-160:]
        eqb, eqn = [], []
        for d in last160:
            real = sum(t['net'] for t in P['closed'] if t['exit'] <= d); un = 0.0
            for o in P['open']:
                b = bars.get(o['t'])
                if b and o['ed'] <= d:
                    k = max((x for x in range(len(b['d'])) if b['d'][x] <= d), default=None)
                    if k is not None: un += o['qty'] * (b['c'][k] - o['entry'])
            eqb.append(CAPITAL + real + un); eqn.append(CAPITAL * nb['c'][nd.index(d)] / nb['c'][nd.index(last160[0])])
        openout = []
        for o in P['open']:
            ed = dt.date.fromisoformat(o['ed']); b = bars[o['t']]; held = len(b['d']) - 1 - (b['d'].index(o['ed']) if o['ed'] in b['d'] else 0)
            openout.append({**{k: o[k] for k in ('t', 'n', 'st', 'tier', 'entry', 'qty', 'stop', 'target', 'last', 'wk')}, 'days': held * 7 // 5, 'd0': -(dt.date.fromisoformat(asof) - ed).days})
        label = ['Trending up', 'Sideways', 'Weak or volatile'][ri]
        detail = f"Nifty {nb['c'][-1]:,.0f} · 50-day {ni['s50'][-1]:,.0f} · 200-day {ni['s200'][-1]:,.0f}"
        state = {'empty': False, 'mock': MOCK, 'asof': asof, 'generated': now().isoformat(timespec='seconds'), 'regime': {'idx': ri, 'label': label, 'vix': round(vix, 1), 'detail': detail},
                 'ideas': ideas, 'vetoed': vetoed, 'health': H, 'inbox': [{'id': 'r%d' % i, 'sev': a, 'title': b_, 'body': c_, 'did': d_, 'need': e_, 'when': tz} for i, (a, b_, c_, d_, e_) in enumerate(inbox)],
                 'open': openout, 'journal': P['closed'][-300:][::-1], 'pending': len(P['pending']), 'eq': {'dates': last160, 'bot': eqb, 'nifty': eqn}, 'tested': tested,
                 'universe': {'n': len(syms), 'got': len(bars), 'skipped_short_history': skipped, 'failed': [s for s, _ in failed][:40]}, 'seconds': round(time.time() - t0)}
        jsave(os.path.join(DATA, 'state.json'), state); CACHE['state'] = state
        STATUS.update(msg=f'Done. Prices up to {asof}.', done=STATUS['total'])
    except Exception as e:
        import traceback; traceback.print_exc(); STATUS.update(msg='Failed: ' + str(e)[:200])
    finally: STATUS['running'] = False

def cross_check(bars, asof):
    if MOCK: return {'ok': True, 'n': 0, 'text': '', 'mismatch': [], 'date': asof}
    d = dt.date.fromisoformat(asof)
    for back in range(0, 7):
        day = d - dt.timedelta(days=back)
        try:
            raw = http_get('https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_%s.csv' % day.strftime('%d%m%Y'), tries=1, timeout=30).decode('utf-8', 'replace'); break
        except Exception: raw = None
    if not raw: return {'ok': False, 'n': 0, 'text': 'Could not read the NSE daily price file (the site may be blocking this request)', 'mismatch': [], 'date': asof}
    rows = csv.DictReader(io.StringIO(raw.replace(', ', ',').replace(' ,', ','))); n = ok = 0; mism = []; iso = day.isoformat()
    for r in rows:
        r = {(k or '').strip(): (v or '').strip() for k, v in r.items()}
        if r.get('SERIES') != 'EQ' or r.get('SYMBOL') not in bars: continue
        b = bars[r['SYMBOL']]
        if iso not in b['d']: continue
        try: nse = float(r['CLOSE_PRICE'])
        except Exception: continue
        y = b['c'][b['d'].index(iso)]; n += 1
        if abs(y / nse - 1) <= .01: ok += 1
        else: mism.append((r['SYMBOL'], y, nse))
    return {'ok': n > 0 and ok / n > .97, 'n': n, 'text': f'NSE file for {iso}: Yahoo matches on {ok} of {n} stocks', 'mismatch': mism, 'date': iso}

# ---------------------------------------------------------------- Claude (Haiku), off unless a key is set
def usage_path(): return os.path.join(DATA, 'usage.json')
def ask_claude(body):
    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key: return 400, {'error': 'no_key', 'text': 'No Anthropic key found on this computer. Set ANTHROPIC_API_KEY, then restart.'}
    u = jload(usage_path(), {}); mk = now().strftime('%Y-%m'); m = u.get(mk, {'in': 0, 'out': 0, 'calls': 0})
    spent = (m['in'] * PRICE_IN + m['out'] * PRICE_OUT) / 1e6 * USD_INR
    if body.get('cap') is not None and spent >= float(body['cap']): return 429, {'error': 'cap', 'text': f'The monthly Claude spending cap of ₹{body["cap"]} is reached (about ₹{spent:.0f} used). Raise it in Preferences.'}
    msgs = body.get('messages') or [{'role': 'user', 'content': str(body.get('input', ''))}]
    req = urllib.request.Request('https://api.anthropic.com/v1/messages', data=json.dumps({'model': HAIKU_MODEL, 'max_tokens': 500, 'messages': msgs}).encode(),
                                 headers={'content-type': 'application/json', 'x-api-key': key, 'anthropic-version': '2023-06-01'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r: j = json.load(r)
    except urllib.error.HTTPError as e: return 502, {'error': 'api', 'text': 'Claude returned an error: ' + e.read().decode('utf-8', 'replace')[:200]}
    except Exception as e: return 502, {'error': 'api', 'text': 'Could not reach Claude: ' + str(e)[:150]}
    us = j.get('usage', {}); m['in'] += us.get('input_tokens', 0); m['out'] += us.get('output_tokens', 0); m['calls'] += 1; u[mk] = m; jsave(usage_path(), u)
    return 200, {'text': ''.join(x.get('text', '') for x in j.get('content', [])), 'spent': round((m['in'] * PRICE_IN + m['out'] * PRICE_OUT) / 1e6 * USD_INR, 2), 'calls': m['calls']}

# ---------------------------------------------------------------- web server
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, body, ctype='application/json'):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(b))); self.send_header('Cache-Control', 'no-store'); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p = self.path.split('?')[0]
        if p in ('/', '/index.html'):
            with open(os.path.join(BASE, 'index.html'), 'rb') as f: return self.send(200, f.read(), 'text/html; charset=utf-8')
        if p == '/api/state': return self.send(200, CACHE['state'] or {'empty': True})
        if p == '/api/status':
            u = jload(usage_path(), {}).get(now().strftime('%Y-%m'), {'in': 0, 'out': 0, 'calls': 0})
            return self.send(200, dict(STATUS, key=bool(os.environ.get('ANTHROPIC_API_KEY')), spent=round((u['in'] * PRICE_IN + u['out'] * PRICE_OUT) / 1e6 * USD_INR, 2)))
        self.send(404, {'error': 'not found'})
    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0); body = json.loads(self.rfile.read(n) or b'{}'); p = self.path
        if p == '/api/refresh':
            if not STATUS['running']: threading.Thread(target=refresh, daemon=True).start()
            return self.send(200, {'started': True})
        if p == '/api/backtest':
            if not CACHE.get('bars'): return self.send(409, {'error': 'Download data first (Data health, Refresh data).'})
            try: return self.send(200, backtest(body))
            except Exception as e: return self.send(500, {'error': str(e)[:200]})
        if p == '/api/ask':
            c, r = ask_claude(body); return self.send(c, r)
        self.send(404, {'error': 'not found'})

def load_cache_from_disk():
    st = jload(os.path.join(DATA, 'state.json')); CACHE['state'] = st
    uni = jload(os.path.join(DATA, 'universe.json'))
    if st and uni and not MOCK:
        try:
            bars = {s: jload(os.path.join(PRICES, s.replace('&', '_and_') + '.json')) for s in uni}; bars = {s: b for s, b in bars.items() if b and b['d']}
            nb = jload(os.path.join(PRICES, 'NIFTY.json')); vb = jload(os.path.join(PRICES, 'VIX.json'))
            ni = prep(nb); CACHE.update(meta=uni, bars=bars, ind={s: prep(b) for s, b in bars.items()}, regimes=regime_series(nb, ni, vb), nifty=nb)
        except Exception as e: log('Could not rebuild from saved prices:', e); CACHE['bars'] = {}
    return st

def main():
    st = load_cache_from_disk()
    stale = (not st) or MOCK or not CACHE.get('bars') or (now() - dt.datetime.fromisoformat(st['generated'])).total_seconds() > 16 * 3600
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
    log(f'Swing Desk is running. Open  http://127.0.0.1:{PORT}  in your browser. Press Ctrl+C to stop.')
    if MOCK: log('MOCK MODE: prices are made up. For testing only.')
    if stale: threading.Thread(target=refresh, daemon=True).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: log('Stopped.')

if __name__ == '__main__': main()
