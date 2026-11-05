# 公募基金公开产品：部署、采集与验收

本目录维护冻结的 api-223 至 api-274 合同。HTTP 只做认证和固定 SELECT；抓取、计算、迁移均在独立 CLI 进程运行。没有启动 worker、服务或在真实数据库执行写入。现有图表和私人仓位接口不受影响。

## 本批新增能力

- api-223：AKShare 公告报告/分红/人事源。保留 AN... 自然标识，不把它哈希成第三方数字公告 ID；按数字 ID 查询在无对应来源字段时拒绝。
- api-231：Tushare 融资融券明细，数量为股、金额为元，必须已验证场内基金角色。
- api-240：仅解析基准文本中明确的 `名称×比例%+名称*比例%`，比例须精确合计100。没有代码就省略代码；没有生效日期就不接受生效日期查询。不解析含糊自然语言和嵌套公式。
- api-247：货币基金每万份收益及7日年化，绝不作为基金净值。
- api-252：已有任期报告的团队姓名、任期和实际收益快照。多人字符串保持团队；空离任日期不能证明在任。date 为真实源抓取日，该快照不能用于历史时点回测。
- api-254：显式指定指数角色的 Tushare index_daily，不猜指数市场后缀。
- api-255：真实复权净值和指数价格先对齐日期，再计算同区间收益 Pearson 相关。窗口为6/12/36/60日历月，至少60条共同收益；需有起点前7天内的真实观测、终点一致，方差为0或历史不足不填0。方法版本 `aligned-daily-return-pearson-calendar-v1`，不是第三方专有算法。
- api-259：F10 真实报告的最新资产净值与股票/债券/现金/存托凭证净资产比例。亿元转元；不乘已四舍五入比例臆造市值。报告期存源记录标识与覆盖元信息。合同 date 是托管人复核日期，因此未知时省略，不拿报告期顶替。
- api-271：Tushare 沪深 PCF 原始成份数量、替代标志和有来源的现金替代数值。trade_date 为生效交易日，不冒充 publishDate。若没有发行人发布时间证据，仍可读真实篮子字段；带发布日期条件则返回品质错误。固定替代金额仅映射沪市“必须”替代的 sca；cpr/rdr 只有精确整数才填入冻结合同的整数比例字段，不截断小数。
- api-272：同日实际收盘与实际单位净值算 `(close/unitNav-1)*100`，无同日净值则拒绝，不用前日净值补齐。
- api-273：F10 实际按报告期财务数据，半年报 I/年报 F、真实报告截止日、元/每份元/原始百分数。支持10项实际披露指标；无发布日期、累计净值时省略，不拿单位净值代替累计净值，不把半年报冒充季度报。

原有主档、分类、费用、份额、任期、NAV、收益、持仓等17项继续走其已有严格源。api-224/225/226/227/228/229/230由 fin-data 的场内基金源负责，详见平台仓库 `docs/fund-products.md`。

## 第二批已核验子集（2026-11-06）

原待源17项中新增12项原生部分字段实现。公开 schema 只列下表已实现字段，完整参考合同另列；不会用全空列暗示其余字段已支持。下列来源均由离线 worker 采集，HTTP 请求只读已经落地的完整快照。

|编号|真实来源、已实现口径与边界|
|---|---|
|238|东财全市场申赎列表的当前申购上限（元）与原文申赎状态 remark；不是逐次变更记录，不伪造变更类型、生效日。TTL 最多300秒。|
|241|F10 当前投资范围原文 description；不猜投资类别、代码、比例上下限或生效日。|
|244|主档真实 company_id 外键连接 Tushare 公司名、简称和成立日期；不把成立年限伪装为资历评级，不生成评价分数。|
|248|东财明确标注“同类平均”的实际累计收益曲线，与基金曲线共同起止点重基后计算1/3/6日历月收益；方法 `em-source-defined-peer-curve-calendar-returns-v1`。不声称复刻未知同类分类、成员权重或风险算法。|
|251|完整当前经理目录确认姓名唯一后，对该经理页面明确列出的所有当前基金等权计算1/3/6/12/36/60日历月区间收益；方法 `em-current-fund-cohort-equalweight-<id>-v1`。这是独立的当前持管基金篮子，不是经理个人历史业绩；任一成员缺区间则该区间整体不输出，不复制团队业绩。曲线观察日不冒充发布日期。|
|262/268|对平台完整、有效的 Tushare 基金主档逐只核验264快照，再按原始行业代码反向筛选；任何成员无来源、过期或截断即拒绝发布。268仅支持 matchType=4 的最新 NAV 占比阈值；不猜 ETF 身份、季度均值、股票资产分母或穿透权重。当前仅隔离单基金目录完成全覆盖演练，生产全目录仍需离线采齐。|
|264|东财已披露最新报告的原始行业代码、名称、NAV 占比；110011样本为 GICS 分类，不能标成申万。无已证明发布日期，因此不输出或筛选发布日期。|
|265|F10 持有人报告的机构和个人占比。来源“内部持有”包括 ETF 联接持有，不能当员工持有；日期列语义不可靠，不猜报告/公告日期；亿元份额的四舍五入值不反推精确份数或户数。|
|269|Tushare 已披露持仓与同身份、同截止日股票收盘价，计算未复权价格年内收益；年初锚缺失的 A 股可提供同日真实总市值（万元乘10000为元）。没有年初价格不编上市以来收益；没有行业来源不编行业平均回报。|
|270|上交所官方 PCF 头。严格区分上一交易日 NAV/每篮 NAV/现金差额与当日预估现金；申赎单位、上限保持份。生效交易日不冒充发布时间，不能证明单位的现金替代比例省略。|
|274|Tushare 真实03/31、06/30、09/30、12/31单位净值观测及季度标签；仅季度末净值子集，不是完整季度财报，不提供虚构利润或未证实单位的资产净值。|

