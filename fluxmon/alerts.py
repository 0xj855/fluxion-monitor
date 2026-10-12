"""告警规则。每条告警有稳定的 key，用于去重：只在新触发 / 恢复时推送。

告警以 (key, severity, code, params) 结构输出，文案由 code + params 渲染：
中文文案在这里（飞书推送、latest.md），看板前端另有中英文模板。
"""


def evaluate(r, prev, cfg):
    """r: 当前池子指标；prev: lookback 之前的快照（可能为 None）。返回 [(key, severity, code, params)]。"""
    a = cfg["alerts"]
    g = r["group"]
    base = {"pool": r["pool"], "name": f"{r['pair']} ({r['fee_pct']}%)"}
    out = []
    add = lambda key, sev, code, **p: out.append((f"{r['pool']}:{key}", sev, code, {**base, **p}))
    d2 = r.get("depth", {}).get("2")
    if d2:
        if min(d2["up"], d2["down"]) < a["depth2_min_usd"][g]:
            add("depth_min", "warn", "depth_min", up=d2["up"], down=d2["down"], min=a["depth2_min_usd"][g])
        if prev and prev.get("depth", {}).get("2"):
            p2 = prev["depth"]["2"]
            for side in ("up", "down"):
                if p2.get(side) and d2[side] < p2[side] * (1 - a["depth2_drop_pct"] / 100):
                    add(f"depth_drop_{side}", "critical", "depth_drop", side=side,
                        drop=1 - d2[side] / p2[side], before=p2[side], after=d2[side])
    if prev and prev.get("tvl_usd") and r.get("tvl_usd") is not None:
        if r["tvl_usd"] < prev["tvl_usd"] * (1 - a["tvl_drop_pct"] / 100):
            add("tvl_drop", "critical", "tvl_drop", drop=1 - r["tvl_usd"] / prev["tvl_usd"],
                before=prev["tvl_usd"], after=r["tvl_usd"])
    dev = r.get("price_dev_pct")
    if dev is not None and abs(dev) > a["price_dev_pct"][g]:
        add("price_dev", "warn" if abs(dev) < 3 * a["price_dev_pct"][g] else "critical", "price_dev",
            dev=dev, price=r["price"], ref=r["ref_price"], source=r["ref_source"])
    lp = r.get("lp") or {}
    if lp.get("top1_pct", 0) > a["top1_lp_share_pct"]:
        add("lp_conc", "info", "lp_conc", share=lp["top1_pct"], owner=lp["top_owner"])
    for b in lp.get("big_ranges", []):
        if b["full_range"] or b["share_pct"] < 20:
            continue
        edge = min(b["base_up_to_edge_pct"], b["base_down_to_edge_pct"])
        if edge < a["range_edge_warn_pct"]:
            side = "upper" if b["base_up_to_edge_pct"] < b["base_down_to_edge_pct"] else "lower"
            add(f"range_edge:{b['owner']}", "critical", "range_edge", share=b["share_pct"], side=side, edge=edge, owner=b["owner"])
    ev = r.get("events_24h", {})
    if r.get("tvl_usd") and ev.get("burn_max_usd", 0) > r["tvl_usd"] * a["large_burn_tvl_pct"] / 100:
        add("large_burn", "critical", "large_burn", usd=ev["burn_max_usd"], pct=a["large_burn_tvl_pct"])
    return out


def message_zh(code, p):
    n = p["name"]
    if code == "depth_min":
        return f"{n} ±2% 深度偏薄：上 ${p['up']:,.0f} / 下 ${p['down']:,.0f}（阈值 ${p['min']:,}）"
    if code == "depth_drop":
        return f"{n} {'上方' if p['side'] == 'up' else '下方'} ±2% 深度骤降 {p['drop']:.0%}：${p['before']:,.0f} → ${p['after']:,.0f}"
    if code == "tvl_drop":
        return f"{n} TVL 下降 {p['drop']:.0%}：${p['before']:,.0f} → ${p['after']:,.0f}"
    if code == "price_dev":
        return f"{n} 池价偏离参考价 {p['dev']:+.2f}%（池 {p['price']:.4f} vs 参考 {p['ref']:.4f}，来源 {p['source']}）"
    if code == "lp_conc":
        return f"{n} 单一 LP 占在区间流动性 {p['share']:.1f}%（{p['owner']}），撤资即失去深度"
    if code == "range_edge":
        return f"{n} 占 {p['share']:.0f}% 的 LP 头寸距区间{'上沿' if p['side'] == 'upper' else '下沿'}仅 {p['edge']:.1f}%，价格穿出后深度将大幅下降"
    if code == "large_burn":
        return f"{n} 24h 内出现大额撤出 ${p['usd']:,.0f}（≥ TVL 的 {p['pct']}%）"
    if code == "commitment":
        return f"{n} 在 SOW 指定池子中的资金 ${p['usd']:,.0f}，低于合同约定的 ${p['min']:,.0f}"
    if code == "missing_pairs":
        return f"{n} 未在以下指定交易对提供流动性：{', '.join(p['pairs'])}"
    return f"{n} {code}"
