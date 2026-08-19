"""Grid trading engine — level computation, order placement, fill tracking, rebalancing."""

import uuid
from datetime import datetime, timezone

import psycopg2.extras

from traders.strategies.grid import config as GC


def _wallet_qty(wallet_balance, base):
    """Return (total, free) of the base asset from a ccxt balance dict.

    Returns (None, None) when wallet_balance is falsy (fetch failed) so callers
    can skip reconciliation for the cycle. Handles ccxt None values defensively.
    """
    if not wallet_balance:
        return None, None
    entry = wallet_balance.get(base) or {}
    total, free = entry.get("total"), entry.get("free")
    try:
        total = float(total) if total is not None else 0.0
    except (TypeError, ValueError):
        total = 0.0
    try:
        free = float(free) if free is not None else 0.0
    except (TypeError, ValueError):
        free = 0.0
    return total, free


# ── paper-mode limit order helpers ──────────────────────────────

def _paper_limit(side, symbol, qty, price):
    """Simulate a limit order in paper mode."""
    oid = f"paper-limit-{uuid.uuid4().hex[:12]}"
    return {
        "id": oid,
        "clientOrderId": oid,
        "average": price,
        "price": price,
        "filled": qty,
        "remaining": 0.0,
        "cost": qty * price,
        "side": side,
        "symbol": symbol,
        "status": "closed",
        "info": {"paper": True},
    }


def _paper_limit_check(order, current_price):
    """In paper mode, limit orders fill when price crosses the limit.
    Returns True if the order would have filled by now."""
    limit_price = float(order["price"])
    side = order["side"]
    if side == "buy":
        return current_price <= limit_price
    else:
        return current_price >= limit_price


# ── grid range computation ──────────────────────────────────────

def compute_grid_range(conn, pair):
    """Auto-detect grid range from 30-day asset_prices percentiles.

    Returns (grid_low, grid_high) or None if insufficient data / range too tight.
    """
    base = pair.split("/")[0].upper()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT percentile_cont(0.05) WITHIN GROUP (ORDER BY price),
                          percentile_cont(0.95) WITHIN GROUP (ORDER BY price)
                   FROM asset_prices
                   WHERE exchange = 'kraken' AND symbol = %s
                     AND timestamp >= NOW() - INTERVAL '%s days'""",
                (base, GC.RANGE_LOOKBACK_DAYS),
            )
            row = cur.fetchone()
    except Exception:
        return None

    if not row or row[0] is None or row[1] is None:
        return None

    p5, p95 = float(row[0]), float(row[1])
    if p5 <= 0:
        return None

    range_pct = (p95 - p5) / p5 * 100.0
    if range_pct < GC.RANGE_MIN_PCT:
        return None
    if range_pct > GC.RANGE_MAX_PCT:
        center = (p5 + p95) / 2.0
        half = center * GC.RANGE_MAX_PCT / 100.0 / 2.0
        p5 = center - half
        p95 = center + half

    grid_low = p5 * (1 - GC.RANGE_BUFFER_PCT / 100.0)
    grid_high = p95 * (1 + GC.RANGE_BUFFER_PCT / 100.0)
    return round(grid_low, 6), round(grid_high, 6)


def _compute_grid_spread(grid_low, grid_high, num_grids):
    """Per-level spread as percentage, after adjusting num_grids if needed."""
    spread = (grid_high - grid_low) / grid_low / num_grids * 100.0
    if spread < GC.MIN_GRID_SPREAD_PCT:
        needed = int((grid_high - grid_low) / grid_low / (GC.MIN_GRID_SPREAD_PCT / 100.0))
        num_grids = max(2, needed)
        spread = (grid_high - grid_low) / grid_low / num_grids * 100.0
    return spread, num_grids


# ── grid lifecycle ──────────────────────────────────────────────

def create_grid(conn, pair, cash_eur, available_cash=None):
    """Create a new grid for `pair`. Returns grid dict or None if range unavailable."""
    rng = compute_grid_range(conn, pair)
    if rng is None:
        return None
    grid_low, grid_high = rng

    effective_cash = available_cash if available_cash is not None else cash_eur
    capital = min(effective_cash * GC.CAPITAL_PER_GRID_PCT, effective_cash / GC.MAX_OPEN_GRIDS)
    if capital < GC.MIN_TRADE_EUR * GC.NUM_GRIDS:
        return None

    spread_pct, num_grids = _compute_grid_spread(grid_low, grid_high, GC.NUM_GRIDS)
    capital_per_level = capital / num_grids

    levels = []
    step = (grid_high - grid_low) / (num_grids - 1) if num_grids > 1 else 0
    for i in range(num_grids):
        price = round(grid_low + step * i, 6)
        qty = round(capital_per_level / price, 6)
        if qty * price < GC.MIN_TRADE_EUR:
            continue
        levels.append({
            "price": price,
            "status": "idle",
            "buy_qty": qty,
            "buy_price": None,
            "sell_price": None,
            "cycles_since_placed": 0,
            "order_id": None,
        })

    if len(levels) < 2:
        return None

    return {
        "symbol": pair,
        "grid_low": grid_low,
        "grid_high": grid_high,
        "num_grids": len(levels),
        "capital_allocated": capital,
        "spread_pct": round(spread_pct, 4),
        "levels": levels,
        "total_buys": 0,
        "total_sells": 0,
        "realized_pnl": 0,
        "status": "active",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def load_grid(conn, pair, exchange_name):
    """Load grid state from DB. Returns dict or None."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, symbol, grid_low, grid_high, num_grids, capital_allocated, "
                "levels, total_buys, total_sells, realized_pnl, status, "
                "created_at, updated_at "
                "FROM grid_state WHERE symbol = %s AND exchange = %s",
                (pair, exchange_name),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "symbol": row[1],
            "grid_low": float(row[2]),
            "grid_high": float(row[3]),
            "num_grids": row[4],
            "capital_allocated": float(row[5]),
            "levels": row[6] if isinstance(row[6], list) else [],
            "total_buys": row[7],
            "total_sells": row[8],
            "realized_pnl": float(row[9]) if row[9] else 0,
            "status": row[10],
            "created_at": row[11].isoformat() if row[11] else None,
            "updated_at": row[12].isoformat() if row[12] else None,
        }
    except Exception:
        return None


