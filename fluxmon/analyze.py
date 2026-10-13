"""把链上状态转成美元口径的流动性质量指标。"""
import math

from . import lb, v3


def band_reserves(st, factor):
    return (lb if st.get("kind") == "lb" else v3).band_reserves(st, factor)


def swap_exact_in(st, amount_in, zero_for_one, fee):
    return (lb if st.get("kind") == "lb" else v3).swap_exact_in(st, amount_in, zero_for_one, fee)


def orient(pool, meta, cfg, xst):
    """决定 base（风险资产）与 quote（计价资产）。返回 base_is0。"""
    stables, weth = cfg["stables"], cfg["weth"]
    t0, t1 = pool["token0"], pool["token1"]
    if t1 in stables:
        return True
    if t0 in stables:
        return False
    if t1 == weth and t0 not in xst:
        return True
    if t0 == weth and t1 not in xst:
        return False
    return t0 in xst or t1 == weth


def group_of(pool, cfg, xst):
    return "xstock" if (pool["token0"] in xst or pool["token1"] in xst) else "weth"


def analyze_pool(pool, st, meta, usd, cfg, xst):
    t0, t1 = meta[pool["token0"]], meta[pool["token1"]]
    d0, d1 = 10 ** t0["decimals"], 10 ** t1["decimals"]
    base_is0 = orient(pool, meta, cfg, xst)
    p01 = st["sqrtP"] ** 2 * d0 / d1                       # 1 token0 值多少 token1（人类单位）
    u0, u1 = usd.get(pool["token0"]), usd.get(pool["token1"])
    if u0 is None and u1 is not None:
        u0 = u1 * p01
    if u1 is None and u0 is not None:
        u1 = u0 / p01 if p01 else None
    priced = u0 is not None and u1 is not None
    base, quote = (t0, t1) if base_is0 else (t1, t0)
    r = {
        "pool": pool["pool"], "fee_pct": pool["fee"] / 1e4, "group": group_of(pool, cfg, xst),
        "dex": pool.get("dex", "fluxion"), "kind": pool.get("kind", "v3"),
        "bin_step": pool["tickSpacing"] if pool.get("kind") == "lb" else None,
        "pair": f"{base['symbol']}/{quote['symbol']}", "base": base["symbol"], "quote": quote["symbol"],
        "base_addr": pool["token0"] if base_is0 else pool["token1"],
        "tick": st["tick"], "active_L": str(st["L"]), "initialized_ticks": len(st["ticks"]),
        "price": p01 if base_is0 else (1 / p01 if p01 else None),
        "base_usd": (u0 if base_is0 else u1) if priced else None,
        "tvl_usd": (st["bal0"] / d0 * u0 + st["bal1"] / d1 * u1) if priced else None,
        "depth": {}, "impact": {},
        "_val": {"sqrtP": st["sqrtP"], "u": [u0, u1], "d": [d0, d1]} if priced else None,   # 头寸估值用，写报告前删除
    }
    if not priced:
        return r
    for pct in cfg["depth_pcts"]:
        row = {}
        for side, f in (("up", 1 + pct / 100), ("down", 1 - pct / 100)):
            f01 = f if base_is0 else 1 / f                  # base 价格变动换算成 token1/token0 价格变动
            amt = band_reserves(st, f01)
            row[side] = amt / d0 * u0 if f01 > 1 else amt / d1 * u1
        r["depth"][str(pct)] = row
    # 累积深度曲线（0–30%，1% 步长），相邻两点之差即该价格区间内的流动性分布
    r["curve"] = {"up": [0.0], "down": [0.0]}
    for pct in range(1, 31):
        for side, f in (("up", 1 + pct / 100), ("down", 1 - pct / 100)):
            f01 = f if base_is0 else 1 / f
            amt = band_reserves(st, f01)
            r["curve"][side].append(amt / d0 * u0 if f01 > 1 else amt / d1 * u1)
    if r["tvl_usd"]:
        for pct in ("2", "10"):
            if pct in r["depth"]:
                r[f"in_band_{pct}_pct"] = (r["depth"][pct]["up"] + r["depth"][pct]["down"]) / r["tvl_usd"] * 100
    fee = pool["fee"] / 1e6
    mid = st["sqrtP"] ** 2
    for size in cfg["impact_sizes_usd"]:
        row = {}
        for side in ("buy", "sell"):                        # buy = 用 quote 买 base
            token_in_is0 = (side == "sell") == base_is0
            amt_in = size / (u0 if token_in_is0 else u1) * (d0 if token_in_is0 else d1)
            out, _, short = swap_exact_in(st, amt_in, token_in_is0, fee)
            # 成交损耗（含手续费）：按中间价计，收到的价值比投入少了多少
            value_out = out / mid if token_in_is0 else out * mid
            row[side] = None if short or not out else (1 - value_out / amt_in) * 100
        r["impact"][str(size)] = row
    return r


