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