def save_grid(conn, grid, exchange_name):
    """Upsert grid state to DB."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO grid_state
                   (symbol, exchange, grid_low, grid_high, num_grids, capital_allocated,
                    levels, total_buys, total_sells, realized_pnl, status, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                   ON CONFLICT (symbol, exchange) DO UPDATE SET
                     grid_low = EXCLUDED.grid_low,
                     grid_high = EXCLUDED.grid_high,
                     num_grids = EXCLUDED.num_grids,
                     capital_allocated = EXCLUDED.capital_allocated,
                     levels = EXCLUDED.levels,
                     total_buys = EXCLUDED.total_buys,
                     total_sells = EXCLUDED.total_sells,
                     realized_pnl = EXCLUDED.realized_pnl,
                     status = EXCLUDED.status,
                     updated_at = NOW()""",
                (
                    grid["symbol"], exchange_name,
                    grid["grid_low"], grid["grid_high"], grid["num_grids"],
                    grid["capital_allocated"],
                    psycopg2.extras.Json(grid["levels"]),
                    grid["total_buys"], grid["total_sells"],
                    grid["realized_pnl"], grid["status"],
                ),
            )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


# ── order placement ─────────────────────────────────────────────

def place_limit_buy(exchange, pair, qty, price, is_paper):
    """Place a limit buy order. Returns order dict."""
    if is_paper:
        return _paper_limit("buy", pair, qty, price)
    return exchange.create_limit_buy_order(pair, qty, price)


def place_limit_sell(exchange, pair, qty, price, is_paper):
    """Place a limit sell order. Returns order dict."""
    if is_paper:
        return _paper_limit("sell", pair, qty, price)
    return exchange.create_limit_sell_order(pair, qty, price)


def place_market_sell(exchange, pair, qty, price_hint, is_paper):
    """Place a market sell (fallback for unfilled limit)."""
    if is_paper:
        oid = f"paper-market-{uuid.uuid4().hex[:12]}"
        return {
            "id": oid, "average": price_hint, "price": price_hint,
            "filled": qty, "cost": qty * price_hint, "side": "sell",
            "symbol": pair, "status": "closed", "info": {"paper": True},
        }
    return exchange.create_market_sell_order(pair, qty)


def place_market_buy(exchange, pair, qty, price_hint, is_paper):
    """Place a market buy (fallback for unfilled limit)."""
    if is_paper:
        oid = f"paper-market-{uuid.uuid4().hex[:12]}"
        return {
            "id": oid, "average": price_hint, "price": price_hint,
            "filled": qty, "cost": qty * price_hint, "side": "buy",
            "symbol": pair, "status": "closed", "info": {"paper": True},
        }
    return exchange.create_market_buy_order(pair, qty)


