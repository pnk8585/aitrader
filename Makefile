.PHONY: install test lint compile

install:
	pip install -e ".[dev]"

test:
	pytest tests/ -v

compile:
	python3 -m py_compile traders/crypto_trades/kraken_pullback.py
	python3 -m py_compile traders/crypto_trades/kraken_momentum.py
	python3 -m py_compile traders/trades/alpaca_stocks.py

pnl:
	python3 scripts/pnl_dashboard.py

backtest:
	python3 research/backtest_pullback.py