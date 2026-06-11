#!/usr/bin/env python3
"""Debug version - fetch raw CoinGecko data."""
import json
import urllib.request

url = 'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana&vs_currencies=usd&include_1hr_change=true&include_24hr_change=true'
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
resp = urllib.request.urlopen(req, timeout=10)
data = json.loads(resp.read().decode())
print(json.dumps(data, indent=2))
