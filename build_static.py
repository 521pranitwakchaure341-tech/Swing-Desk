#!/usr/bin/env python3
"""Phone version builder. Runs once a day on GitHub (free), then publishes a small website that
any phone can open. Reads real prices, writes docs/state.json and docs/trades_*.json."""
import json, os, sys
import server as s
ROOT = os.path.dirname(os.path.abspath(__file__))
s.DATA = os.path.join(ROOT, 'data'); s.PRICES = os.path.join(s.DATA, 'prices'); os.makedirs(s.PRICES, exist_ok=True)
DOCS = ROOT
s.BUDGET_MIN = 75
def probe(name, url):
    try: s.http_get(url, tries=1, timeout=20); print('PROBE ok     ', name)
    except Exception as e: print('PROBE blocked', name, '->', str(e)[:80])
print('--- Which sources can this server reach? ---')
probe('Yahoo (query1)', 'https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=5d&interval=1d')
probe('Yahoo (query2)', 'https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=5d&interval=1d')
probe('NSE index list', s.UNI_URLS['Large'][0])
probe('niftyindices list', s.UNI_URLS['Large'][1])
import datetime as _dt
d = _dt.date.today() - _dt.timedelta(days=3); tried = 0
while tried < 5:
    if d.weekday() < 5:
        tried += 1
        try: s.http_get(s.bhav_url(d), tries=1, timeout=30); print('PROBE ok      NSE daily price file', d); break
        except Exception as e: print('PROBE no      NSE daily price file', d, '->', str(e)[:60])
    d -= _dt.timedelta(days=1)
try: s.http_get(s.idx_url(d), tries=1, timeout=30); print('PROBE ok      NSE daily index file', d)
except Exception as e: print('PROBE no      NSE daily index file', d, '->', str(e)[:60])
probe('Google News headlines', 'https://news.google.com/rss/search?q=reliance+shares&hl=en-IN&gl=IN&ceid=IN:en')
try:
    _ev, _note = s.fetch_events(_dt.date.today()); print('PROBE', 'ok     ' if _ev else 'blocked', 'NSE results calendar ->', _note)
except Exception as e: print('PROBE blocked NSE results calendar ->', str(e)[:80])
print('---')
s.USE_NSE = True            # Yahoo blocks GitHub's servers, so use NSE's own daily files
s.refresh()
st = s.CACHE.get('state')
if not st or s.STATUS['msg'].startswith('Failed'):
    print('Refresh failed:', s.STATUS['msg']); sys.exit(1)
nb = s.CACHE['nifty']; st['nifty'] = {'d': nb['d'], 'c': nb['c']}
st['static'] = True
json.dump(st, open(os.path.join(DOCS, 'state.json'), 'w'), separators=(',', ':'))
for k in ('brk', 'pb', 'mr'):
    t = s.collect_trades(k); json.dump(t, open(os.path.join(DOCS, f'trades_{k}.json'), 'w'), separators=(',', ':')); print(k, len(t), 'trades')
print('Done. Prices up to', st['asof'])
