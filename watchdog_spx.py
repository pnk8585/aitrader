#!/usr/bin/env python3
"""Fetch S&P 500 index data."""
import json
import urllib.request

def fetch_json(url, timeout=10):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return None

# S&P 500 futures (ES) from Yahoo Finance
spx = fetch_json('https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=5d&interval=1d')
if spx and spx.get('chart', {}).get('result'):
    r = spx['chart']['result'][0]['meta']
    reg = r.get('regularMarketPrice', 0)
    prev = r.get('previousClose', 0)
    if reg and prev and prev != 0:
        chg = (reg - prev) / prev * 100
        print(f'SP500: ${reg} | prev_close: ${prev} | chg: {chg:+.2f}%')
    else:
        print(f'SP500: ${reg} prev_close: ${prev}')
else:
    print('SP500: FAILED')

# S&P 500 futures (/ES) for after-hours
es = fetch_json('https://query1.finance.yahoo.com/v8/finance/chart/ES=F?range=5d&interval=1d')
if es and es.get('chart', {}).get('result'):
    r = es['chart']['result'][0]['meta']
    reg = r.get('regularMarketPrice', 0)
    prev = r.get('previousClose', 0)
    if reg and prev and prev != 0:
        chg = (reg - prev) / prev * 100
        print(f'ES_FUT: ${reg} | prev_close: ${prev} | chg: {chg:+.2f}%')
    else:
        print(f'ES_FUT: ${reg} prev_close: ${prev}')
else:
    print('ES_FUT: FAILED')
