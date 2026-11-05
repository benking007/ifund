# 开放平台只读查询

`/api/open-data/v1` 供独立金融数据网关访问。所有接口使用 iFund 的 Bearer JWT。网关先用服务账号 PAT 调 `/api/auth/token/exchange`，再用得到的 JWT 请求这些数据接口。原有图表和详情接口保留原来的语义。

单基金接口接受 `fund_code`，批量接受互斥的 `fund_codes` 逗号列表，最多 20 个六位代码。指定批量持仓时需要明确共同的报告季度。

所有接口返回 `items`、真实 `total`、`page`、`page_size` 和 `source=ifund`。`page` 从 1 开始，`page_size` 为 1–500。日期使用真实的 `YYYY-MM-DD` 并包含起止日。字段投影只包含公共产品数据。非法或重复参数返回 400。

| GET 路径 | 查询参数与行为 |
|---|---|
| `/fund/nav` | `fund_code`、可选 `start_date/end_date`，按日期升序分页，分别返回经过类型审定的原始单位 `nav`、累计 `acc_nav` 和复权 `adj_nav/adj_src` |
| `/fund/basic-info` | 精确 `fund_code`，合并主档、详情快照和扩展主档的公开字段 |
| `/fund/list` | 可选精确 `fund_code`，按代码升序分页 |
| `/fund/categories` | 分页查询实际分类字典，不接受基金或日期筛选 |
| `/fund/returns` | `fund_code`，可选详情快照日期筛选，收益单位为百分数 |
| `/fund/fees` | `fund_code`，返回实际管理、托管和销售服务费快照，未知可选字段为 null |
| `/fund/events` | `fund_code`、`event_type=div` 或 `split`、可选日期窗口，按除权日升序 |
| `/fund/holdings` | `fund_code`、`holding_type=stock` 或 `bond`、可选 `quarter=YYYYQn`，明确指定的报告期没有记录时返回空列表，省略时返回实际最新季度 |
| `/fund/manager-tenure` | `fund_code`、可选 `is_current=0/1`，按任职段顺序分页，返回真实共管情况与任职收益 |
| `/fund/shares` | `fund_code`、可选日期窗口，返回份额快照及 `share_unit`，不推断申赎分量 |

`scale` 使用亿元，区间与任职收益使用百分数。`share_value` 的源单位是万份并保留显式单位字段。股票持股数的源单位为万股，持仓市值为万元，`units` 返回对应标记。分红为每份派息元数，源数据没有确定的税前或税后标记。事件与持仓仅代表已入库记录的覆盖范围。完整性和对外字段能力由网关契约继续约束。

净值接口先在同一个只读事务中核对整批基金的 `funds.type`。该字段来自既有 `fund_name_em` 主档同步；`fund_type=stock/non_stock` 只是本项目的选基分组，不能作为净值类型依据。代码内 `_UNIT_NAV_TYPES` 明确列出目前审定的股票、混合、债券、股票/固收指数、FOF、QDII 及 REITs 类型。货币类型、空类型、新增未知类型、主档缺失均返回 HTTP 503、`error=product_window_incomplete`、`reason=nav_type_unverified`，网关按质量错误 `50301` 处理。批量中任一基金未通过时整批拒绝，不静默删除记录或改变总数。

此限制适用于单位、累计及复权净值：既有采集器会把货币基金的 `Data_millionCopiesIncome` 写入 `fund_nav.nav`，部分 `adj_src=calc` 的复权值也可能建立在该字段上。每万份收益不能当作单位净值，也不能根据产品名称一律补成 1。参考接口 api-247 单独定义了货币基金收益。日后需先补齐来源及收益类型元信息，再审定相应查询能力。本次仅限制新的开放只读出口，既有采集、走势图和详情接口保持原行为。

查询复用 SQLite/MySQL 访问抽象。总数与本页记录在一致读事务中取得，SQLite 使用延迟读事务，MySQL 使用 `START TRANSACTION READ ONLY`。这些请求不会调用采集器、触发回补或修改业务数据。

安装与验证遵循 iFund 既有依赖和部署流程。合并此模块及 `main.py` 的蓝图注册后，在隔离数据库运行以下检查。

```sh
PYTHONPATH=backend python -m pytest -q backend/tests/test_open_data.py
python -m pylint backend/app/open_data backend/app/db/sqlite.py backend/app/db/mysql.py backend/app/main.py
```

本机测试覆盖超过原走势图 2000 条限制的净值分页、单位与累计净值区别、货币和未知类型拒绝、混合批次分页前核验、历史季度精确匹配、Key/JWT 拒绝、字段投影和实际总数。生产验证需要核对部署的数据库字段、服务账号 JWT 和实际数据窗口。

