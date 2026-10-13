"""入口：python3 -m fluxmon.main [--no-notify] [--skip-positions]"""
import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

from . import chain
from .alerts import evaluate, message_zh
from . import lb
from .analyze import analyze_pool, lp_concentration, lp_from_holders, lp_holders, orient
from .chain import BLOCKS_PER_DAY, decode_string, multicall
from .discover import sweep_pools, sync_events, sync_pools, sync_positions
from .sources import eth_usd, wrapper_ratios, xstock_reference, xstocks_catalog
from .store import Store
from .v3 import read_pool

ROOT = Path(__file__).resolve().parent.parent


def clean(o):
    """JSON 不支持 inf/nan，统一转成 null。"""
    if isinstance(o, float):
        return o if o == o and o not in (float("inf"), float("-inf")) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


def token_meta(store, addrs):
    meta = store.get("token_meta", {})
    todo = [a for a in addrs if a not in meta]
    if todo:
        r = multicall([(a, "0x95d89b41") for a in todo] + [(a, "0x313ce567") for a in todo])
        for i, a in enumerate(todo):
            meta[a] = {"symbol": decode_string(r[i]) if r[i] else a[:8], "decimals": int(r[len(todo) + i], 16)}
        store.put("token_meta", meta)
    return meta


def load_config():
    """config.json + 私有覆盖（config/private.json 或环境变量 FLUXMON_PRIVATE_CONFIG）。

    私有部分放 LP 标签、合同承诺金额等不宜进公开仓库的信息。"""
    cfg = json.loads((ROOT / "config" / "config.json").read_text())
    priv = ROOT / "config" / "private.json"
    extra = json.loads(priv.read_text()) if priv.exists() else {}
    if os.environ.get("FLUXMON_PRIVATE_CONFIG"):
        extra = json.loads(os.environ["FLUXMON_PRIVATE_CONFIG"])
    for k, v in extra.items():
        cfg[k] = {**cfg.get(k, {}), **v} if isinstance(v, dict) and isinstance(cfg.get(k), dict) else v
    return cfg


def designated_summary(rows, cfg, labels, xcat):
    """SOW 约定的指定交易对：合计 TVL、按 LP 地址汇总的资金、承诺资金方是否达标。"""
    d = cfg.get("designated")
    if not d:
        return None
    want = set(d["pairs"])
    owners = {o.lower() for o in d.get("committed_owners", [])}
    pools = [r for r in rows if r["dex"] == d.get("dex", "fluxion") and r["base_addr"] in xcat
             and xcat[r["base_addr"]]["symbol"] in want and r["quote"] == "USDC"]
    # 同一交易对若有多个 USDC 池（不同费率 / 包装币版本），全部计入
    holders, per_pool = {}, []
    for r in sorted(pools, key=lambda r: -(r["tvl_usd"] or 0)):
        hs = (r.get("lp") or {}).get("holders", [])
        committed = sum(h["usd"] for h in hs if h["owner"] in owners)
        per_pool.append({"pool": r["pool"], "pair": r["pair"], "symbol": xcat[r["base_addr"]]["symbol"], "tvl_usd": r["tvl_usd"],
                         "committed_usd": committed, "other_usd": sum(h["usd"] for h in hs) - committed,
                         "other_lps": sum(1 for h in hs if h["owner"] not in owners)})
        for h in hs:
            agg = holders.setdefault(h["owner"], {"owner": h["owner"], "label": labels.get(h["owner"]), "usd": 0.0, "pools": 0, "positions": 0})
            agg["usd"] += h["usd"]; agg["pools"] += 1; agg["positions"] += h["positions"]
    tvl = sum(p["tvl_usd"] or 0 for p in per_pool)
    hl = sorted(holders.values(), key=lambda h: -h["usd"])
    for h in hl:
        h["share_pct"] = h["usd"] / tvl * 100 if tvl else None
    committed = sum(h["usd"] for h in hl if h["owner"] in owners)
    covered = sorted({p["symbol"] for p in per_pool if p["committed_usd"] > 0})
    return {"pairs": d["pairs"], "commitment_usd": d.get("commitment_usd"), "committed_owners": sorted(owners),
            "committed_label": d.get("committed_label"), "tvl_usd": tvl, "committed_usd": committed,
            "unattributed_usd": tvl - sum(h["usd"] for h in hl), "missing_pairs": sorted(want - set(covered)) if owners else [],
            "holders": hl, "pools": per_pool}


