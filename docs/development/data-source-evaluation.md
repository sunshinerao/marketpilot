# MarketPilot 数据源评估：Databento vs Massive vs Cboe DataShop（含 Short-Squeeze 筛选数据）

> 调研完成于 2026-08-18，以各官网当前页面为准（正文附 URL）。所有价格均为美元/月，
> 面向个人非专业（non-professional）、非展示/内部研究用途。购买前需按第 4 节复核；
> 标注"未决"的条目只有试用账户或销售电话能定案。
>
> 评级口径：✅ 可用且已验证 / 🟡 部分可用或有重大限制 / ❌ 不可用。
>
> **背景**：Webull OpenAPI 已在 schema 级验证覆盖 SPXW 实时 NBBO/Greeks/分钟 bar
> （见 capability-probe.md），因此本评估的增量需求是：ES 期货、SPX 指数、
> VIX/VIX1D 指数、**已到期** SPXW 的分钟级 NBBO 历史，以及 short-squeeze 筛选数据。

---

## 1. 需求矩阵（5 需求 × 来源）

| 需求 | Databento | Massive (原 Polygon.io) | Cboe DataShop / Cboe 直接 |
|---|---|---|---|
| **1. ES 期货（实时+历史）** | ✅ GLBX.MDP3，CME/CBOT/NYMEX/COMEX 全场，历史自 2010；Standard $199/mo 含实时 | ✅ 2026 年 GA 的 Futures API（CME/CBOT/COMEX/NYMEX），Futures Advanced $199/mo 实时含 quotes，历史自 2017-04-03 | 🟡 DataMine 仅历史一次性购买；实时 MDP 直连对个人不现实（ILA 非展示费 ~$290–427/mo + 基础设施） |
| **2. SPX 指数（实时+历史）** | ❌ 无指数点位数据集（其资产类别仅 futures/options/equities；OPRA 页出现的 "SPX" 是期权标的） | 🟡 I:SPX 实时 ✅（source_feed = Cboe Global Indices Main，Indices Advanced $99/mo）；但**指数历史仅从 2023-03 起** | ✅ 权威来源：Cboe Global Indices Feed（MAIN 频道含 SPX，定制报价，机构向）；历史文件经 DataShop 销售；官网免费 EOD CSV |
| **3. VIX / VIX1D（实时+历史）** | 🟡 无 VIX 指数点位；但有 XCBF.PITCH（CFE，2026-04 上线）：VX/VXM 期货 + VIX 期货期权，历史自 2018-11，可作 VIX 衍生品代理，**不是 VIX 指数本身** | 🟡 VIX 在指数列表内（发布博客明确点名），实时 $99/mo；**VIX1D（I:VIX1D）未经账户验证**；历史同样只从 2023-03（VIX1D 本身 2023-04 才推出，故 VIX1D 影响小） | ✅ VIX/VIX1D 均为 Cboe 专有，CGI Feed MAIN 频道；DataShop 有历史文件与 VIX EOD 计算输入数据；官网免费 EOD 历史 CSV |
| **4. 已到期 SPXW 分钟 NBBO 历史** | ✅ OPRA.PILLAR：含 SPX/SPXW 全部已到期合约；**CBBO-1m（分钟 NBBO+last sale）已回填至 2013-04**；逐笔 NBBO（CMBP-1/TCBBO）仅从 2023-03-28；usage-based 历史 from $0.04/GB | 🟡 Options Advanced $199/mo 含 NBBO quotes flat files，但**仅从 2022-03-07 起**；分钟 aggregates 宣称 "5+ 年"；SPXW 合约覆盖需账户验证（其 KB 确认支持指数期权 I:SPX） | 🟡 DataShop "Option Quotes"/"Option Trades"（C1 交易所数据，含 SPX/SPXW），价格需登录/询价；非 OPRA 合并 NBBO，仅为 Cboe 本所 BBO |
| **5. Short-squeeze 筛选（short interest / borrow fee / float / days-to-cover）** | ❌ 完全没有：无 short interest、无 borrow、无基本面/股本数据 | 🟡 Short Interest（FINRA 双周，含 ADV 与 days-to-cover）+ Short Volume（每日，分 TRF/ATS 明细）已进 REST API，**含免费 Basic 档**；无 borrow fee、无 float | —（不适用；第 5 需求的专门来源为 FINRA / IBKR / Ortex / Fintel / S3，详见 2.4 节） |