def check_order_filled(exchange, order_id, pair, is_paper):
    """Check if a limit order has filled. Returns (filled, fill_price) or (False, None)."""
    if is_paper or (isinstance(order_id, str) and order_id.startswith("paper-")):
        return False, None
    try:
        order = exchange.fetch_order(order_id, pair)
        if order and order.get("status") == "closed":
            fill_price = float(order.get("average") or order.get("price") or 0)
            return True, fill_price
    except Exception:
        pass
    return False, None


# ── cycle logic ─────────────────────────────────────────────────

def run_cycle(conn, exchange, pair, grid, is_paper):
    """Process one grid cycle: check each level, place/fill orders, handle stops.

    Returns (grid, report) where report is a list of action strings for logging/notify.
    """
    report = []
    if grid["status"] != "active":
        return grid, report

    try:
        ticker = exchange.fetch_ticker(pair)
        current_price = float(ticker.get("last", 0))
    except Exception:
        report.append(f"⚠️ Could not fetch price for {pair}")
        return grid, report

    if current_price <= 0:
        return grid, report

    # Wallet reconciliation — fetch once per cycle (live mode only).
    # Fail-safe: if fetch_balance fails, skip reconciliation this cycle, retry next.
    wallet_balance = None
    if not is_paper:
        try:
            wallet_balance = exchange.fetch_balance() or {}
        except Exception:
            pass

    modified = False

    for i, level in enumerate(grid["levels"]):
        status = level.get("status", "idle")
        buy_qty = level.get("buy_qty", 0)
        buy_price = level.get("buy_price")
        order_id = level.get("order_id")
        cycles = level.get("cycles_since_placed", 0)

        # ── hard stop check for active positions ──
        if status in ("buy_filled", "sell_placed") and buy_price:
            plpc = (current_price - buy_price) / buy_price * 100.0
            if plpc <= GC.GRID_HARD_STOP_PCT:
                _stop_level(conn, exchange, grid, i, level, pair, current_price,
                            is_paper, report)
                modified = True
                continue

        # ── IDLE → place buy ──
        if status == "idle" and current_price <= level["price"] * 1.005:
            try:
                order = place_limit_buy(exchange, pair, buy_qty, level["price"], is_paper)
                level["status"] = "buy_placed"
                level["order_id"] = order.get("id")
                level["cycles_since_placed"] = 0
                modified = True
                report.append(f"📥 Limit buy {pair} qty={buy_qty} @{level['price']}")
            except Exception as e:
                report.append(f"❌ Buy order failed {pair} @{level['price']}: {e}")

        # ── BUY_PLACED → check fill ──
        elif status == "buy_placed":
            filled = False
            fill_price = level["price"]
            if is_paper and order_id:
                filled = _paper_limit_check(
                    {"price": level["price"], "side": "buy"}, current_price
                )
            elif order_id:
                filled, fp = check_order_filled(exchange, order_id, pair, is_paper)
                if fp:
                    fill_price = fp

            if filled:
                level["status"] = "buy_filled"
                level["buy_price"] = fill_price
                level["cycles_since_placed"] = 0
                grid["total_buys"] += 1
                modified = True
                report.append(f"✅ Buy filled {pair} qty={buy_qty} @{fill_price}")
            else:
                level["cycles_since_placed"] = cycles + 1
                # fallback to market after LIMIT_ORDER_CYCLES
                if level["cycles_since_placed"] >= GC.LIMIT_ORDER_CYCLES:
                    try:
                        order = place_market_buy(exchange, pair, buy_qty, current_price, is_paper)
                        level["status"] = "buy_filled"
                        level["buy_price"] = float(order.get("average") or current_price)
                        level["order_id"] = order.get("id")
                        level["cycles_since_placed"] = 0
                        grid["total_buys"] += 1
                        modified = True
                        report.append(f"✅ Market buy (fallback) {pair} qty={buy_qty} @{level['buy_price']}")
                    except Exception as e:
                        report.append(f"❌ Market buy fallback failed {pair}: {e}")
                modified = True

        # ── BUY_FILLED → place sell ──
        elif status == "buy_filled" and buy_price:
            sell_price = buy_price * (1 + grid.get("spread_pct", GC.MIN_GRID_SPREAD_PCT) / 100.0)
            if current_price >= sell_price * 0.995:
                base = pair.split("/")[0]
                total_qty, free_qty = _wallet_qty(wallet_balance, base)

                if not is_paper and total_qty is not None and total_qty <= 0:
                    # Phantom level — wallet truly holds no coins anywhere → reset.
                    level["status"] = "idle"
                    level["buy_price"] = None
                    level["sell_price"] = None
                    level["order_id"] = None
                    level["cycles_since_placed"] = 0
                    modified = True
                    report.append(f"🧹 Phantom level reset {pair}: wallet holds no {base}")
                    continue

                if not is_paper and total_qty is not None and free_qty is not None and free_qty < buy_qty:
                    # Coins held but locked elsewhere (open orders) — defer this
                    # sell to a later cycle instead of double-selling.
                    report.append(f"⏳ Sell deferred {pair}: free {base}={round(free_qty,6)} < {buy_qty} (locked elsewhere)")
                    continue

                try:
                    order = place_limit_sell(exchange, pair, buy_qty, sell_price, is_paper)
                    level["status"] = "sell_placed"
                    level["sell_price"] = sell_price
                    level["order_id"] = order.get("id")
                    level["cycles_since_placed"] = 0
                    modified = True
                    report.append(f"📤 Limit sell {pair} qty={level['_actual_sell_qty']} @{sell_price}")
                except Exception as e:
                    level.pop("_actual_sell_qty", None)
                    report.append(f"❌ Sell order failed {pair} @{sell_price}: {e}")

        # ── SELL_PLACED → check fill ──
        elif status == "sell_placed":
            filled = False
            fill_price = level.get("sell_price", current_price)
            if is_paper and order_id:
                filled = _paper_limit_check(
                    {"price": fill_price, "side": "sell"}, current_price
                )
            elif order_id:
                filled, fp = check_order_filled(exchange, order_id, pair, is_paper)
                if fp:
                    fill_price = fp

            if filled:
                actual_qty = level.pop("_actual_sell_qty", buy_qty)
                pnl = (fill_price - buy_price) * actual_qty if buy_price else 0
                level["status"] = "idle"
                level["buy_price"] = None
                level["sell_price"] = None
                level["order_id"] = None
                level["cycles_since_placed"] = 0
                grid["total_sells"] += 1
                grid["realized_pnl"] = round(grid.get("realized_pnl", 0) + pnl, 6)
                modified = True
                report.append(f"💰 Cycle complete {pair}: bought @{buy_price} sold @{fill_price} (+{round(pnl, 4)}€)")
                grid.setdefault("_cycle_trades", []).append({
                    "qty": actual_qty, "price": fill_price, "pnl": pnl
                })
            else:
                level["cycles_since_placed"] = cycles + 1
                if level["cycles_since_placed"] >= GC.LIMIT_ORDER_CYCLES:
                    base = pair.split("/")[0]
                    total_qty, free_qty = _wallet_qty(wallet_balance, base)
                    actual_qty = buy_qty
                    if not is_paper and total_qty is not None and total_qty <= 0:
                        # Phantom level — wallet truly holds no coins → clean up.
                        if order_id:
                            try:
                                exchange.cancel_order(order_id, pair)
                            except Exception:
                                pass
                        level["status"] = "idle"
                        level["buy_price"] = None
                        level["sell_price"] = None
                        level["order_id"] = None
                        level["cycles_since_placed"] = 0
                        modified = True
                        report.append(f"🧹 Phantom level reset {pair}: wallet holds no {base}")
                        continue
                    if not is_paper and total_qty is not None and free_qty is not None and free_qty < buy_qty:
                        # Coins exist but are locked in open orders — do NOT market
                        # sell on top of them; retry next cycle.
                        report.append(f"⏳ Market sell fallback deferred {pair}: free {base}={round(free_qty,6)} < {buy_qty} (locked)")
                        continue

                    # Cancel our own unfilled limit sell first, then re-check free
                    # balance before market-selling (avoid double-selling).
                    if not is_paper and order_id:
                        try:
                            exchange.cancel_order(order_id, pair)
                        except Exception:
                            pass  # already filled or gone
                        try:
                            fresh = exchange.fetch_balance() or {}
                            _, fresh_free = _wallet_qty(fresh, base)
                            if fresh_free is not None and fresh_free < buy_qty:
                                report.append(f"⏳ Market sell fallback deferred {pair}: free {base}={round(fresh_free,6)} < {buy_qty} after cancel")
                                continue
                        except Exception:
                            pass  # balance re-check failed — proceed cautiously

                    try:
                        order = place_market_sell(exchange, pair, actual_qty, current_price, is_paper)
                        fill_price = float(order.get("average") or current_price)
                        pnl = (fill_price - buy_price) * actual_qty if buy_price else 0
                        level["status"] = "idle"
                        level["buy_price"] = None
                        level["sell_price"] = None
                        level["order_id"] = None
                        level["cycles_since_placed"] = 0
                        grid["total_sells"] += 1
                        grid["realized_pnl"] = round(grid.get("realized_pnl", 0) + pnl, 6)
                        modified = True
                        report.append(f"💰 Market sell (fallback) {pair}: bought @{buy_price} sold @{fill_price} (+{round(pnl, 4)}€)")
                        grid.setdefault("_cycle_trades", []).append({
                            "qty": actual_qty, "price": fill_price, "pnl": pnl
                        })
                    except Exception as e:
                        report.append(f"❌ Market sell fallback failed {pair}: {e}")
                modified = True

    # ── rebalance check ──
    if _needs_rebalance(grid, current_price):
        grid = _rebalance(grid, pair, current_price)
        report.append(f"📏 Grid rebalanced: {pair} range extended to €{grid['grid_low']}-€{grid['grid_high']}")
        modified = True

    # ── check if >50% levels stopped → pause ──
    stopped = sum(1 for l in grid["levels"] if l.get("status") == "stopped")
    if stopped > len(grid["levels"]) / 2:
        grid["status"] = "paused"
        report.append(f"🛑 Grid paused: {pair} — {stopped}/{len(grid['levels'])} levels hit hard stop")

    if modified:
        grid["updated_at"] = datetime.now(timezone.utc).isoformat()

    return grid, report