def designated_alerts(ds, cfg):
    if not ds:
        return []
    out, who = [], ds.get("committed_label") or "committed LP"
    if ds["commitment_usd"] and ds["committed_usd"] < ds["commitment_usd"]:
        out.append(("designated:commitment", "critical", "commitment",
                    {"name": who, "usd": ds["committed_usd"], "min": ds["commitment_usd"]}))
    if ds["missing_pairs"]:
        out.append(("designated:missing_pairs", "critical", "missing_pairs",
                    {"name": who, "pairs": ds["missing_pairs"]}))
    return out


def notify(cfg, lines):
    hook = os.environ.get(cfg["lark_webhook_env"])
    if not hook or not lines:
        return
    body = json.dumps({"msg_type": "text", "content": {"text": "【Mantle DEX 流动性监控】\n" + "\n".join(lines)}}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(hook, body, {"content-type": "application/json"}), timeout=15)
    except Exception as e:
        print("lark webhook failed:", e)


def run(args):
    cfg = load_config()
    chain.RPC_URL = cfg["rpc_url"]
    store = Store(ROOT / "data" / "fluxmon.db")
    now = int(time.time())
    hist_url = os.environ.get("FLUXMON_HISTORY_URL")
    if hist_url:   # CI：从线上看板续接历史
        try:
            hist = json.load(urllib.request.urlopen(urllib.request.Request(hist_url, headers={"user-agent": "fluxmon"}), timeout=30))
            print("imported history rows:", store.import_history(hist))
        except Exception as e:
            print("history import skipped:", e)
    head = chain.block_number()

    xcat = xstocks_catalog()
    weth = cfg["weth"]
    dexes = {d["id"]: d for d in cfg["dexes"]}
    seed = ROOT / "config" / "pools_seed.json"
    if store.get("pools_synced_block") is None and seed.exists():   # 全新环境：用种子清单跳过 Fluxion 从部署区块开始的全量扫描
        sd = json.loads(seed.read_text())
        store.add_pools(sd["pools"])
        store.put("pools_synced_block", sd["synced_block"])
    for d in dexes.values():
        if d["type"] == "lb":
            lb.sync_pairs(store, d)
        elif d["discovery"] == "events":
            sync_pools(store, d, head)
        else:
            quotes = sorted(set(cfg["stables"]) | {weth} | {a.lower() for a in d.get("quotes", [])})
            sweep_pools(store, d, sorted(xcat), quotes, now)
    # 只看两侧都是可信资产（稳定币 / WETH / xStocks / 配置白名单）的池子，排除测试币池；每个 DEX 只看它配置的资产类别
    trusted = set(cfg["stables"]) | {weth} | set(xcat) | {a.lower() for a in cfg.get("extra_tokens", {})}

    def wanted(p):
        assets = dexes.get(p["dex"], {}).get("assets", [])
        hit = ("xstock" in assets and (p["token0"] in xcat or p["token1"] in xcat)) or \
              ("weth" in assets and weth in (p["token0"], p["token1"]))
        return hit and p["token0"] in trusted and p["token1"] in trusted
    pools = [p for p in store.pools() if p["dex"] in dexes and wanted(p)]
    meta = token_meta(store, sorted({t for p in pools for t in (p["token0"], p["token1"])}))
    states = {p["pool"]: (lb.read_pair(p) if p["kind"] == "lb" else read_pool(p)) for p in pools}

    # ---- 美元定价：稳定币=1，WETH=CEX，其余代币用与稳定币/WETH 配对、储备最大的池子推导
    usd = {a: 1.0 for a in cfg["stables"]}
    eth = eth_usd()
    usd[weth] = eth["price"] if eth else None
    for p in sorted(pools, key=lambda p: -(states[p["pool"]]["bal0"] + states[p["pool"]]["bal1"])):
        st = states[p["pool"]]
        for a, b in ((p["token0"], p["token1"]), (p["token1"], p["token0"])):
            if a not in usd and usd.get(b) and st["L"] > 0:
                m0, m1 = meta[p["token0"]], meta[p["token1"]]
                p01 = st["sqrtP"] ** 2 * 10 ** m0["decimals"] / 10 ** m1["decimals"]
                usd[a] = usd[b] * (p01 if a == p["token0"] else 1 / p01)
    usd = {k: v for k, v in usd.items() if v}

    rows = [analyze_pool(p, states[p["pool"]], meta, usd, cfg, xcat) for p in pools]
    rows = [r for r in rows if (r["tvl_usd"] or 0) >= cfg["min_tvl_usd"]]
    for r in rows:
        r["dex_name"] = dexes[r["dex"]]["name"]
    by_pool = {p["pool"]: p for p in pools}

    # ---- 参考价与偏离
    stable_quoted = lambda r: by_pool[r["pool"]]["token0"] in cfg["stables"] or by_pool[r["pool"]]["token1"] in cfg["stables"]
    if eth:
        for r in rows:
            if r["base_addr"] == weth and stable_quoted(r):
                r.update(ref_price=eth["price"], ref_source="cex", price_dev_pct=(r["price"] / eth["price"] - 1) * 100)
    xrows = [r for r in rows if r["base_addr"] in xcat and stable_quoted(r)]
    ratios = wrapper_ratios([r["base_addr"] for r in xrows if xcat[r["base_addr"]]["kind"] == "wrapper"])
    refs = {}
    for r in xrows:
        info = xcat[r["base_addr"]]
        if info["symbol"] not in refs:   # 同一个 xStock 在多个 DEX 上只查一次
            refs[info["symbol"]] = xstock_reference(info["symbol"], info["native"], cfg["usdc"], cfg["rfq_size_usdc"])
            time.sleep(1.1)   # RFQ 限速 60 次/分钟
        ref = refs[info["symbol"]]
        ratio = ratios.get(r["base_addr"]) or 1.0
        r["xstock"] = {**ref, "wrapper_ratio": ratio}
        fresh = ref["source"] == "rfq" or (ref["kline_age_s"] is not None and ref["kline_age_s"] <= cfg["alerts"]["xstock_ref_max_age_s"])
        if ref["price"]:
            ref_wrapped = ref["price"] * ratio      # 换算成 1 份包装币的参考价
            r.update(ref_price=ref_wrapped, ref_source=ref["source"] + ("" if fresh else "(stale)"),
                     price_dev_pct=(r["price"] / ref_wrapped - 1) * 100 if fresh else None,
                     price_dev_pct_raw=(r["price"] / ref_wrapped - 1) * 100)

    # ---- 事件：交易量、Mint/Burn
    def usd_of(p, a0, a1, kind):
        u0, u1 = usd.get(p["token0"]), usd.get(p["token1"])
        m0, m1 = meta[p["token0"]], meta[p["token1"]]
        v0 = abs(a0) / 10 ** m0["decimals"] * u0 if u0 else None
        v1 = abs(a1) / 10 ** m1["decimals"] * u1 if u1 else None
        if kind == "swap":   # 取计价侧（quote）的金额作为名义成交额
            q_is1 = orient(p, meta, cfg, xcat)
            return (v1 if q_is1 else v0) or v0 or v1 or 0
        return (v0 or 0) + (v1 or 0)
    sync_events(store, [by_pool[r["pool"]] for r in rows], head, cfg["event_backfill_days"], usd_of)
    for r in rows:
        for label, days in (("events_24h", 1), ("events_7d", 7)):
            ev = {}
            for kind, n, total, mx in store.event_stats(r["pool"], head - days * BLOCKS_PER_DAY):
                ev[f"{kind}_n"], ev[f"{kind}_usd"], ev[f"{kind}_max_usd"] = n, total, mx
            r[label] = ev
        if r["tvl_usd"]:
            r["turnover_24h_pct"] = r["events_24h"].get("swap_usd", 0) / r["tvl_usd"] * 100
            r["fee_apr_7d_pct"] = r["events_7d"].get("swap_usd", 0) / 7 * 365 * r["fee_pct"] / 100 / r["tvl_usd"] * 100

    # ---- LP 集中度
    labels = {k.lower(): v for k, v in cfg.get("lp_labels", {}).items()}
    if not args.skip_positions:
        for d in dexes.values():
            drows = [r for r in rows if r["dex"] == d["id"]]
            if not drows:
                continue
            if d["type"] == "lb":
                for r in drows:
                    p = by_pool[r["pool"]]
                    created = store.get(f"lb_created:{p['pool']}") or lb.first_block(p["pool"], head)
                    store.put(f"lb_created:{p['pool']}", created)
                    v = r.get("_val")
                    hs = [{"owner": o, "label": labels.get(o), "positions": 1,
                           "usd": x / v["d"][0] * v["u"][0] + y / v["d"][1] * v["u"][1]}
                          for o, (x, y) in lb.holders(store, p, states[p["pool"]], head, created - 1).items()] if v else []
                    hs.sort(key=lambda h: -h["usd"])
                    for h in hs:
                        h["share_pct"] = h["usd"] / r["tvl_usd"] * 100 if r["tvl_usd"] else None
                    r["lp"] = lp_from_holders(hs)
            elif d.get("npm") and d["discovery"] == "events":   # NPM 全量扫描只适合持仓 NFT 不多的 DEX
                keys = {(by_pool[r["pool"]]["token0"], by_pool[r["pool"]]["token1"], by_pool[r["pool"]]["fee"]): r["pool"] for r in drows}
                sync_positions(store, d["npm"], keys)
                for r in drows:
                    p = by_pool[r["pool"]]
                    pos = store.positions(r["pool"])
                    r["lp"] = lp_concentration(pos, r["tick"], orient(p, meta, cfg, xcat))
                    if r.get("_val"):
                        r["lp"]["holders"] = lp_holders(pos, r["_val"], r["tvl_usd"], labels)

    # ---- SOW 指定池子：TVL 与 LP 构成
    designated = designated_summary(rows, cfg, labels, xcat)

    # ---- 告警（去重：仅新触发 / 恢复时推送）
    a = cfg["alerts"]
    fired = {}
    for r in rows:
        prev = store.previous_snapshot(r["pool"], now, a["depth_drop_lookback_s"])
        for key, sev, code, params in evaluate(r, prev, cfg):
            fired[key] = (sev, message_zh(code, params), code, params)
    for key, sev, code, params in designated_alerts(designated, cfg):
        fired[key] = (sev, message_zh(code, params), code, params)
    active = store.active_alerts()
    new_lines, resolved_lines = [], []
    for key, (sev, msg, _, _) in fired.items():
        if key not in active:
            new_lines.append(f"[{sev.upper()}] {msg}")
        store.set_alert(key, sev, msg, now)
    for key, row in active.items():
        if key not in fired:
            store.resolve_alert(key, now)
            resolved_lines.append(f"[已恢复] {row[2]}")
    for r in rows:
        r.pop("_val", None)
    store.save_snapshot(now, head, rows)

    report = {"ts": now, "block": head, "eth_usd": eth, "pools": rows, "designated": designated, "lp_labels": labels,
              "dexes": [{"id": d["id"], "name": d["name"], "type": d["type"],
                         "pools_tracked": sum(1 for p in pools if p["dex"] == d["id"]),
                         "pools_shown": sum(1 for r in rows if r["dex"] == d["id"])} for d in dexes.values()],
              "alerts": [{"key": k, "severity": s, "message": m, "code": c, "params": p} for k, (s, m, c, p) in fired.items()]}
    out_dir = ROOT / "data" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latest.json").write_text(json.dumps(clean(report), indent=1, ensure_ascii=False))
    (out_dir / "latest.md").write_text(render_md(report))
    (out_dir / "history.json").write_text(json.dumps(clean(store.export_history()), separators=(",", ":")))
    print(render_console(report))
    if new_lines or resolved_lines:
        print("\n".join(["", "== 告警变化 =="] + new_lines + resolved_lines))
        if not args.no_notify:
            notify(cfg, new_lines + resolved_lines)


