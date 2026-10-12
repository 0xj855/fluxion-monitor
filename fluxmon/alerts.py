"""告警规则。每条告警有稳定的 key，用于去重：只在新触发 / 恢复时推送。"""


def evaluate(r, prev, cfg):
    """r: 当前池子指标；prev: lookback 之前的快照（可能为 None）。返回 [(key, severity, message)]。"""
    a = cfg["alerts"]
    g = r["group"]
    name = f"{r['pair']} ({r['fee_pct']}%)"
    out = []
    d2 = r.get("depth", {}).get("2")
    if d2:
        lo = min(d2["up"], d2["down"])
        if lo < a["depth2_min_usd"][g]:
            out.append((f"{r['pool']}:depth_min", "warn",
                        f"{name} ±2% 深度偏薄：上 ${d2['up']:,.0f} / 下 ${d2['down']:,.0f}（阈值 ${a['depth2_min_usd'][g]:,}）"))
        if prev and prev.get("depth", {}).get("2"):
            p2 = prev["depth"]["2"]
            for side, cn in (("up", "上方"), ("down", "下方")):
                if p2[side] > 0 and d2[side] < p2[side] * (1 - a["depth2_drop_pct"] / 100):
                    out.append((f"{r['pool']}:depth_drop_{side}", "critical",
                                f"{name} {cn} ±2% 深度骤降 {1 - d2[side] / p2[side]:.0%}：${p2[side]:,.0f} → ${d2[side]:,.0f}"))
    if prev and prev.get("tvl_usd") and r.get("tvl_usd") is not None:
        if r["tvl_usd"] < prev["tvl_usd"] * (1 - a["tvl_drop_pct"] / 100):
            out.append((f"{r['pool']}:tvl_drop", "critical",
                        f"{name} TVL 下降 {1 - r['tvl_usd'] / prev['tvl_usd']:.0%}：${prev['tvl_usd']:,.0f} → ${r['tvl_usd']:,.0f}"))
    dev = r.get("price_dev_pct")
    if dev is not None and abs(dev) > a["price_dev_pct"][g]:
        out.append((f"{r['pool']}:price_dev", "warn" if abs(dev) < 3 * a["price_dev_pct"][g] else "critical",
                    f"{name} 池价偏离参考价 {dev:+.2f}%（池 {r['price']:.4f} vs 参考 {r['ref_price']:.4f}，来源 {r['ref_source']}）"))
    lp = r.get("lp") or {}
    if lp.get("top1_pct", 0) > a["top1_lp_share_pct"]:
        out.append((f"{r['pool']}:lp_conc", "info",
                    f"{name} 单一 LP 占在区间流动性 {lp['top1_pct']:.1f}%（{lp['top_owner']}），撤资即失去深度"))
    for b in lp.get("big_ranges", []):
        if b["full_range"] or b["share_pct"] < 20:
            continue
        edge = min(b["base_up_to_edge_pct"], b["base_down_to_edge_pct"])
        if edge < a["range_edge_warn_pct"]:
            side = "上沿" if b["base_up_to_edge_pct"] < b["base_down_to_edge_pct"] else "下沿"
            out.append((f"{r['pool']}:range_edge:{b['owner']}", "critical",
                        f"{name} 占 {b['share_pct']:.0f}% 的 LP 头寸距区间{side}仅 {edge:.1f}%，价格穿出后深度将大幅下降"))
    ev = r.get("events_24h", {})
    if r.get("tvl_usd") and ev.get("burn_max_usd", 0) > r["tvl_usd"] * a["large_burn_tvl_pct"] / 100:
        out.append((f"{r['pool']}:large_burn", "critical",
                    f"{name} 24h 内出现大额撤出 ${ev['burn_max_usd']:,.0f}（≥ TVL 的 {a['large_burn_tvl_pct']}%）"))
    return out