def lp_concentration(positions, tick, base_is0):
    act = [p for p in positions if p["L"] > 0 and p["tickLower"] <= tick < p["tickUpper"]]
    tot = sum(p["L"] for p in act)
    if not tot:
        return {"lps": 0, "active_positions": 0, "top1_pct": 0, "top3_pct": 0, "hhi": 0, "top_owner": None, "big_ranges": []}
    by = {}
    for p in act:
        by[p["owner"]] = by.get(p["owner"], 0) + p["L"]
    shares = sorted(((v / tot, o) for o, v in by.items()), reverse=True)
    big = []
    for p in sorted(act, key=lambda p: -p["L"]):
        share = p["L"] / tot
        if share < 0.05:
            break
        full = p["tickLower"] <= -887000 and p["tickUpper"] >= 887000
        lo = (1 - 1.0001 ** (p["tickLower"] - tick)) * 100       # token1/token0 价格再跌多少出区间
        hi = (1.0001 ** (p["tickUpper"] - tick) - 1) * 100        # 再涨多少出区间
        if not base_is0:   # 换成 base 价格方向
            lo, hi = (1 - 1 / (1 + hi / 100)) * 100, (1 / (1 - lo / 100) - 1) * 100 if lo < 100 else math.inf
        big.append({"owner": p["owner"], "share_pct": share * 100, "full_range": full,
                    "base_up_to_edge_pct": None if full else hi, "base_down_to_edge_pct": None if full else lo})
    return {"lps": len(by), "active_positions": len(act), "top1_pct": shares[0][0] * 100,
            "top3_pct": sum(s for s, _ in shares[:3]) * 100, "hhi": sum(s * s for s, _ in shares),
            "top_owner": shares[0][1], "big_ranges": big}


def position_usd(p, val):
    """单个 V3 头寸按当前价格折算的美元价值（不含未领取手续费）。"""
    sa, sb, sp, L = 1.0001 ** (p["tickLower"] / 2), 1.0001 ** (p["tickUpper"] / 2), val["sqrtP"], p["L"]
    if sp <= sa:
        a0, a1 = L * (sb - sa) / (sa * sb), 0.0
    elif sp >= sb:
        a0, a1 = 0.0, L * (sb - sa)
    else:
        a0, a1 = L * (sb - sp) / (sp * sb), L * (sp - sa)
    return a0 / val["d"][0] * val["u"][0] + a1 / val["d"][1] * val["u"][1]


def lp_holders(positions, val, tvl, labels):
    """按 LP 地址汇总该池全部头寸的美元价值。"""
    by = {}
    for p in positions:
        if p["L"] <= 0:
            continue
        h = by.setdefault(p["owner"], {"owner": p["owner"], "label": labels.get(p["owner"]), "usd": 0.0, "positions": 0})
        h["usd"] += position_usd(p, val)
        h["positions"] += 1
    out = sorted(by.values(), key=lambda h: -h["usd"])
    for h in out:
        h["share_pct"] = h["usd"] / tvl * 100 if tvl else None
    return out


def lp_from_holders(hs):
    """LB 池：没有价格区间头寸的概念，直接按 LP 地址的美元价值算集中度。hs 已按 usd 降序。"""
    tot = sum(h["usd"] for h in hs)
    if not tot:
        return {"lps": 0, "active_positions": 0, "top1_pct": 0, "top3_pct": 0, "hhi": 0, "top_owner": None, "big_ranges": [], "holders": hs}
    shares = [h["usd"] / tot for h in hs]
    return {"lps": len(hs), "active_positions": len(hs), "top1_pct": shares[0] * 100, "top3_pct": sum(shares[:3]) * 100,
            "hhi": sum(x * x for x in shares), "top_owner": hs[0]["owner"], "big_ranges": [], "holders": hs}
