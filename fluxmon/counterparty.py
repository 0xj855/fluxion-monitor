"""Bybit xStocks 充提的对手方画像：把与交易所往来的地址归并成实体，按行为分成 做市/套利/机构、散户、未判定。

这是行为推断，不是真实身份。规则：
- 实体归并：只从 1–2 个来源收币、再全部转给交易所的地址视为交易所专属充值地址，归到上游；
  从交易所提币后 ≥90% 转给同一个普通地址，也视为同一人的另一个钱包。
- 做市/套利/机构：满足任一 MM_RULES；散户：同时满足至少 RETAIL_MIN 条散户特征；其余未判定。
判定依据以 {code, ...参数} 输出，文案由看板按语言渲染。
"""
import collections
import statistics

from . import chain

WINDOWS = {"24h": 1, "7d": 7, "30d": 30}
INFO_TTL_S = 86400
RULES = {"flows_min": 20, "nonce_min": 200, "wrap_min": 10, "issuer_usd_min": 10_000,
         "fast_hold_h": 6, "fast_usd_min": 1_000, "retail_flows_max": 5, "retail_hold_h": 24, "retail_min": 4}


def _addr_info(store, addrs, now):
    """链上画像（是否合约、nonce），缓存一天。"""
    cache = store.get("cp_addr_info", {})
    for a in addrs:
        if a in cache and now - cache[a][2] < INFO_TTL_S:
            continue
        try:
            code = chain.retry(lambda: chain.rpc("eth_getCode", [a, "latest"]), tries=3)
            n = int(chain.retry(lambda: chain.rpc("eth_getTransactionCount", [a, "latest"]), tries=3), 16)
            cache[a] = [len(code) > 2, n, now]
        except Exception as e:
            print("counterparty addr info failed:", a, e)
    store.put("cp_addr_info", {k: v for k, v in cache.items() if now - v[2] < 7 * INFO_TTL_S})
    return {a: {"contract": v[0], "nonce": v[1]} for a, v in cache.items()}