明确的当前快照没有合同日期字段时，携带日期条件会返回50301，而非忽略筛选。季度标签及曲线日期等有明确角色的日期保留本身含义，均不冒充发行人发布日期。

## 显式迁移与离线运行

在 backend 目录、项目虚拟环境和既有数据库配置下：

```sh
python -m app.open_data.product_worker migrate
python -m app.open_data.product_worker collect --kind pcf-components --fund-code 510300 --ts-code 510300.SH --start 2026-11-04 --end 2026-11-04
python -m app.open_data.product_worker collect --kind premium --fund-code 510300 --ts-code 510300.SH --start 2026-10-29 --end 2026-10-30
python -m app.open_data.product_worker collect --kind financial-indicators --fund-code 110011 --start 2024-01-01 --end 2026-11-05
python -m app.open_data.product_worker collect --kind asset-allocation --fund-code 110011 --end 2026-11-05
python -m app.open_data.product_worker collect --kind benchmark-components --fund-code 110011 --ts-code 110011.OF
python -m app.open_data.product_worker collect --kind manager-history --fund-code 110011 --ts-code 110011.OF
python -m app.open_data.product_worker collect --kind correlations --fund-code 110011 --ts-code 110011.OF --benchmark-code 000300 --benchmark-ts-code 000300.SH --start 2021-01-01 --end 2026-11-05
python -m app.open_data.product_worker collect --kind purchase-status --fund-code 110011 --end 2026-11-06
python -m app.open_data.product_worker collect --kind holder-ratios --fund-code 110011 --end 2026-11-06
python -m app.open_data.product_worker collect --kind investment-scope --fund-code 110011 --end 2026-11-06
python -m app.open_data.product_worker collect --kind company-profile --fund-code 110011 --end 2026-11-06
python -m app.open_data.product_worker collect --kind peer-returns --fund-code 110011 --end 2026-11-06
python -m app.open_data.product_worker collect --kind industry-exposure --fund-code 110011 --end 2026-11-06
python -m app.open_data.product_worker collect --kind pcf-header --fund-code 510300 --ts-code 510300.SH --end 2026-11-06
python -m app.open_data.product_worker collect --kind quarter-nav --fund-code 110011 --ts-code 110011.OF --start 2026-09-29 --end 2026-10-01
python -m app.open_data.product_worker collect --kind manager-directory --ttl-seconds 3600
python -m app.open_data.product_worker collect --kind manager-intervals --manager-id 30189744 --manager-name 张坤 --end 2026-11-06
python -m app.open_data.product_worker collect --kind holding-stock-returns --fund-code 161725 --ts-code 161725.SZ --end 2026-11-05
python -m app.open_data.product_worker collect --kind industry-reverse --product 262 --end 2026-11-06
python -m app.open_data.product_worker collect --kind industry-reverse --product 268 --end 2026-11-06
```

`migrations/20261105_open_fund_products.sql` 提供产品表 DDL；CLI migrate 另创建经理完整目录两表，并检查和建立二级索引。读取请求不会创建表。不要在生产只读验证账户上执行迁移。Tushare 复用项目凭据文件与跨进程限速器；本管道仅使用官方 HTTPS，拒绝代理和明文请求。密钥不进入记录、命令参数或日志。AKShare 仅在本 CLI 子进程执行。财务源逐个读取已披露年份，最多50年，模式变更、重复冲突、非半年度日期均拒绝落地。

