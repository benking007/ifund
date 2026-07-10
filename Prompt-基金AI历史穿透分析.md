# Prompt：基金 AI 历史穿透分析（打分 + 归因）

> 用途：给单只基金做**定性打分**，写入 `fund_ai_analysis`。
> 与旧版 `Prompt-基金AI定性分析填充.md` 的区别：**不只看当下快照，而是基于历史一起判断**——
> 业绩要归因到「当前团队」、集中度要看「是否正在漂移成单押」。
> 输入是 `ifund analyze bundle --code <CODE> --json` 产出的数据包（已把历史算好），
> 输出是 `fund_ai_analysis` 的 JSON，经 `ifund preset ai-set` 写库。

---

## 一、拿到什么（数据包字段速览）

每只基金一个 JSON 包（`ifund analyze bundle --code X --json`），关键区块：

- `meta`：名称/类型/公司/成立日/规模(亿)/投资策略摘要。
- `metrics`：`return`/`sharpe`/`max_drawdown`/`volatility`/`rank` 各周期（ytd/1y/3y/5y），`position_stock/bond`。
  ⚠️ 这些是**全历史**指标，不区分是谁做的——**归因交给下面 `manager` 区块**。
- `manager`（**归因核心**）：
  - `current` / `current_lead` / `is_comanaged`：当前团队、主理人、是否共管。
  - `exact_team_since` / `exact_team_years`：**当前完全相同团队**连续在任起始日与年限。
  - `lead_since` / `lead_years`：**主理人**（首位经理）连续在任起始（容忍共管更替）。
  - `exact_team_covers` / `lead_covers`：`{1y,3y,5y}` 各布尔——**该业绩窗口是否被当前团队/主理人的任期覆盖**。
  - `segment_count` / `distinct_team_count`：任职段数 / 不同团队组合数（>1 = 历史换过团队）。
  - `recent_segments`：最近若干段（起止/经理/任职天数/任职回报%）。
- `holdings`（**漂移核心 + 赛道判断核心**，跨 5 季度）：
  - `quarters[]`：每季 `top1/top3_ratio/top5_ratio/top10_ratio/hhi_top10/num_stock/report_type`，
    **以及行业结构**：`sw_l1_mix`（前十大按申万一级聚合 `[{l1,ratio,share%}]`）、`top_l1`/`top_l1_share`、
    `distinct_l1_top10`（前十大横跨多少个申万一级）、`theme_mix`（按大主题聚合）、`top_theme`/`top_theme_share`。
    （`report_type`：季报=前十大；半年/年报=全量。**跨季用 `top10_ratio` 比较**，各季都有≥10 只，可比。）
  - `consecutive_top10_overlap[]`：相邻季前十大重合只数（低=高换手/交易型）。
  - `concentration_trend_top10`：`上升/平稳/下降`（按仓位集中度）。
  - **`dominant_theme_by_quarter`**：逐季「最大主题」序列——稳定=某赛道 / 切换=轮动。
  - **`distinct_dominant_theme`** / **`theme_concentration_trend`**（`收敛/稳定/发散`）。
  - **`sector_focus`**：据持仓主题结构派生的赛道定性线索（`单一主题高度集中(赛道特征)`/`单一主题主导(赛道倾向)`/
    `主题轮动`/`多行业分散(选股型)`）——**这是「按持仓而非名字」判断 `fund_kind` 的主依据**（下文问②′）。
  - **`latest_top10_detail`**：最新一季前十大明细 `[{name,l1,theme,l3,ratio}]`——看具体持有哪些票、横跨哪些行业。
  - `latest_top10_ratio` / `latest_top1` / `latest_top_theme` / `latest_top_theme_share` / `latest_distinct_l1`。
- `nav_regime`：累计收益曲线派生——`return_last_1y/3y`、`return_since_current_team`（当前团队上任以来净收益）。
- `data_caveats`：数据局限（如持仓仅覆盖 2025Q1 起、无 5 年指标、历史换过 N 个团队）。**必须纳入判断并降置信度。**

