"""池子发现（Factory PoolCreated 增量扫描）、事件增量同步、NonfungiblePositionManager 持仓同步。"""
from .chain import BLOCKS_PER_DAY, block_number, get_logs, multicall, sint, words

T_POOL_CREATED = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
T_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
T_MINT = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
T_BURN = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"


def sync_pools(store, cfg, head):
    start = store.get("pools_synced_block", cfg["factory_deploy_block"] - 1) + 1
    logs = get_logs(cfg["factory"], [T_POOL_CREATED], start, head)
    pools = []
    for l in logs:
        d = l["data"][2:]
        pools.append({"token0": "0x" + l["topics"][1][-40:], "token1": "0x" + l["topics"][2][-40:],
                      "fee": int(l["topics"][3], 16), "tickSpacing": sint(d[:64]),
                      "pool": "0x" + d[88:128], "block": int(l["blockNumber"], 16)})
    store.add_pools(pools)
    store.put("pools_synced_block", head)
    return pools


def sync_events(store, pools, head, backfill_days, usd_of):
    """增量拉取 Swap/Mint/Burn。usd_of(pool, amount0_raw, amount1_raw) -> 美元名义金额。"""
    by_addr = {p["pool"]: p for p in pools}
    last = store.get("events_synced", {})
    default_start = head - int(backfill_days * BLOCKS_PER_DAY)
    # 新加入的池子单独回填，其余池子从统一游标继续
    groups = {}
    for pa in by_addr:
        groups.setdefault(last.get(pa, default_start - 1) + 1, []).append(pa)
    rows = []
    for start, addrs in groups.items():
        for l in get_logs(addrs, [[T_SWAP, T_MINT, T_BURN]], start, head):
            pa = l["address"].lower()
            d = words(l["data"])
            k = l["topics"][0]
            if k == T_SWAP:
                kind, a0, a1, actor = "swap", sint(d[0]), sint(d[1]), "0x" + l["topics"][1][-40:]
            elif k == T_MINT:
                kind, a0, a1, actor = "mint", int(d[2], 16), int(d[3], 16), "0x" + l["topics"][1][-40:]
            else:
                kind, a0, a1, actor = "burn", int(d[1], 16), int(d[2], 16), "0x" + l["topics"][1][-40:]
            rows.append((pa, int(l["blockNumber"], 16), int(l["logIndex"], 16), kind, float(a0), float(a1),
                         usd_of(by_addr[pa], a0, a1, kind), actor))
    store.add_events(rows)
    store.put("events_synced", {pa: head for pa in by_addr})
    return rows


def sync_positions(store, npm, pool_keys):
    """pool_keys: {(token0, token1, fee): pool}。扫描新 tokenId，并刷新已知持仓的流动性与持有人。"""
    sel_pos, sel_owner = "0x99fbab88", "0x6352211e"
    known = {p["id"] for p in store.positions()}
    scanned = store.get("npm_scanned_id", 0)
    if store.get("npm_pool_count") != len(pool_keys):   # 监控池集合变了，全量重扫一次
        scanned = 0

    def fetch(ids):
        pr = multicall([(npm, sel_pos + format(i, "064x")) for i in ids])
        ow = multicall([(npm, sel_owner + format(i, "064x")) for i in ids])
        rows, last_hit = [], None
        for i, r, o in zip(ids, pr, ow):
            if not r:
                continue
            last_hit = i
            d = words(r)
            key = ("0x" + d[2][-40:], "0x" + d[3][-40:], int(d[4], 16))
            if key in pool_keys:
                rows.append((i, pool_keys[key], "0x" + o[-40:] if o else None, sint(d[5]), sint(d[6]), str(int(d[7], 16))))
        return rows, last_hit

    # 1) 增量扫描新 id：连续一整批都不存在则认为到头（已销毁的 id 也会读不到，因此用较大的批次）
    batch, nxt = 600, scanned + 1
    while True:
        rows, last_hit = fetch(range(nxt, nxt + batch))
        store.upsert_positions(rows)
        if last_hit is None:
            break
        scanned = last_hit
        nxt += batch
    store.put("npm_scanned_id", scanned)
    store.put("npm_pool_count", len(pool_keys))
    # 2) 刷新已知持仓（流动性、owner 会变化；已 burn 的 NFT 读不到则把流动性置零）
    known_ids = sorted(known)
    for i in range(0, len(known_ids), 300):
        ids = known_ids[i:i + 300]
        rows, _ = fetch(ids)
        seen = {r[0] for r in rows}
        gone = [(pid,) for pid in ids if pid not in seen]
        store.upsert_positions(rows)
        if gone:
            store.db.executemany("UPDATE positions SET liquidity='0' WHERE id=?", gone)
            store.db.commit()