def _f(v, fmt="{:,.0f}", none="-"):
    return none if v is None else fmt.format(v)


def render_console(rep):
    lines = [f"Mantle DEX 流动性快照  block {rep['block']}  ETH(CEX) ${_f((rep['eth_usd'] or {}).get('price'), '{:,.2f}')}",
             f"{'DEX':<14}{'池子':<22}{'费率':>6}{'TVL':>13}{'±2%上':>10}{'±2%下':>10}{'±10%内占比':>11}{'$10k买冲击':>11}{'24h量':>11}{'偏离':>8}{'Top1 LP':>9}"]
    for r in sorted(rep["pools"], key=lambda r: -(r["tvl_usd"] or 0)):
        d2 = r["depth"].get("2", {})
        imp = r["impact"].get("10000", {}).get("buy")
        lines.append(f"{r['dex_name']:<14}{r['pair']:<22}{r['fee_pct']:>5}%{_f(r['tvl_usd'], '${:,.0f}'):>13}{_f(d2.get('up'), '${:,.0f}'):>10}"
                     f"{_f(d2.get('down'), '${:,.0f}'):>10}{_f(r.get('in_band_10_pct'), '{:.1f}%'):>11}{_f(imp, '{:.2f}%', '不足'):>11}"
                     f"{_f(r['events_24h'].get('swap_usd', 0), '${:,.0f}'):>11}{_f(r.get('price_dev_pct'), '{:+.2f}%'):>8}"
                     f"{_f((r.get('lp') or {}).get('top1_pct'), '{:.0f}%'):>9}")
    return "\n".join(lines)