> 若未附数据包，可自行 `ifund(["analyze","bundle","--code","<CODE>","--json"])` 取。

---

## 二、怎么判断（三问 + 历史归因规则）

核心仍是三问：**①靠运气还是硬实力 ②是否单押赛道 ③有无可复用的硬逻辑**。
但每一问都必须**先做历史归因**，不能拿全历史指标直接给现任团队记功。

### 问① 靠运气还是实力 —— 先归因，再打分

**归因优先级**：判断某个漂亮的 `sharpe_3y/5y`、`return_3y/5y`、`rank` 时，先看它落在谁的任期里：

1. `exact_team_covers[窗口] = true` → 该窗口业绩**完全归当前团队**，可据此给实力分。
2. `exact_team_covers = false` 但 `lead_covers[窗口] = true` → **主理人在、但共管换过人**：
   业绩**部分归主理人**，在 `skill_reason` 注明共管更替（`distinct_team_count`），`confidence` 至多 `medium`。
3. 两者皆 `false`（现任是**接任**，任期没覆盖该窗口）→ 该长周期指标**不算现任的功劳**：
   - **不得**用 3y/5y 的高 sharpe/排名去抬 `skill_score`；
   - 改用 `return_since_current_team` + 1y 指标评估现任；
   - 现任 `exact_team_years < 2` → `style_stability=unproven`、`confidence=low`、`skill_reason` 注明样本短。

**运气信号**（压低 `skill_score`、`luck_verdict` 趋向 `luck`）：
- 业绩集中来自单一风口主题（见问②），且该主题恰好近一两年爆发；
- 长周期指标好但**不归现任**（接任者蹭前任余荫）；
- `distinct_team_count` 大、团队频繁更替，业绩连续性存疑。

**实力信号**（抬 `skill_score`、趋向 `solid`）：
- 有**归属现任**的、够长（≥3y）的任期，且该期内 sharpe/排名稳定靠前；
- 超额不依赖单一主题（行业不单押、重仓有轮动但逻辑清晰）；
- 跨不同市场环境仍兑现（看 `recent_segments` 各段任职回报的稳健度）。

### 问② 是否单押赛道 —— 看历史漂移，不只看当下

用 `holdings.quarters[]` 的 `top10_ratio` 序列 + `concentration_trend_top10` + `latest_top1` 判断：

- `single_bet`：最新 `top10_ratio` 高（≳45%）**或**某主题/个股连续多季霸榜且 `trend=上升`；
- `focused`：适度集中（`top10_ratio` 约 30–45%），行业不单押；
- `diversified`：`top10_ratio` 低（≲30%）且分散。

**关键场景（本次分析的重点）**：`distinct_team_count`/经理没变、但持仓**从分散漂移成单押**
（`top10_ratio` 明显上行 + 单一主题霸榜）→ 判 `single_bet` 或 `focused`，
在 `concentration_reason` 写明「**同一经理、风格漂移集中**」，并据此审视 `rating`：
若这种集中是**无补偿的押注**（波动/回撤放大而超额不稳），压低 `rating`。

用 `consecutive_top10_overlap` 辅判换手：重合持续偏低（如 ≤4/10）→ 交易型/高换手 →
`style_stability=volatile`、写 `turnover_note`。

### 问②′ 是不是「赛道型」基金 —— ⚠️ 一律以持仓行业结构判定，**绝不看基金名/契约**

> 这是本次修订的重点：`fund_kind` 过去易被基金名带偏（名字含「消费/科技/医药/芯片」就判赛道）。
> **基金名与契约只是线索；一旦与实际持仓冲突，以持仓为准。**
> 例：某「消费优选」基金前十大含立讯精密(电子)、法拉电子(电子)、春秋航空(交运)、亿纬锂能(电力设备)，
> 横跨 8 个申万一级、最大主题占比仅 46% → 它**不是消费赛道基，而是选股型**。