def classify(store, labels, xcat, prices, head, now, known_mm=(), days=30):
    """labels: flows.labels；prices: {symbol: 美元价}；known_mm: 已知做市商地址（公开产物传空）。"""
    lab = {a.lower(): v for a, v in labels.items()}
    ex = {a for a, v in lab.items() if v.get("kind") == "exchange"}
    issuer = {a for a, v in lab.items() if v.get("kind") == "issuer"}
    wrappers = {a for a, v in xcat.items() if v["kind"] == "wrapper"}
    special = ex | issuer | wrappers | {"0x" + "0" * 40}
    known_mm = {a.lower() for a in known_mm}
    lp_owners = {o for (o,) in store.db.execute("SELECT DISTINCT owner FROM positions WHERE liquidity!='0'")}
    bpd = chain.BLOCKS_PER_DAY
    usd = lambda sym, amt: amt * (prices.get(sym) or 0)

    rows = store.xfers_since(0)   # (block, symbol, src, dst, amount)
    out_by, in_by = collections.defaultdict(list), collections.defaultdict(list)
    for b, s, f, t, a in rows:
        out_by[f].append((b, s, t, a))
        in_by[t].append((b, s, f, a))
    since = head - days * bpd
    wd = [(b, s, t, a) for b, s, f, t, a in rows if f in ex and t not in ex and b >= since]
    dp = [(b, s, f, a) for b, s, f, t, a in rows if t in ex and f not in ex and b >= since]

    parent = {}
    def find(a):
        while parent.get(a, a) != a:
            a = parent[a]
        return a

    deposit_up = {}   # 充值地址 -> 上游
    for a in {f for _, _, f, _ in dp}:
        srcs = collections.Counter()
        for _, s, f, amt in in_by[a]:
            srcs[f] += usd(s, amt)
        if out_by[a] and all(t in ex for _, _, t, _ in out_by[a]) and 0 < len(srcs) <= 2:
            up = srcs.most_common(1)[0][0]
            deposit_up[a] = up
            if up not in ex:
                parent[a] = up
    for a in {t for _, _, t, _ in wd}:
        dst = collections.Counter()
        for _, s, t, amt in out_by[a]:
            dst[t] += usd(s, amt)
        tot = sum(dst.values())
        if tot:
            top, v = dst.most_common(1)[0]
            if top not in special and top not in deposit_up and v / tot >= 0.9 and find(top) != find(a):
                parent[find(a)] = find(top)

    ents = collections.defaultdict(lambda: {"addrs": set(), "wd": [], "dp": []})
    for b, s, t, a in wd:
        e = ents[find(t)]; e["addrs"].add(t); e["wd"].append((b, s, a))
    for b, s, f, a in dp:
        e = ents[find(deposit_up.get(f, f)) if deposit_up.get(f, f) not in ex else find(f)]
        e["addrs"].add(f); e["dp"].append((b, s, a))
    info = _addr_info(store, sorted({a for k, e in ents.items() for a in e["addrs"] | {k}}), now)

    out = []
    for k, e in ents.items():
        members = e["addrs"] | {k}
        wrap = sum(t in wrappers for m in members for _, _, t, _ in out_by[m]) + sum(f in wrappers for m in members for _, _, f, _ in in_by[m])
        iss_usd = sum(usd(s, a) for m in members for _, s, t, a in out_by[m] if t in issuer) + \
            sum(usd(s, a) for m in members for _, s, f, a in in_by[m] if f in issuer)
        holds = []
        for b, s, _ in e["wd"]:
            nxt = [ob for m in members for ob, os_, _, _ in out_by[m] if os_ == s and ob > b]
            holds.append((min(nxt) - b) * 86400 / bpd / 3600 if nxt else None)
        held = [h for h in holds if h is not None]
        med = statistics.median(held) if held else None
        nonce = max(info.get(m, {}).get("nonce", 0) for m in members)
        n = len(e["wd"]) + len(e["dp"])
        both = bool(e["wd"]) and bool(e["dp"])
        syms = sorted({s for _, s, _ in e["wd"] + e["dp"]})
        wd_usd, dp_usd = sum(usd(s, a) for _, s, a in e["wd"]), sum(usd(s, a) for _, s, a in e["dp"])

        R, why, sub = RULES, [], None
        if any(deposit_up.get(m) in ex for m in members):
            cls, why = "unknown", [{"code": "internal"}]
        else:
            if members & known_mm:
                why.append({"code": "known_mm"})
            if members & lp_owners:
                why.append({"code": "lp"})
            if iss_usd >= R["issuer_usd_min"]:
                why.append({"code": "issuer", "usd": iss_usd})
            if n >= R["flows_min"]:
                why.append({"code": "many_flows", "n": n})
            if both and nonce >= R["nonce_min"]:
                why.append({"code": "two_way_active", "nonce": nonce})
            if wrap >= R["wrap_min"]:
                why.append({"code": "wrap", "n": wrap})
            if med is not None and med < R["fast_hold_h"] and len(held) >= 3 and wd_usd + dp_usd >= R["fast_usd_min"]:
                why.append({"code": "fast", "h": med})
            if why:
                cls = "mm"
                sub = ("dex_arb" if wrap >= R["wrap_min"] else "issuer" if iss_usd >= R["issuer_usd_min"]
                       else "shuttle" if both else "other")
            else:
                if n <= R["retail_flows_max"]:
                    why.append({"code": "few_flows", "n": n})
                if nonce < R["nonce_min"]:
                    why.append({"code": "low_nonce", "nonce": nonce})
                if not both:
                    why.append({"code": "one_way"})
                if len(syms) <= 2:
                    why.append({"code": "few_syms", "n": len(syms)})
                if (med is None and holds) or (med is not None and med >= R["retail_hold_h"]):
                    why.append({"code": "held", "h": med})
                cls = "retail" if len(why) >= R["retail_min"] else "unknown"
        out.append({"entity": k, "members": sorted(members), "deposit_addrs": sorted(m for m in members if m in deposit_up),
                    "class": cls, "sub": sub, "why": why, "nonce": nonce, "contract": any(info.get(m, {}).get("contract") for m in members),
                    "wd_n": len(e["wd"]), "wd_usd": wd_usd, "dp_n": len(e["dp"]), "dp_usd": dp_usd, "syms": syms,
                    "wrap": wrap, "issuer_usd": iss_usd, "hold_median_h": med,
                    "_flows": [("wd", b, s, usd(s, a)) for b, s, a in e["wd"]] + [("dp", b, s, usd(s, a)) for b, s, a in e["dp"]]})

    windows, by_symbol, subtypes = {}, collections.defaultdict(lambda: collections.defaultdict(float)), {}
    for name, d in WINDOWS.items():
        lo = head - d * bpd
        agg = {}
        for en in out:
            hit = False
            for kind, b, s, u in en["_flows"]:
                if b >= lo:
                    g = agg.setdefault(en["class"], {"wd_n": 0, "wd_usd": 0.0, "dp_n": 0, "dp_usd": 0.0, "entities": 0})
                    g[kind + "_n"] += 1; g[kind + "_usd"] += u; hit = True
                    if name == "30d":
                        by_symbol[s][en["class"]] += u
            if hit:
                agg[en["class"]]["entities"] += 1
        windows[name] = agg
    for en in out:
        if en["class"] == "mm":
            st = subtypes.setdefault(en["sub"], {"entities": 0, "flows": 0, "usd": 0.0})
            st["entities"] += 1; st["flows"] += en["wd_n"] + en["dp_n"]; st["usd"] += en["wd_usd"] + en["dp_usd"]
        en.pop("_flows")
    out.sort(key=lambda x: -(x["wd_usd"] + x["dp_usd"]))
    return {"ts": now, "head_block": head, "days": days, "rules": RULES, "prices": prices,
            "windows": windows, "subtypes": subtypes, "by_symbol": {k: dict(v) for k, v in by_symbol.items()}, "entities": out}