def render_md(rep):
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(rep["ts"]))
    md = [f"# Mantle DEX 流动性快照（{ts}）", "",
          f"区块 {rep['block']}，ETH 参考价（CEX）${_f((rep['eth_usd'] or {}).get('price'), '{:,.2f}')}", "",
          "| DEX | 池子 | 费率 | TVL | ±2% 上 | ±2% 下 | ±10% 内占 TVL | $10k 买入冲击 | 24h 成交 | 7d 费率 APR | 偏离参考价 | Top1 LP |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rep["pools"], key=lambda r: -(r["tvl_usd"] or 0)):
        d2 = r["depth"].get("2", {})
        md.append("| " + " | ".join([
            r["dex_name"], r["pair"], f"{r['fee_pct']}%", _f(r["tvl_usd"], "${:,.0f}"), _f(d2.get("up"), "${:,.0f}"), _f(d2.get("down"), "${:,.0f}"),
            _f(r.get("in_band_10_pct"), "{:.1f}%"), _f(r["impact"].get("10000", {}).get("buy"), "{:.2f}%", "流动性不足"),
            _f(r["events_24h"].get("swap_usd", 0), "${:,.0f}"), _f(r.get("fee_apr_7d_pct"), "{:.2f}%"),
            _f(r.get("price_dev_pct", r.get("price_dev_pct_raw")), "{:+.2f}%") + ("" if r.get("price_dev_pct") is not None or r.get("price_dev_pct_raw") is None else "（参考价过期）"),
            _f((r.get("lp") or {}).get("top1_pct"), "{:.1f}%")]) + " |")
    md += ["", "## 当前告警", ""] + [f"- **{a['severity']}** {a['message']}" for a in rep["alerts"]]
    return "\n".join(md) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Fluxion AMM 流动性质量监控（xStocks / WETH）")
    ap.add_argument("--no-notify", action="store_true", help="不推送飞书 webhook")
    ap.add_argument("--skip-positions", action="store_true", help="跳过 LP 持仓同步（首次全量较慢）")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
