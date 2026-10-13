"""池子发现（Factory PoolCreated 增量扫描）、事件增量同步、NonfungiblePositionManager 持仓同步。"""
from .chain import BLOCKS_PER_DAY, addr_word, get_logs, multicall, sint, word, words
from . import lb

T_POOL_CREATED = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
T_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
T_SWAP_PANCAKE = "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83"   # Agni：多两个 protocolFees 字段
T_MINT = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
T_BURN = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"


def sync_pools(store, dex, head):
    """V3 Factory 的 PoolCreated 增量扫描（适合池子不多、部署较晚的 DEX，例如 Fluxion）。"""
    key = "pools_synced_block" if dex["id"] == "fluxion" else f"pools_synced_block:{dex['id']}"
    start = store.get(key, dex["deploy_block"] - 1) + 1
    logs = get_logs(dex["factory"], [T_POOL_CREATED], start, head)
    pools = []
    for l in logs:
        d = l["data"][2:]
        pools.append({"token0": "0x" + l["topics"][1][-40:], "token1": "0x" + l["topics"][2][-40:],
                      "fee": int(l["topics"][3], 16), "tickSpacing": sint(d[:64]),
                      "pool": "0x" + d[88:128], "block": int(l["blockNumber"], 16), "dex": dex["id"], "kind": "v3"})
    store.add_pools(pools)
    store.put(key, head)
    return pools


def sweep_pools(store, dex, tokens, quotes, now):
    """V3 Factory 的 getPool 穷举（适合池子很多的老 DEX，例如 Agni）：只查 关注代币 × 计价币 × 费率档。

    每 sweep_hours 小时重扫一次，用来发现新上的 xStocks 池子。"""
    key = f"pools_swept:{dex['id']}"
    if now - store.get(key, 0) < dex.get("sweep_hours", 24) * 3600:
        return []
    calls, keys = [], []
    for t in tokens:
        for q in quotes:
            if t == q:
                continue
            a, b = sorted([t, q])
            for f in dex["fees"]:
                calls.append((dex["factory"], "0x1698ee82" + addr_word(a) + addr_word(b) + word(f)))
                keys.append((a, b, f))
    res = multicall(calls, chunk=400)
    found = [(k, "0x" + h[-40:]) for k, h in zip(keys, res) if h and int(h, 16)]
    spacing = {}
    if found:
        fees = sorted({k[2] for k, _ in found})
        spacing = dict(zip(fees, (sint(h) for h in multicall([(dex["factory"], "0x22afcccb" + word(f)) for f in fees]))))
    pools = [{"token0": a, "token1": b, "fee": f, "tickSpacing": spacing[f], "pool": p, "block": 0, "dex": dex["id"], "kind": "v3"}
             for (a, b, f), p in found]
    store.add_pools(pools)
    store.put(key, now)
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
        topics = [T_SWAP, T_SWAP_PANCAKE, T_MINT, T_BURN, lb.T_SWAP, lb.T_DEPOSIT, lb.T_WITHDRAW]
        for l in get_logs(addrs, [topics], start, head):
            pa = l["address"].lower()
            d = words(l["data"])
            k = l["topics"][0]
            if k in (T_SWAP, T_SWAP_PANCAKE):
                kind, a0, a1, actor = "swap", sint(d[0]), sint(d[1]), "0x" + l["topics"][1][-40:]
            elif k == T_MINT:
                kind, a0, a1, actor = "mint", int(d[2], 16), int(d[3], 16), "0x" + l["topics"][1][-40:]
            elif k == T_BURN:
                kind, a0, a1, actor = "burn", int(d[1], 16), int(d[2], 16), "0x" + l["topics"][1][-40:]
            elif k == lb.T_SWAP:   # data: id, amountsIn, amountsOut, ...（打包的 X/Y）
                (ix, iy), (ox, oy) = lb.unpack(d[1]), lb.unpack(d[2])
                kind, a0, a1, actor = "swap", ix + ox, iy + oy, "0x" + l["topics"][1][-40:]
            else:                  # DepositedToBins / WithdrawnFromBins: data = ids[], amounts[]
                amts = _dyn_array(l["data"], 1)
                a0, a1 = sum(lb.unpack(h)[0] for h in amts), sum(lb.unpack(h)[1] for h in amts)
                kind, actor = ("mint" if k == lb.T_DEPOSIT else "burn"), "0x" + l["topics"][2][-40:]
            rows.append((pa, int(l["blockNumber"], 16), int(l["logIndex"], 16), kind, float(a0), float(a1),
                         usd_of(by_addr[pa], a0, a1, kind), actor))
    store.add_events(rows)
    store.put("events_synced", {pa: head for pa in by_addr})
    return rows


def _dyn_array(data, idx):
    """取 ABI 编码 data 中第 idx 个动态数组的元素（hex 字符串列表）。"""
    h = data[2:]
    off = int(h[64 * idx:64 * idx + 64], 16) * 2
    n = int(h[off:off + 64], 16)
    return ["0x" + h[off + 64 + 64 * i:off + 128 + 64 * i] for i in range(n)]


def sync_positions(store, npm, pool_keys):
    """pool_keys: {(token0, token1, fee): pool}。扫描新 tokenId，并刷新已知持仓的流动性与持有人。"""
    sel_pos, sel_owner = "0x99fbab88", "0x6352211e"
    known = {p["id"] for p in store.positions(npm=npm)}
    scanned = store.get(f"npm_scanned_id:{npm}", 0)
    if store.get(f"npm_pool_count:{npm}") != len(pool_keys):   # 监控池集合变了，全量重扫一次
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
                rows.append((npm, i, pool_keys[key], "0x" + o[-40:] if o else None, sint(d[5]), sint(d[6]), str(int(d[7], 16))))
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
    store.put(f"npm_scanned_id:{npm}", scanned)
    store.put(f"npm_pool_count:{npm}", len(pool_keys))
    # 2) 刷新已知持仓（流动性、owner 会变化；已 burn 的 NFT 读不到则把流动性置零）
    known_ids = sorted(known)
    for i in range(0, len(known_ids), 300):
        ids = known_ids[i:i + 300]
        rows, _ = fetch(ids)
        seen = {r[1] for r in rows}
        gone = [(npm, pid) for pid in ids if pid not in seen]
        store.upsert_positions(rows)
        if gone:
            store.db.executemany("UPDATE positions SET liquidity='0' WHERE npm=? AND id=?", gone)
            store.db.commit()
