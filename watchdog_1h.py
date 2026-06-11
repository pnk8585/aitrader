#!/usr/bin/env python3
"""Fetch 1h price changes from CoinGecko market_chart API."""
import json
import urllib.request
import time

def fetch_json(url, timeout=10):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return None

# Get 1 hour ago timestamp
now = int(time.time())
one_hour_ago = now - 3600

# Fetch price history for BTC, ETH, SOL (hourly data for 2 days to get accurate 1h change)
for coin_id, label in [('bitcoin', 'BTC'), ('ethereum', 'ETH'), ('solana', 'SOL')]:
    data = fetch_json(f'https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart?vs_currency=usd&days=2')
    if data and 'prices' in data:
        prices = data['prices']
        current_price = prices[-1][1]
        
        # Find price closest to 1 hour ago
        one_h_ago_price = None
        min_diff = float('inf')
        for ts, pr in prices:
            diff = abs(ts/1000 - one_hour_ago)
            if diff < min_diff:
                min_diff = diff
                one_h_ago_price = pr
        
        if one_h_ago_price and current_price and one_h_ago_price != 0:
            change_1h = (current_price - one_h_ago_price) / one_h_ago_price * 100
            print(f'{label}: ${current_price:.2f} | 1h_chg: {change_1h:+.2f}% | 1h_ago: ${one_h_ago_price:.2f}')
        else:
            print(f'{label}: ${current_price:.2f} | 1h_chg: N/A (no 1h price point)')
    else:
        print(f'{label}: FAILED')
