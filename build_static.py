#!/usr/bin/env python3
"""Phone version builder. Runs once a day on GitHub (free), then publishes a small website that
any phone can open. Reads real prices, writes docs/state.json and docs/trades_*.json."""
import json, os, sys
import server as s
ROOT = os.path.dirname(os.path.abspath(__file__))
s.DATA = os.path.join(ROOT, 'data'); s.PRICES = os.path.join(s.DATA, 'prices'); os.makedirs(s.PRICES, exist_ok=True)
DOCS = ROOT
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
