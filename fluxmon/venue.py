"""跨链比价：Fluxion RFQ（Mantle）vs Jupiter 聚合路由（Solana）同一 xStock 的可成交报价。

成本口径统一为 bps：买入 = 成交价高于美股价多少，卖出 = 成交价低于美股价多少（越低越好）。
Solana 上的 xStocks 使用 Token-2022 Scaled UI Amount，1 原始单位 = multiplier 股，价格先换算成每股再比较。
"""
import time

from .sources import SOL_USDC, SOL_XSTOCK_DECIMALS, jupiter_quote, solana_multiplier, underlying_price

MULT_TTL_S = 6 * 3600
# 历史序列字段（看板按时段聚合用）
HISTORY_FIELDS = ["session", "f_quoted", "f_spread", "j_spread", "f_buy10", "j_buy10", "f_buy100", "j_buy100",
                  "f_sell10", "j_sell10", "buy_adv10", "buy_adv100", "sell_adv10"]


def _bps(x):
    return round(x * 1e4, 1) if x is not None else None


def _rel(a, b):
    return a / b - 1 if a and b else None


def multipliers(store, sol, symbols, now):
    """multiplier 变化很少（仅派息时），缓存 6 小时。"""
    cache = store.get("sol_multipliers", {})
    for sym in symbols:
        info = sol.get(sym)
        if not info or (sym in cache and now - cache[sym][1] < MULT_TTL_S):
            continue
        m = solana_multiplier(info["mint"], now)
        if m:
            cache[sym] = [m, now]
    store.put("sol_multipliers", cache)
    return {k: v[0] for k, v in cache.items()}


def jupiter_side(mint, mult, sizes, sell_size, delay_s):
    """Jupiter 每股买价（各档 USDC 精确输入）与 sell_size 档卖价。"""
    out = {"ask": {}, "bid": {}, "route": None}
    unit = 10 ** SOL_XSTOCK_DECIMALS
    for sz in sizes:
        q = jupiter_quote(SOL_USDC, mint, sz * 10 ** 6)
        time.sleep(delay_s)
        out["ask"][str(sz)] = sz / (q["out"] / unit * mult) if q else None
        if q and out["route"] is None:
            out["route"] = q["route"]
    ref_ask = out["ask"].get(str(sell_size)) or next((v for v in out["ask"].values() if v), None)
    if ref_ask:
        shares = sell_size / ref_ask
        q = jupiter_quote(mint, SOL_USDC, shares / mult * unit)
        time.sleep(delay_s)
        out["bid"][str(sell_size)] = q["out"] / 1e6 / shares if q else None
    return out


def compare(item, jup, ref):
    """item 为 probe_rfq_depth 的结果；返回逐项成本（bps）与 Fluxion 相对优势（正 = Fluxion 更优）。"""
    bd, sd = item.get("buy_depth") or {}, item.get("sell_depth") or {}
    f_ask10, f_ask100, f_bid10 = (bd.get("10000") or {}).get("price"), (bd.get("100000") or {}).get("price"), (sd.get("10000") or {}).get("price")
    j_ask10, j_ask100, j_bid10 = jup["ask"].get("10000"), jup["ask"].get("100000"), jup["bid"].get("10000")
    p = (ref or {}).get("price")

    def spread(a, b):
        return _bps((a - b) / ((a + b) / 2)) if a and b else None

    return {
        "ref_price": p, "ref_ts": (ref or {}).get("ts"), "jup_route": jup.get("route"),
        "f_ask10": f_ask10, "f_ask100": f_ask100, "f_bid10": f_bid10, "j_ask10": j_ask10, "j_ask100": j_ask100, "j_bid10": j_bid10,
        "f_spread": spread(f_ask10, f_bid10), "j_spread": spread(j_ask10, j_bid10),
        "f_buy10": _bps(_rel(f_ask10, p)), "j_buy10": _bps(_rel(j_ask10, p)),
        "f_buy100": _bps(_rel(f_ask100, p)), "j_buy100": _bps(_rel(j_ask100, p)),
        "f_sell10": _bps(-_rel(f_bid10, p)) if _rel(f_bid10, p) is not None else None,
        "j_sell10": _bps(-_rel(j_bid10, p)) if _rel(j_bid10, p) is not None else None,
        # 不依赖美股参考价的直接对比
        "buy_adv10": _bps(_rel(j_ask10, f_ask10)), "buy_adv100": _bps(_rel(j_ask100, f_ask100)), "sell_adv10": _bps(_rel(f_bid10, j_bid10)),
    }


def run(store, items, cfg, session, now):
    """对 RFQ 探测过的每个标的做跨链比价；结果写回 item["venue"] 并返回汇总。"""
    from .sources import xstocks_solana
    vc = cfg.get("venue_compare", {})
    delay = vc.get("delay_s", 1.1)
    sol = xstocks_solana()
    mult = multipliers(store, sol, [it["symbol"] for it in items], now)
    for it in items:
        info = sol.get(it["symbol"])
        if not info or not mult.get(it["symbol"]):
            it["venue"] = None
            continue
        jup = jupiter_side(info["mint"], mult[it["symbol"]], (10000, 100000), 10000, delay)
        it["venue"] = compare(it, jup, underlying_price(info["underlying"]))
        it["venue"]["sol_multiplier"] = mult[it["symbol"]]
    vs = [it["venue"] for it in items if it.get("venue")]

    def wins(k):
        xs = [v[k] for v in vs if v.get(k) is not None]
        return {"flux": sum(x >= 1 for x in xs), "jup": sum(x <= -1 for x in xs), "tie": sum(-1 < x < 1 for x in xs)}

    return {"buy10": wins("buy_adv10"), "buy100": wins("buy_adv100"), "sell10": wins("sell_adv10"),
            "ref_available": sum(1 for v in vs if v.get("ref_price")), "total": len(vs), "session": session["session"]}


def history_row(item, session):
    v = item.get("venue") or {}
    return [session] + [1 if item.get("status") == "active" else 0] + [v.get(k) for k in HISTORY_FIELDS[2:]]