判定 `fund_kind` 用 `holdings` 的行业结构（`sector_focus` / `dominant_theme_by_quarter` /
`top_theme_share` / `distinct_l1_top10` / `latest_top10_detail`），而非名字：

- **`sector`（赛道/主题）**：某一大主题**长期居首且当前仍集中**——`dominant_theme_by_quarter` 基本同一主题，
  且最新 `top_theme_share ≳ 55%`、`distinct_l1_top10` 小（≲4）。（`sector_focus` 多为「赛道特征/赛道倾向」。）
- **`rotation`（行业轮动）**：每季较集中于某一两个主题，但**主导主题跨季切换**
  （`dominant_theme_by_quarter` 有 ≥2 个不同值、各季 `top_theme_share` 仍偏高）。
- **`subjective`（主观选股）**：**横跨多个申万一级、无单一主题过半**——最新 `distinct_l1_top10 ≥ 6~8`、
  `top_theme_share < 50%`。**即便基金名含赛道词，也判 `subjective`，并在 `concentration_reason`/`hard_thesis` 点明「名不符实、实为选股型」。**

**结合历史看漂移**（配合 `theme_concentration_trend`）：
- 名义赛道基但主题占比**发散**（`发散(更分散)`、最新 `top_theme_share` 明显低于早期）→ 正在松绑为选股型，勿再判 `sector`。
- 名义宽基/选股但主题占比**收敛**到单一主题（`收敛(更集中)`）→ 正漂移成赛道单押，`concentration` 趋 `single_bet`，`fund_kind` 趋 `sector`。

> `fund_kind` 与 `concentration` 是两件事：`fund_kind` 看**行业主题结构**（横跨几条赛道），
> `concentration` 看**权重集中度**（`top10_ratio`/`hhi`）。二者结合：
> 「单一主题 + 高权重集中」= 典型赛道单押（`sector` + `single_bet`）；
> 「多主题 + 权重也分散」= 选股型分散（`subjective` + `diversified`）。

### 问③ 有无可复用硬逻辑

`hard_thesis` 写能力来源是否**可解释、可复用**：选股 / 行业轮动 / 风控 / 长期任职兑现。
若业绩主要靠单主题 β 或接任余荫，明说「硬逻辑不足/存疑」。

---

## 三、打分刻度（skill_score 0–100，锚定归因）

- **75–100**：有**归现任**的长期（≥3y）业绩且该期排名稳定靠前；超额可复用、不靠单一主题。
- **45–74**：数据不错但**归属期偏短**，或超额部分来自风口/单押，或主理在而共管频繁更替。
- **0–44**：长周期指标**不归现任**（近期接任）、或靠单一热门主题（集中度上行且无可复用边际）、或样本严重不足。

`rating`(0–3) 与 `recommend`(0/1) 综合三问给：单押且无补偿 / 归因不成立 / 样本太短 → 从严。

---

## 四、输出字段（`fund_ai_analysis`，枚举只能取给定值）