def _stop_level(conn, exchange, grid, idx, level, pair, price, is_paper, report):
    """Hard-stop a single grid level: market sell, mark as stopped."""
    buy_qty = level.get("buy_qty", 0)
    buy_price = level.get("buy_price", price)
    try:
        order = place_market_sell(exchange, pair, buy_qty, price, is_paper)
        fill_price = float(order.get("average") or price)
        loss = (fill_price - buy_price) * buy_qty if buy_price else 0
        grid["realized_pnl"] = round(grid.get("realized_pnl", 0) + loss, 6)
        level["status"] = "stopped"
        level["buy_price"] = None
        level["sell_price"] = None
        level["order_id"] = None
        level["cycles_since_placed"] = 0
        grid["total_sells"] += 1
        report.append(f"🛑 Hard stop {pair}: sold @{fill_price} (entry @{buy_price}, loss €{round(abs(loss), 4)})")
    except Exception as e:
        report.append(f"❌ Hard stop sell failed {pair}: {e}")


def _needs_rebalance(grid, current_price):
    """Check if current price has drifted outside the grid range."""
    return current_price < grid["grid_low"] or current_price > grid["grid_high"]


def _rebalance(grid, pair, current_price):
    """Extend grid range when price exits bounds."""
    original_num = grid["num_grids"]

    if current_price < grid["grid_low"]:
        extend = grid["grid_low"] * (GC.EXTEND_RANGE_PCT / 100.0)
        grid["grid_low"] = round(grid["grid_low"] - extend, 6)
    elif current_price > grid["grid_high"]:
        extend = grid["grid_high"] * (GC.EXTEND_RANGE_PCT / 100.0)
        grid["grid_high"] = round(grid["grid_high"] + extend, 6)

    spread_pct, _ = _compute_grid_spread(grid["grid_low"], grid["grid_high"], grid["num_grids"])
    grid["spread_pct"] = round(spread_pct, 4)

    step = (grid["grid_high"] - grid["grid_low"]) / (grid["num_grids"] - 1) if grid["num_grids"] > 1 else 0
    capital_per_level = grid["capital_allocated"] / grid["num_grids"]

    # Keep ALL existing non-idle levels (they represent real positions)
    preserved = [l for l in grid["levels"] if l.get("status") != "idle"]
    preserved_prices = {l["price"] for l in preserved}

    new_levels = list(preserved)
    for i in range(grid["num_grids"]):
        price = round(grid["grid_low"] + step * i, 6)
        if any(abs(lp - price) < 0.0001 for lp in preserved_prices):
            continue
        qty = round(capital_per_level / price, 6) if price > 0 else 0
        new_levels.append({
            "price": price,
            "status": "idle",
            "buy_qty": qty,
            "buy_price": None,
            "sell_price": None,
            "cycles_since_placed": 0,
            "order_id": None,
        })

    # Cap at 2× original num_grids — remove idle levels furthest from current price
    max_levels = original_num * 2
    if len(new_levels) > max_levels:
        idle_idx = [(j, l) for j, l in enumerate(new_levels) if l.get("status") == "idle"]
        idle_idx.sort(key=lambda x: abs(x[1]["price"] - current_price), reverse=True)
        remove = {j for j, _ in idle_idx[:len(new_levels) - max_levels]}
        new_levels = [l for j, l in enumerate(new_levels) if j not in remove]

    grid["levels"] = new_levels
    grid["num_grids"] = len(new_levels)
    return grid