---

## 2. 每个来源详评

### 2.1 Databento

**Coverage（已验证）**
- [GLBX.MDP3](https://databento.com/datasets/GLBX.MDP3)：CME Globex 全部期货（含 ES/MES），历史自 2010，usage-based 历史 from $0.50/GB。
- [OPRA.PILLAR](https://databento.com/catalog/opra/OPRA.PILLAR)：全美股期权合并行情，明确含 SPX/SPXW/VIX 期权（[OPRA 发布博客](https://databento.com/blog/opra-data)）。历史分层：
  - Trades / OHLCV / statistics / definitions / **CBBO-1m（分钟 NBBO）**：自 2013-04-01；
  - CMBP-1 / TCBBO / CBBO-1s（逐笔 NBBO）：自 2023-03-28。

  （来源：[OPRA improvements 博客，2025-05](https://databento.com/blog/opra-improvements-coming-soon)；[options 落地页](https://databento.com/options)标注 "Since 2013, Historical from $0.04/GB"）
- [XCBF.PITCH（CFE）](https://databento.com/blog/introducing-cboe-futures-exchange-cfe)：VX/VXM 期货等，历史自 2018-11-04，from $27/GB。**注意：这是 VIX 期货，不是 VIX 指数。**
- **无任何指数点位数据集**（SPX/VIX 指数均不可用）；无 short interest / 股本 / 基本面数据。

**Pricing（[pricing 页](https://databento.com/pricing) + 博客）**
- 历史数据 usage-based（按未压缩 GB 计费），新用户 [$125 免费额度](https://databento.com/pricing)，无订阅也可买历史。
- 实时必须订阅：GLBX.MDP3 Standard **$199/mo**（2026-06-22 起新价；老用户 grandfathered $179 十二个月，见[2026-05 定价更新](https://databento.com/blog/updates-to-subscription-pricing)），含实时 + "No license fees" + 全历史 L0（trades/BBO 级）；OPRA Standard **$199/mo**（2025-06-03 推出，见 OPRA improvements 博客）。Plus $1,750 / Unlimited $4,500 为机构档（年约、含再分发权）。
- 许可：自助问卷判定 non-pro 身份，交易所 license fee 转嫁无加价（[GLBX.MDP3 页](https://databento.com/datasets/GLBX.MDP3)）；外部再分发需 Plus 以上。个人内部研究/derived data 自用通常落在 Standard 范围，但**衍生数据对内共享边界建议在协议中确认**。

**PIT 保证**：业内最强。纳秒级硬件时间戳 + PTP 同步、最多 4 个时间戳/事件（ts_event/ts_recv 等）、raw capture 无损、"point-in-time instrument definitions and timestamping … to mitigate lookahead errors"（OPRA 发布博客原文）；历史即实时流的录制回放，不回填修订；有 gap 的交易日被明确标记 "Degraded"（2025-05 后双 A/B 路仲裁已大幅消除）。

**API**：Python/Rust/C++ 客户端 + Raw TCP/HTTP；同一 API 同时服务 live 与 historical replay；batch flat-file 下载；DBN/CSV/JSON。成熟度三家最高。

### 2.2 Massive（原 Polygon.io）

**Coverage（已验证）**
- **Futures（新，2026 GA）**：[发布博客](https://massive.com/blog/futures-data-has-arrived)确认 CME/CBOT/COMEX/NYMEX，含 ES。[Futures docs](https://massive.com/docs/rest/futures/overview)：trades+quotes+合约/产品/赛程参考数据，REST+WebSocket+Flat Files；quotes 历史自 2017-04-03，aggregates "7+ 年"。
- **Indices**：I:SPX 存在且 source_feed 为 **CboeGlobalIndicesMain**（[Indices 发布博客](https://massive.com/blog/indices-data-has-arrived)），11,400+ 指数含 S&P 500、DJI、NDX、**VIX**；实时 WebSocket 逐值推送。**历史仅从 2023-03 起**（官方明示在回填，进展未公开）。VIX1D 未验证。
- **Options**：KB 确认支持指数期权（[I:SPX 期权链示例](https://massive.com/knowledge-base/article/does-massive-support-options-data-for-index-contracts)）；NBBO quotes flat files **仅从 2022-03-07 起**（[flat files 文档](https://massive.com/docs/flat-files/options/quotes)，2022 年 19.8TB/208 个交易日），且 quotes 仅 Options Advanced 可用；分钟 aggregates 各档 2–5+ 年。
- **Short interest / Short volume**：见 2.4 节（第 5 需求）。

**Pricing（[pricing 页](https://massive.com/pricing)，个人档实测提取）**

| 计划 | 价格 | 关键点 |
|---|---|---|
| Indices Starter / Advanced | $49 / **$99** | Advanced = 实时（限 non-pro）；指数历史仅 1+ 年在线可查 |
| Options Starter / Developer / Advanced | $29 / $79 / **$199** | 只有 Advanced 有 quotes（实时 + 全部 NBBO 历史）；均限 non-pro |
| Futures Starter / Developer / Advanced | $29 / $79 / **$199** | Starter/Developer 为 10 分钟延迟；Advanced 实时；Developer 起有 trades |
| Stocks Starter / Developer / Advanced | $29 / $79 / **$199** | Short Interest / Short Volume 端点**所有档可用（含免费 Basic）** |
| 免费 Basic 档 | $0 | 各资产类均有，EOD/2 年历史，适合 sandbox 评估 API |

- 许可：个人档 "Individual use only"，Advanced 档要求 non-professional 资格声明；再分发/商用需 Business 档（futures 每交易所 $999/mo）。**未决**：Futures Advanced $199 是否已含 CME non-pro 转收费，定价页未明示。

**PIT 立场（明确弱于 Databento）**：[官方 KB](https://massive.com/knowledge-base/article/how-much-does-massives-feeds-handle-canceled-trades)：tick 级 trades 保留已取消交易并以 correction flag 标注（原始性尚可），但 **aggregates 每日收盘后会被重算修订** —— 即 bar 数据非 point-in-time。无对称的 "as-of" 保证文档；期权/期货参考数据（contracts endpoint）支持 PIT 查询。

**API**：Python/REST/WebSocket/S3 flat files，开发者体验好；文档每页可导出 markdown。

### 2.3 Cboe DataShop / Cboe 直接

**Coverage**
- [Cboe Global Indices Feed](https://www.cboe.com/us/indices/accessing-index-data/)：SPX、VIX（及 VIX1D）的**权威实时来源**，MAIN 频道；交付方式 = 直连（Secaucus/Chicago）、Cboe Global Cloud、或经 data vendors；价格 = "Get a custom pricing plan, Contact Us"，需签 North American Data Agreement——**机构产品，个人通常只能经 vendor（如 Massive、券商、TradingView）间接获取**。
- [DataShop](https://datashop.cboe.com/)：自助电商式历史数据，trending 产品含 "Option Quotes"、"Option Trades"、"Option EOD Summary"、"CFE Futures Trades"、[VIX Index EOD Calculation Inputs](https://datashop.cboe.com/vix-index-eod-calculation-inputs)。Option Quotes 为 **Cboe C1 本所** BBO（SPX/SPXW 主上市地，SPXW 几乎全部流动性在 C1，故本所 BBO ≈ NBBO 的偏差很小但存在）。**价格在登录后/询价才可见**，公开页面无标价。
- 免费层：cboe.com 官网提供 SPX/VIX/VIX1D 的**免费 EOD 历史 CSV** 与 15 分钟延迟报价——MVP 期校准 EOD 可直接用。

**PIT**：交易所官方记录，历史文件按录制时态发布，无修订政策问题；但 DataShop 不同产品的字段/延迟口径各异，需逐产品确认。

**CME 直接（对照项）**：实时 MDP 需签 ILA，个人开发者经 broker API 拉取实时 CME 数据的非展示许可实测 ~$290–427/mo（[第三方实测](https://blog.pickmytrade.io/cme-ila-fee-explained-why-it-costs-290-427-mo/)），直连还需基础设施与审计义务；[DataMine](https://www.cmegroup.com/market-data/browse-data/catalog/futures-and-options-data.html) 为历史一次性购买。**结论：对个人研究者不现实，Databento/Massive 的转嫁模式严格更优。**

### 2.4 Short-Squeeze 筛选数据源详评（第 5 需求）

#### (a) Short Interest（空头持仓）

- **FINRA 源数据（免费，权威但滞后）**：FINRA 双周 short interest 以结算日（每月月中/月末）为基准，发布滞后约 1–2 周；OTC 股票由 FINRA 汇总，上市股票由 NYSE/Nasdaq 分别发布。[FINRA Equity Short Interest 目录页](https://www.finra.org/finra-data/browse-catalog/equity-short-interest)。另有**每日** TRF short sale volume 文件（Reg SHO 日报，免费）。FINRA 同时提供 Developer Platform / Query API（免费注册，**仅限非商业用途**——我们 read-only 内部研究符合，但产品化展示需另行确认）。可脚本化批量下载，适合回测（PIT 性好：按公布日入库即可避免前视）。
- **Massive（已验证，API 化最省事）**：[Short Interest endpoint](https://massive.com/docs/rest/stocks/fundamentals/short-interest)（GET /stocks/v1/short-interest）——FINRA 双周数据，字段含**平均日成交量与 days-to-cover 比率**（见[官方教程博客](https://massive.com/blog/short-volume-short-interest-tutorial)）；[Short Volume endpoint](https://massive.com/docs/rest/stocks/fundamentals/short-volume)（GET /stocks/v1/short-volume）——每日 short volume、exempt volume、按 NYSE/Nasdaq Carteret/Nasdaq Chicago/FINRA ADF 分场所明细、short volume ratio。**两端点均包含在全部 Stocks 计划中**：Basic 免费档（2 年历史）、Starter $29 起全历史。本质是把 FINRA 免费数据做了 API 化与历史整理，不改变其固有的双周滞后。
- **Databento**：无任何 short interest / 股本 / 基本面数据，不适用。
- **"实时估算" SI（Ortex/S3）**：交易所官方 SI 永远滞后；Ortex 的 estimated SI 与 S3 Partners 的 SI 是基于证券借贷市场推算的准实时数据（见下）。

#### (b) Borrow rate / fee（借券费率）

| 来源 | 内容 | API/交付 | 个人成本 | 评价 |
|---|---|---|---|---|
| **IBKR Short Stock Availability** | 可借数量、fee rate、rebate rate，盘中多次更新 | FTP 文件（免费；历史上 ftp3.interactivebrokers.com 公开，**当前确切 URL/是否免登录需实测**）；账户持有人另可经 TWS API 取 shortable shares 与 SLB 费率 tick | $0（开户更佳） | 个人最实用的免费 borrow 数据；但只是 IBKR 一家的借贷池，非全市场；历史需自行每日落盘积累（官方不提供长历史） |
| **Ortex** | Cost-to-Borrow（new loans / 平均）、utilization、estimated SI、days-to-cover | 平台 + [API（docs.ortex.com）](https://docs.ortex.com/reference/ortex-apis) + Python SDK + Excel；[个人定价](https://public.ortex.com/ortex-pricing/) Basic $39–49/mo、Advanced $99–149/mo | $39–149/mo | **未决：个人档是否含 API 访问**（定价页宣传 "by API"，但历史上 API 与 Excel 插件多见于高档/机构计划）——需 sales 或免费试用确认。另经 [Nasdaq Data Link (ORTX)](https://data.nasdaq.com/databases/ORTX) 分销，偏机构定价 |
| **Fintel** | Short squeeze 评分、borrow fee（网页产品）；[API](https://developers.fintel.io/docs) 主力是 SEC filings/ownership/short volume，且其 Web Data API **正在停售** | API + 网页订阅 | 网页订阅约 $25+/mo 档起（第三方口径，官网定价页有反爬未能直验） | 作为 borrow fee 的 API 来源**不可靠**；更适合 ownership/insider 筛查 |
| **S3 Partners** | 机构级实时 SI、borrow rate、squeeze 风险 | 机构 feed | Enterprise（contact sales） | 个人不现实 |

#### (c) Float 与 days-to-cover 输入

- **Days-to-cover**：Massive 的 Short Interest endpoint 直接给出 days_to_cover 与 ADV（免费档即可用）；自算则 = SI ÷ 20 日 ADV（ADV 来自任意股价源）。
- **Float（自由流通股本）**：这是**个人最难干净获取的字段**。Massive ticker overview 只有 shares_outstanding 类字段（非 float）；FINRA 不提供 float。现实选项：Fintel（网页/API 部分覆盖）、Benzinga float（可经 [Massive Partner Data](https://massive.com/pricing) $99/mo/dataset 获取，需确认 float 字段是否在该 dataset 内）、或 SEC 文件衍生供应商。**注意 float 本身存在口径分歧与静默修订**，用于回测时必须按"当时可得值"快照入库，否则产生前视。SI % of float = FINRA SI ÷ float。
- **PIT 提示**：双周 SI 天然带公布滞后——回测时必须用"公布日+1"作为可用时点，而非结算日，否则是最常见的前视错误之一。

---

## 3. 推荐组合

### (a) MVP / Shadow 研究期 —— 目标 ≈ $99–128/mo + 少量一次性历史费用

| 用途 | 选择 | 成本 |
|---|---|---|
| SPX/VIX 实时指数 | **Massive Indices Advanced** | $99/mo |
| VIX1D 实时 | 同上（开户后首日验证 I:VIX1D；若无则临时用 Cboe 官网延迟值 + VX 近月期货推算） | $0 |
| ES 实时 | **暂不购买**：MVP 用 "implied SPX" 可从 SPX 指数直接获得；若需要 ES，Massive Futures Starter $29（10 分钟延迟足够 shadow 校准） | $0–29/mo |
| SPXW 分钟 NBBO 历史（回测） | **Databento usage-based 历史**：OPRA CBBO-1m（2013 起，含已到期合约）+ GLBX.MDP3 ES trades/BBO 历史；先用 $125 免费额度跑成本估算（metadata.get_cost 免费） | 一次性，预计数十到数百美元（按 GB 计费，CBBO-1m 极轻量） |
| SPX/VIX EOD 校验 | Cboe 官网免费 CSV | $0 |
| Short-squeeze 筛选 | **零新增成本起步**：Massive Stocks **Basic 免费档**（Short Interest + Short Volume，2 年历史）或直接抓 FINRA 免费文件回测；borrow fee 用 **IBKR 免费 SLB 文件**每日落盘自建历史；float 暂用 shares outstanding 近似或人工维护 watchlist | $0 |

**覆盖缺口**：2023-03 之前的 SPX/VIX **盘中**指数历史（Massive 指数历史仅从 2023-03）；VIX1D 实时未验证；ES 实时 deferred（与原设计一致）；全市场历史 borrow fee 不可得（IBKR 单池免费数据是个人上限）。

### (b) Production-grade 期 —— ≈ $526/mo

| 用途 | 选择 | 成本 |
|---|---|---|
| ES 实时 + 深历史 | **Databento GLBX.MDP3 Standard**（实时、PIT、与回测同一 API） | $199/mo |
| SPXW/SPX 期权实时 NBBO 冗余 + 全历史 | **Databento OPRA Standard**（与 Webull 互为冗余；历史 CBBO-1m 回 2013） | $199/mo |
| SPX/VIX(/VIX1D) 实时指数 | **Massive Indices Advanced** | $99/mo |
| Short interest / short volume 全历史 | **追加 Massive Stocks Starter**（若因其他原因已持有 Stocks 更高档则边际成本 $0） | $29/mo |
| VIX 期限结构（可选增强） | Databento XCBF.PITCH（VX 期货，usage-based 历史 + 订阅实时） | 按量 |

**仍然未覆盖 / 需另购**：
- 2023-03 前的 SPX/VIX **分钟级**指数历史 → Cboe DataShop 历史指数文件（价格需登录查询，一次性）；
- 若未来需要 CME L2/L3 深度或 >1 个月的 MBP/MBO 历史 → Databento usage-based 补购；
- 若 short-squeeze 策略上线后被证明依赖 borrow/utilization 信号 → 评估 **Ortex Advanced（$99–149/mo，先确认个人档含 API）**；S3 Partners / IHS Markit Securities Finance 仅在机构化阶段考虑；
- 一切数据的**对外再分发/产品化展示**均需升级（Databento Plus $1,750 起 / Massive Business / Cboe 直连协议）——read-only 内部决策平台在当前组合内合规。

**为什么生产期不把期权实时也交给 Massive**：Massive 的 aggregates 会每日修订（非 PIT），NBBO 历史只到 2022-03，且实时 quotes 与 Databento OPRA Standard 同价（$199）；Databento 在 PIT、时间戳精度、历史深度上全面占优。Massive 在组合中的独特价值是**指数**（Databento 完全没有、Cboe 直连太贵）与**低成本的 short interest/short volume API**。

---

## 4. 风险与未决问题（只有 sales call 或试用账户能定）

1. **Massive I:VIX1D 与 SPXW 合约覆盖**：文档示例只有 I:SPX/I:NDX/I:DJI；VIX1D（2023-04 推出）是否在 11,400+ 指数列表内、SPXW 周期权合约在 options endpoints 是否完整（含已到期），需免费 Basic 账户调 tickers/contracts endpoint 验证。
2. **Massive Futures Advanced $199 是否含 CME non-pro 交易所费**：定价页写 "Real-time" 但未写是否另收 CME 转收费；另需确认其期货数据来源（直连 CME 还是经第三方，影响延迟与 PIT 性）。
3. **Databento OPRA Standard 的 non-pro license fee 细节**：GLBX Standard 明示 "No license fees"，OPRA 档是否同样豁免 OPRA non-pro 费需注册后走 licensing 问卷确认（OPRA non-pro 费名义上极低）。
4. **Cboe DataShop 全目录价格**：Option Quotes/Trades、指数历史文件（SPX/VIX/VIX1D 盘中）、VIX EOD Calculation Inputs 的标价均需创建免费账户或邮件询价（+1 800 307-8979 / marketdata@cboe.com）。历史指数文件的**最早日期**（决定能否覆盖 2023-03 前回测）也只有询价能确认。
5. **Massive 指数历史回填进展**：官方 2023 年称 "actively looking to acquire and backfill"，至今未见完成公告——若已回填至更早，MVP 的指数历史缺口会缩小。
6. **衍生数据（derived data）条款**：三家对个人研究自用均宽松，但我们平台会存储加工后的分钟级特征（如 implied vol surface）。Databento/Massive/Cboe 对 "不可还原为原始数据的衍生数据" 通常豁免，但需逐份协议确认（尤其 Cboe 指数衍生数据有专门条款）。
7. **Webull 叠加 OPRA 订阅的重复许可**：同时用 Webull（已含 OPRA 显示许可）和 Databento OPRA Standard 时，两边各自的 subscriber 申报口径（non-pro 身份一致性）需在开户问卷中保持准确。
8. **Ortex 个人计划的 API 权限**：Basic/Advanced 是否开放 REST API 与 Python SDK、速率限制、历史深度，定价页未明示——免费试用或 sales 确认。
9. **IBKR SLB 文件的当前访问方式**：公开 FTP 端点历史上可用但官方页面几经改版（原 api.ibkr.com 说明页已 404），需实测免登录可用性与更新频率；若无 IBKR 账户，需确认该文件是否仍匿名可取。
10. **FINRA 数据的许可边界**：FINRA Data 明示 "non-commercial use"；我们内部决策支持用途大概率合规，但若平台输出未来对外展示需重新评估。
11. **Massive short interest/volume 的历史起点**：Basic 档 2 年、付费档 "All history"——"All" 具体起始年未在文档标注（FINRA 日报 2010 年后才有完整 TRF 口径），回测前需用免费账户实测最早可用日期。
12. **Float 字段来源与口径**：Benzinga（Massive partner，$99/mo）是否含 float 及更新频率未验证；不同供应商 float 差异可达数个百分点，影响 SI%float 类筛选阈值。

---

## 主要引用来源

- **Databento**: [pricing](https://databento.com/pricing) · [GLBX.MDP3](https://databento.com/datasets/GLBX.MDP3) · [OPRA.PILLAR](https://databento.com/catalog/opra/OPRA.PILLAR) · [CME plans 博客](https://databento.com/blog/introducing-new-cme-pricing-plans) · [2026-05 调价](https://databento.com/blog/updates-to-subscription-pricing) · [OPRA improvements](https://databento.com/blog/opra-improvements-coming-soon) · [OPRA 发布](https://databento.com/blog/opra-data) · [CFE 发布](https://databento.com/blog/introducing-cboe-futures-exchange-cfe)
- **Massive**: [pricing](https://massive.com/pricing) · [Futures 发布](https://massive.com/blog/futures-data-has-arrived) · [Futures docs](https://massive.com/docs/rest/futures/overview) · [Indices 发布](https://massive.com/blog/indices-data-has-arrived) · [Options quotes flat files](https://massive.com/docs/flat-files/options/quotes) · [指数期权 KB](https://massive.com/knowledge-base/article/does-massive-support-options-data-for-index-contracts) · [取消交易处理 KB](https://massive.com/knowledge-base/article/how-much-does-massives-feeds-handle-canceled-trades) · [覆盖范围 KB](https://massive.com/knowledge-base/article/what-symbols-exchanges-are-included-in-massives-data) · [Short Interest endpoint](https://massive.com/docs/rest/stocks/fundamentals/short-interest) · [Short Volume endpoint](https://massive.com/docs/rest/stocks/fundamentals/short-volume) · [Short 数据教程博客](https://massive.com/blog/short-volume-short-interest-tutorial)
- **Cboe**: [Global Indices Feed / 接入方式](https://www.cboe.com/us/indices/accessing-index-data/) · [DataShop](https://datashop.cboe.com/) · [VIX EOD Calculation Inputs](https://datashop.cboe.com/vix-index-eod-calculation-inputs)
- **CME 直接成本参考**: [CME ILA 费用实测（第三方）](https://blog.pickmytrade.io/cme-ila-fee-explained-why-it-costs-290-427-mo/) · [CME DataMine 目录](https://www.cmegroup.com/market-data/browse-data/catalog/futures-and-options-data.html)
- **Short-squeeze 专项**: [FINRA Equity Short Interest](https://www.finra.org/finra-data/browse-catalog/equity-short-interest) · [Ortex Pricing](https://public.ortex.com/ortex-pricing/) · [Ortex API docs](https://docs.ortex.com/reference/ortex-apis) · [Nasdaq Data Link ORTX](https://data.nasdaq.com/databases/ORTX) · [Fintel API docs](https://developers.fintel.io/docs) · [第三方对比：Alphanume short-selling data sources（2026-03）](https://www.alphanume.com/blog/best-market-data-sources-for-systematic-short-selling-research)