| 字段 | 类型 | 取值 / 填法 |
| --- | --- | --- |
| `verdict` | 文本 | 一句话总评，**点明归因**（如「主理3.5y、共管13拨换手，3y业绩仅部分归他」） |
| `rating` | 整数 | 0–3 |
| `recommend` | 整数 | 0/1 |
| `skill_score` | 整数 | 0–100（按第三节刻度） |
| `luck_verdict` | 枚举 | `solid`/`mixed`/`luck` |
| `skill_reason` | 文本 | **必须写归因**：用了哪个窗口、归谁、覆盖与否 |
| `concentration` | 枚举 | `single_bet`/`focused`/`diversified` |
| `concentration_reason` | 文本 | 引 `top10_ratio` 序列/趋势/第一大；如属「同经理漂移集中」务必点明 |
| `fund_kind` | 枚举 | `subjective`=主观选股 / `rotation`=行业轮动 / `sector`=赛道主题。**按问②′据持仓行业结构判，禁止看基金名**；名不符实（名义赛道、实为选股）判 `subjective` 并在 `concentration_reason` 点明 |
| `hard_thesis` | 文本 | 能力来源是否可复用 |
| `manager` | 文本 | = `manager.current` |
| `tenure_years` | 小数 | = `manager.exact_team_years`（当前团队年限）；`skill_reason` 可另述 `lead_years` |
| `is_original` | 整数 | 0/1，当前团队是否自成立起就管（`exact_team_since == meta.establish_date`） |
| `is_comanaged` | 整数 | 0/1，= `manager.is_comanaged` |
| `scale_risk` | 枚举 | `tiny`(<~1亿清盘险)/`small`/`ok`/`large`(过大恐平庸) |
| `style_stability` | 枚举 | `stable`/`volatile`(漂移/交易型)/`unproven`(样本不足) |
| `turnover_note` | 文本 | 换手风格（引 `consecutive_top10_overlap`） |
| `tags` | 数组 | JSON 字符串数组，如 `["接任","单主题","光模块"]` |
| `confidence` | 枚举 | `high`/`medium`/`low`（归因不实/样本短→降） |
| `model` | 文本 | 你的模型名 |
| `data_basis` | 文本 | 用了哪些区块/区间（务必含归因窗口与持仓覆盖季度） |

**准则**：
- 归因不成立、样本 <2y、数据 caveat 明显 → `style_stability=unproven`、`confidence=low`，别硬下强结论。
- 区分「经理硬实力」与「基金 β」：长指标好但不归现任 = 不给现任记功。
- 小规模(<~1亿)提示清盘；大规模但平庸 → `scale_risk=large` 并在 `hard_thesis` 说明是否被规模拖累。

---

## 五、写入

```
ifund preset ai-set --code <CODE> --data @/path/to/result.json
```

- `--data` 支持 JSON 字面串 / `@文件` / `-`(stdin)；**部分字段 upsert**（只覆盖本次给的字段）。
- 写入前有枚举/越界/tags 校验，被拒按提示改。字段多时优先写文件再 `@路径`，避免转义出错。
- 写完 `ifund preset funds --id 15 --ai --json` 回读核对。

---

## 六、样例（同一经理、风格漂移成单押）

014728 易方达成长动力C：刘健维自成立(2022-02)一人到底，3y 完全归他（`exact_team_covers.3y=true`），
但持仓 `top10_ratio` 从 28%→47% 一路上行、新易盛（光模块）连押 4 季、`trend=上升`——
经理没换、风格漂移成单主题押注。示例结论：

```json
{"manager":"刘健维","verdict":"原装3.5y、业绩全归他，但已漂移成光模块单主题押注",
 "rating":2,"recommend":0,"skill_score":68,"luck_verdict":"mixed",
 "skill_reason":"exact_team自2022成立覆盖3y，3y夏普1.84/排名46/3599均归现任；但近1年超额高度依赖AI算力β",
 "concentration":"single_bet","concentration_reason":"前十大占比28%→47%持续上行，新易盛连4季第一大，同一经理风格漂移集中",
 "fund_kind":"sector","hard_thesis":"成长赛道景气投资，但当前重仓高度绑定光模块，可复用性存疑",
 "tenure_years":4.4,"is_original":1,"is_comanaged":0,
 "scale_risk":"ok","style_stability":"volatile","turnover_note":"相邻季前十大重合3~6/10，换手中等偏高",
 "tags":["原装","单主题","光模块","景气"],"confidence":"medium",
 "model":"<你的模型名>","data_basis":"metrics全周期+manager归因(exact_team覆盖3y)+holdings 2025Q1~2026Q1五季度漂移+nav自成立"}
```
