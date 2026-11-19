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
prev = None
try: prev = json.load(open(os.path.join(DOCS, 'state.json')))
except Exception: pass
s.API_KEYS['ia'] = os.environ.get('INDIANAPI_KEY', '').strip(); s.API_KEYS['ed'] = os.environ.get('EODHD_KEY', '').strip()
s.API_PREV = {'usage': (prev or {}).get('api_usage'), 'cache': (prev or {}).get('fund_cache') or {}}
print('API keys present: IndianAPI', bool(s.API_KEYS['ia']), '| EODHD', bool(s.API_KEYS['ed']))
s.USE_NSE = True            # Yahoo blocks GitHub's servers, so use NSE's own daily files
s.refresh()
st = s.CACHE.get('state')
if not st or s.STATUS['msg'].startswith('Failed'):
    print('Refresh failed:', s.STATUS['msg']); sys.exit(1)
nb = s.CACHE['nifty']; st['nifty'] = {'d': nb['d'], 'c': nb['c']}
st['static'] = True
# ---- alerts: new Tier 1/2 ideas and closed paper trades, pushed to your phone through ntfy.sh if you set a topic
alerts = s.make_alerts(prev, st); st['alerts'] = alerts
topic = os.environ.get('NTFY_TOPIC', '').strip(); st['ntfy'] = bool(topic)
if topic:
    repo = os.environ.get('GITHUB_REPOSITORY', ''); click = ('https://%s.github.io/%s/' % (repo.split('/')[0].lower(), repo.split('/')[1])) if '/' in repo else ''
    try:
        if not (prev or {}).get('ntfy'):
            s.push_ntfy(topic, 'Swing Desk alerts are on', 'You will get a message here when a new Tier 1 or Tier 2 idea appears or a paper trade closes.', click); print('ALERT hello sent')
        if alerts:
            ni = sum(1 for a in alerts if a['k'] == 'idea'); nx = len(alerts) - ni
            title = 'Swing Desk: ' + ', '.join(x for x in (('%d new idea%s' % (ni, '' if ni == 1 else 's')) if ni else '', ('%d trade%s closed' % (nx, '' if nx == 1 else 's')) if nx else '') if x)
            s.push_ntfy(topic, title, '\n'.join('• ' + a['text'] for a in alerts[:8]), click); print('ALERT sent:', len(alerts))
        else: print('ALERT none today')
    except Exception as e: print('ALERT failed:', str(e)[:100])
else: print('ALERT off (no NTFY_TOPIC set)')
json.dump(st, open(os.path.join(DOCS, 'state.json'), 'w'), separators=(',', ':'))
for k in ('brk', 'pb', 'mr'):
    t = s.collect_trades(k); json.dump(t, open(os.path.join(DOCS, f'trades_{k}.json'), 'w'), separators=(',', ':')); print(k, len(t), 'trades')
print('Done. Prices up to', st['asof'])
