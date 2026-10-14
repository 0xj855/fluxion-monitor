# Mantle DEX 流动性监控（fluxion-monitor）

监控 Mantle 上 **Fluxion V3**（xStocks + WETH）、**Merchant Moe Liquidity Book**（xStocks）、**Agni V3**（xStocks）池子的流动性质量。**零第三方依赖**，Python 3.11+ 直接运行，数据全部来自链上（公共 RPC）+ Fluxion RFQ / K 线 + CEX 价格。

```bash
cd fluxion-monitor
python3 -m fluxmon.main --no-notify        # 跑一次，只打印不推送
FLUXMON_LARK_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx ./run.sh   # 有新告警时推送到飞书群
```

单次运行约 1–4 分钟（主要耗时在公共 RPC 限速和 RFQ 每秒 1 次的限速）。首次运行会回填 7 天事件并全量扫描 LP 持仓 NFT。

## 覆盖的 DEX（`config/config.json → dexes`）

| DEX | 类型 | 池子发现 | LP 构成 |
|---|---|---|---|
| Fluxion | Uniswap V3 | Factory `PoolCreated` 增量扫描 | NonfungiblePositionManager 全量扫描 |
| Merchant Moe | Liquidity Book | LBFactory 按索引枚举 | `DepositedToBins` / `TransferBatch` 接收方 × 每个 bin 的份额 |
| Agni | V3（Pancake 风格 Swap 事件） | 每 24h 用 `getPool` 穷举 xStocks × 计价币 × 费率档 | 暂不统计（持仓 NFT 30 万+） |

## 每轮做什么

1. **发现池子**：增量扫描 Factory `PoolCreated`，筛出含 WETH 或 xStocks（native / ERC-4626 包装币）的池子；两侧都必须是可信资产（稳定币 / WETH / xStocks / `extra_tokens`），测试币池被排除。
2. **读池子状态**：Multicall3 一次性读 `slot0`、`liquidity`、全部 `tickBitmap` 与已初始化 tick 的 `liquidityNet`。
3. **计算指标**（美元口径，见下表）。
4. **RFQ 深度探测**：对主流 xStocks（SOW 10 个指定标的）向 Fluxion RFQ 接口探测双向做市报价、买卖价差（Spread）及多档买卖深度（$1k / $10k / $50k / $100k），并与链上 AMM 冲击横向对比。
5. **跨链比价**：对上述标的同时向 Solana 上的 Jupiter 聚合器询价（$10k / $100k 买入、$10k 卖出），并取标的美股最新成交价（Yahoo，含盘前盘后），计算两边相对美股的成本和 Fluxion 相对 Jupiter 的优势；Solana 价格按 Token-2022 Scaled UI multiplier（股息再投资）换算成每股。逐轮写入历史，看板按交易时段（盘中 / 盘前盘后 / 周末）汇总报价在线率与优势中位数。`--skip-venue` 可跳过。
6. **同步事件**：Swap / Mint / Burn 增量入库 → 24h / 7d 成交、费率 APR、大额撤出。
7. **LP 集中度**：扫描 NonfungiblePositionManager 的持仓 NFT → 每个池子在区间内流动性的持有人分布、主要头寸距区间边界的距离。
8. **告警**：按 `config/config.json` 阈值判定，按 key 去重，只在新触发 / 恢复时推送。

## 指标口径

| 指标 | 口径 |
|---|---|
| ±x% 深度 | 订单簿口径：base 价格移动 x% 过程中被吃掉的储备价值（上 = 卖盘，下 = 买盘），不含手续费 |
| ±10% 内占 TVL | ±10% 两侧深度之和 / TVL。全区间（v2 式）约 4.9%，越高说明流动性越集中在现价附近 |
| $10k 买入冲击 | 精确模拟 exact-input swap（逐 tick），按中间价计的价值损耗，**含 0.3% 手续费**；已与 Fluxion QuoterV2 交叉验证 |
| RFQ 价格深度与滑点 | 探测 RFQ 在 $1k / $10k / $50k / $100k 规模下的买入/卖出报价、买卖双向价差（bps）及承接滑点 |
| 偏离参考价 | WETH：对 Binance/Coinbase 均价；xStocks：对 Fluxion RFQ `rawPrice`（无报价时用 1 分钟 K 线收盘价，超过 15 分钟视为过期不告警），并按包装币 `convertToAssets` 换算 |
| Top1 LP | 当前在区间内的流动性中，单一地址的占比 |
| 跨链比价成本 | 买入 = 成交价高于美股价的 bps，卖出 = 成交价低于美股价的 bps；优势 = Fluxion 比 Jupiter 省多少 bps（正 = Fluxion 更优，不依赖美股参考价）。Yahoo 不可用时只保留优势列 |

## 告警规则（`config/config.json → alerts`）

| 规则 | 默认 | 级别 |
|---|---|---|
| ±2% 深度低于下限 | WETH $50k / xStocks $2k | warn |
| ±2% 深度较 1 小时前下降 | > 30% | critical |
| TVL 较 1 小时前下降 | > 20% | critical |
| 池价偏离参考价 | WETH 1% / xStocks 1.5%（3 倍以上为 critical） | warn |
| 单一 LP 占比 | > 50% | info |
| 占比 ≥ 20% 的集中头寸距区间边界 | < 3% | critical |
| 24h 单笔 Burn | ≥ TVL 的 10% | critical |

## 产物

| 文件 | 说明 |
|---|---|
| `data/reports/latest.md` | 最新一期快照表 + 当前告警 |
| `data/reports/latest.json` | 全量指标（含各档深度、多档冲击、LP 头寸区间、参考价明细） |
| `data/fluxmon.db` | SQLite：池子清单、事件、持仓缓存、快照历史、告警状态 |

## 关键合约（Mantle, chainId 5000）

| 名称 | 地址 |
|---|---|
| Fluxion V3 Factory | `0xF883162Ed9c7E8EF604214c964c678E40c9B737C` |
| NonfungiblePositionManager | `0x2b70c4e7ca8e920435a5db191e066e9e3afd8db3` |
| QuoterV2 | `0x3E4eE18Ac7280813236a1EB850679Da5322E14CE` |
| Router | `0x5628a59dF0ECAC3f3171f877A94bEb26BA6DFAa0` |

xStocks 地址清单来自 `https://api.backed.fi/api/v1/token`（Mantle deployment 的 native 与 wrapper 地址）。Fluxion 上 xStocks 的 AMM 池是 **ERC-4626 包装币 / USDC**，native xStock 主要走 RFQ。

## 在线看板（GitHub Pages）

`.github/workflows/monitor.yml` 每 5 分钟在 GitHub Actions 上运行一次监控，并把 `site/index.html` 与最新数据发布到 GitHub Pages：

- 看板支持中文 / English（右上角切换，或 `?lang=en`）。
- 看板读取 `data/latest.json`（当前快照，含 ±30% 流动性分布、累积深度曲线、LP 头寸）和 `data/history.json`（30 天时间序列）。
- SQLite 状态通过 Actions cache 续接；缓存丢失时用 `config/pools_seed.json` 跳过池子全量扫描，并从线上 `history.json` 续接历史。
- 飞书推送：在仓库 Settings → Secrets → Actions 添加 `FLUXMON_LARK_WEBHOOK`（自定义机器人 webhook 地址）。

本地预览：`./run.sh --no-notify && mkdir -p _site/data && cp site/index.html _site/ && cp data/reports/*.json _site/data/ && python3 -m http.server -d _site 8000`