持仓单位核对依据为 [AKShare 基金持仓与债券持仓字段](https://akshare.akfamily.xyz/data/fund/fund_public.html)，分红单位依据为 [Tushare fund_div](https://tushare.pro/document/2?doc_id=120)。

## 第二批基金查询

以下新增路径均位于 `/api/open-data/v1`，沿用现有 JWT，只读、单值或最多 20 代码批次及稳定分页；这些是完整参考合同的部分能力，不声明完整覆盖。

| 内部 GET 路径 | 对应公开参考 | 已实现的真实内容 |
|---|---|---|
| `/fund/reference-categories` | api-235 `/fund/categories` | 按基金主档实际 `type` 映射审定的一级分类；原文名称供网关校验，不推断二三级分类 |
| `/fund/reference-fees` | api-243 `/fund/fee-structures` | 当前管理、托管、可选销售服务固定费率；每类费用一条，单位为百分数，真实零费率保留，先展开再计算 total 与分页 |
| `/fund/reference-listings` | api-237 `/fund/listings-record` | 发行日期、面值、日常申赎开放起始日及管理/托管费率；不把实际发行份额当作募集目标 |
| `/fund/manager-tenure` | api-239 `/fund-manager/basic-info` | 原始任职段姓名、起止日期与在任状态；`is_current=0/1`、`manager_name` 精确筛选均在计数和分页前执行 |
| `/fund/reference-shares` | api-266 `/fund/shares-changes` | 唯一主代码角色的时点基金总份额、日期和单位；网关万份乘 10000 转为份，公开 `dataSource=7` 截止时点 |
| `/fund/reference-performance` | api-253 `/fund/performance-attribution` | 已存区间收益及 1/3/5 年年化波动率、夏普和最大回撤；不推断 alpha、择时或选股归因 |

费率、分类、发行和风险快照不具备已核实的历史发布日期，传入日期筛选返回 400。份额按真实 `trade_date` 包含起止日期。数据表没有匹配记录时可以返回合法空集合；存在快照但不含任何对应实值时，费用、发行、风险接口返回 503 质量错误，不把仅代码或名称的占位记录计作成功。基金经理共管段保持整个姓名字段和对应任职段，不拆成人名后套用共管收益；个人履历、学历、生日和个人全局编号尚未开放。

`reference-shares` 必须存在该基金的 `fund_basic_ext.ts_code` 主映射，仅选择该角色的 `share_type=fund_total`、`source_field=fd_share` 记录。它不会合并同一六位码的 `.OF` 和 `.SH/.SZ` 角色。主映射缺失、单位不为 `10k_shares` 或数值异常返回 503 质量错误。期初份额、申赎、转入转出、红利再投、拆分、转换和是否合并披露均没有来源，保持未提供。

年化风险口径依据 [AKShare 官方雪球映射源码](https://github.com/akfamily/akshare/blob/main/akshare/fund/fund_xq.py) 及[接口说明](https://akshare.akfamily.xyz/data/fund/fund_public.html)：年化波动率和最大回撤已为百分数，不再乘 100；夏普比率无量纲。源 `fund_details.trade_date` 是刷新基准交易日，不能证明每项风险统计的原始截止日期，所以不公开为 `endDate`。这是当前观测快照，不能用于时点回测（PIT）或历史时点归因。基金发行及费率字段来自 [Tushare fund_basic](https://tushare.pro/document/1?doc_id=19) 的原始映射；没有证据时不补币种、执行状态、公告日期或阶梯收费条件。

主档输出补充了公开的管理人、托管人、完整名称、到期日、投资目标与策略；净值、回报及任职记录通过有界批量主档查询补基金名称。旧的图表与产品 API 没有改变。

### 发布条件与仍未开放的内容

- 部署需合入本目录全部源路由及网关新合同；现有 phase3 主档、份额表及对应同步应已完成。无需新增业务表、后台任务或临时按需采集。
- 用真实只读服务 JWT 对同基金和同日期核验固定费率、主代码角色、份额乘数、风险样本与源库一致；隔离测试夹具通过不能替代目标环境数据覆盖核验。
- 资产配置仍不发布：既有详情采集调用持仓比例接口时没有传入报告季度，接口默认季度与当前 `trade_date` 可能不同，且分母与报告时间尚未完整留存。不能把这些比例、前十大持仓合计或最新规模快照冒充报告期净资产配置。
- 基金规模仅有来源快照，没有经审定的独立规模历史或报表口径；不拿它填主要财务指标的 `navEnd`。经理个人履历、发行募集结果、阶梯申赎费、完整行业与穿透配置均需新增确切来源和采集后再开放。