经理目录固定按 MGRID 升序抓全声明分页，核验每页总数、页号、所有 ID 严格递增且唯一，目录新版本发布后旧版本不再用于消歧。实取4378人/88页，118组同名均会被拒绝。默认按拼音分页的源存在跨页重复，本轮仅4376个唯一 ID 的不完整目录被拒绝，未用去重伪装完整。目录最多保持1小时；最新目录过期则重新采集，不能回退旧的“姓名唯一”结果。

collector 种类及参数以 `collect --help` 为准。新产品应先用隔离本地数据库完成一次采集、映射和分页验收，再安排调度及历史回填。默认 TTL 为一天、实时最多30秒；过期不再读取为可用事实。

## 存储及查询口径

`open_fund_product_batch` 保存产品、范围、来源、算法版本、真实覆盖窗口、内容哈希和字段能力；`open_fund_product_fact` 保存严格合同字段及自然记录键。不接收私人持仓表、任意 SQL 或表达式。

- 同一批先全部校验，再原子写入。错误批次不能替代好数据。
- 数字拒绝 bool、NaN、Infinity；只有代码、报告类型或全空数值的“事实”拒绝。批次最多10万条/32MB，单条1MB；采集亦有总预算与翻页耗尽检查。
- 同一范围只选择能覆盖请求的一套来源和计算版本；不能拼不同来源或算法填空。
- 每批是其声明窗口的完整替换快照，不是增量补丁。新快照删除的行不从旧快照复活；有日期时先为请求窗口分配最新覆盖片段，再修订去重、筛选和分页。最新覆盖版本过期或缺筛选字段时返回品质错误，不回退仍有较长 TTL 的旧数据。
- 分页前逐批核对 manifest 的 row_count 与物理事实 COUNT，并限制总行数和字节数；任意页甚至其他基金的选中批次事实缺失，均不能返回缩小后的“成功”总数。
- 先按自然键取最新修订，再按全部筛选条件过滤、COUNT 和分页；修正过日期/标题的旧版本不能复活。不同基金相同公告自然标识不会互相去重。
- 显式日期查询要求来源窗口连续完整，且具备合同对应的真实日期字段；缺字段或覆盖不够为503品质错误。
- 未指定日期时，只返回经过验证的本地缓存事实及其真实总数，不承诺成立以来全历史。源元信息给出真实 `verified_windows`，不把有间断缓存宣称连续历史。空集合必须有经验证的来源批次；完全无来源、未迁移、过期、未知参数均不能返回假成功。

## 仍待验证来源：5项

这些合同具备固定 HTTPS provider 采集、字段单位映射、完整窗口校验、显式迁移及只读路由，但没有被计为已经验证可用的数据接口。

| 编号 | 上线前仍须满足的真实条件 |
|---|---|
|234|发行人确认的基金关联及变更日期；已有 ETF 联接的名称/基准启发式不能作为正式关联证据。|
|242|奖项颁发机构、奖项分类及真实奖项编码；不能哈希奖项名称冒充编码。|
|260|FOF 定期报告中的真实持基份额/金额/净值分母；股票持仓不能代替持基。|
|263/267|有权使用的概念分类及对应时点成份、披露期、权重和穿透算法；不能当前标签回灌历史。|

独立 provider 接入：`collect --kind configured-provider --manifest /secure/provider.json --product 265 ...`。manifest 固定批准 HTTPS host、endpoint、rows_path、业务成功条件、字段映射/明确单位乘数及自然键；认证头由环境变量名引用，禁止对照供应商作生产代理。先为实际响应写回归，再接入部署配置。只有上述来源与口径验证通过后，才允许将 gateway coverage 从 source_pending 改为 partial/full。

## 本地实数验收（2026-11-05）

未写真实数据库：官方 Tushare 一日510300 PCF完整300成份，本地 SQLite →已鉴权 Flask 源→网关合同输出，第二页100条、total300；缺发布日时日期查询被拒绝。两日真实 NAV/close 得到 -0.1602875461% 和0.1009628407% 溢价；真实110011半年度/年度4份财务指标和最新资产配置也走通同链。记录在本机 `/private/tmp/fund-source-gateway-rehearsal-20261105.json`。生产需独立执行迁移、凭据配置、首次回填及调度；本轮没有启动生产任务。

2026-11-06新增12个源产品的真实样本通过 parser→隔离 SQLite→严格源 envelope→网关字段白名单验证。其中10项有独立公开/官方实数来源；262/268是对真实行业资料在完整单基金隔离目录中的反向查询演练，不代表生产全部基金已采齐。269实测161725的15只披露持股：12只具有年初/截止收盘，可算价格YTD；3只年内IPO仅返回同日实数市值。270实测510300：上一交易日2026-11-04、NAV4.1785、每篮3760682.55元、上一日现金95477.55元、当日预估94551.55元、申赎单位900000份。目录、实际源样本和异常回归保存在 `backend/tests/fixtures/additional-*20261106.*`；不含凭据。
