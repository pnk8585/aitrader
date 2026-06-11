#!/usr/bin/env python3
"""Fetch live market data from public APIs for Market Move Watchdog."""

import json
import urllib.request
import urllib.error
import sys

def fetch_json(url, timeout=10):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return None

# 1. Yahoo Finance for AAPL, NVDA, MSFT, AMZN, META, GOOGL
symbols = ['AAPL', 'NVDA', 'MSFT', 'AMZN', 'META', 'GOOGL']
for sym in symbols:
    data = fetch_json(f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=2d&interval=1d')
    if data and data.get('chart', {}).get('result'):
        result = data['chart']['result'][0]
        meta = result.get('meta', {})
        reg = meta.get('regularMarketPrice')
        prev = meta.get('previousClose')
        if reg and prev and prev != 0:
            chg = (reg - prev) / prev * 100
            print(f'{sym}: ${reg:.2f} chg: {chg:+.2f}%')
        else:
            print(f'{sym}: NO_DATA')
    else:
        print(f'{sym}: FAILED')

# 2. S&P 500 — try a quick index check
spx = fetch_json('https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=2d&interval=1d')
if spx and spx.get('chart', {}).get('result'):
    r = spx['chart']['result'][0]['meta']
    print(f'SP500: ${r.get("regularMarketPrice","?"):} prev_close=${r.get("previousClose","?"):}')
else:
    print('SP500: FAILED')

# 3. Bitcoin, ETH, SOL from CoinGecko (free, no API key) — with 1h change
all_cg = fetch_json('https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana&vs_currencies=usd&include_1hr_change=true&include_24hr_change=true')
if all_cg:
    for coin_id, label in [('bitcoin', 'BTC'), ('ethereum', 'ETH'), ('solana', 'SOL')]:
        if coin_id in all_cg:
            c = all_cg[coin_id]
            price = c.get("usd", "?")
            h1 = c.get("usd_1h_change")
            h24 = c.get("usd_24h_change")
            ch1 = f'{h1:+.2f}%' if isinstance(h1, (int, float)) else 'N/A'
            ch24 = f'{h24:+.2f}%' if isinstance(h24, (int, float)) else 'N/A'
            print(f'{label}_CG: ${price} 1h_chg: {ch1} 24h_chg: {ch24}')
        else:
            print(f'{label}_CG: FAILED')
else:
    print('CG_ALL: FAILED')

# Try also Coinbase REST API via urllib
cb = fetch_json('https://api.pro.coinbase.com/products/BTC-USD/ticker')
if cb:
    print(f'BTC_CB: ${cb.get("price","?"):}')
else:
    print('BTC_CB: FAILED')

print('---DONE---')
