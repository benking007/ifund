# 基金开放产品：生产服务、采集、覆盖与验收

基金域 api-223 至 api-274 共52项已有真实原生源或明确版本的独立计算绑定，运行字段只列已实现白名单，均保留完整参考合同供对照。**绑定完成不等于全字段、全历史或生产数据已回填。** 本轮不写生产数据库，首次迁移、凭证和采集水位仍需按下列步骤执行；不满足身份、窗口、单位或完整性时返回503，不能用代码加空值冒充可用。

## 独立生产服务

`open_data_service.py` 只注册已有 PAT→JWT 交换和 `/api/open-data/v1` 只读源，不注册个人仓位、注册、登录、AI分析或后台采集；Waitress四线程、请求体1MB。`SECRET_KEY` 至少32字符，弱密钥阻止启动。MySQL运行服务默认不迁移，原完整应用的自动DDL仅显式 `IFUND_AUTO_MIGRATE=1` 开启；SQLite开发保留原行为。

```sh
# 在 backend 目录，使用生产配置和有DDL权限的独立迁移身份，只运行一次
python -m app.open_data.product_worker migrate --base-schema
# 切回读取服务身份；后台采集使用独立写入身份
python open_data_service.py
```

`collect` 仍接受 `--ttl-seconds`，但不再使用它，留给尚未更新的调度器。已有库跑一次 `migrate` 会删掉 7 张表上的 `valid_until` 列（也可看 `backend/migrations/20261111_drop_open_fund_valid_until.sql`）。新代码要在迁移之后才能写库。

显式迁移包括原基础/auth/业务 schema，以及产品批次/事实、经理完整目录和完整概念目录共14张新表（含尚未披露/不适用证据及基础采集水位）及索引。`migrate` 不带 `--base-schema` 可升级已有基础库。读取不会创建表。`GET /api/open-data/v1/_ready` 必须JWT，验证表结构并报告 product scopes、观测时间和实际批次水位；仅空schema返回503，不把存活health当数据ready。整体ready仅表示存在已发布缓存，单个产品仍按其完整窗口核验。数据不设到期时刻，调用方按 `observed_at` 自行判断新旧。

仓库根 `Dockerfile.open-data` 使用Python3.11最小依赖、非root10001、目录 `/srv/ifund`，不安装MCP或模型SDK。构建时提供 named context `collection_runtime` 指向 ai-platform 的 `services/fin_data_runtime`；镜像内运行时模块在 `/opt/collection-runtime`。只给collector设置 `PYTHONPATH=/opt/collection-runtime`、`OPEN_COLLECTION_PYTHON=/usr/local/bin/python`。共享 `/runtime` 需可写且归10001；源服务不启动scheduler。

## 采集计划与凭证

机器可执行计划：`config/open-data/collection-plan.json`（47个job、固定argv、无shell、超时≤3600秒、明确依赖和universe）。每个 per-scope collector 最后一行输出 `quality.status=ready,complete,row_count,verified_empty,coverage_start/end,source,method_version`；非0退出或缺质量声明不能推进水位。`universe --kind ...` 从受治理canonical主档生成准确代码/角色，不能用名字猜ETF或忽略未知身份。

- MySQL使用既有 `DB_BACKEND=mysql` 和 `IFUND_DB_HOST/PORT/NAME/USER/PASSWORD`。迁移、source和collector使用各自最小权限；PAT exchange沿用原实现，需要 `api_tokens.last_used_at` 更新权限。
- Tushare只经官方HTTPS；`TUSHARE_TOKEN_FILE` 只读secret可为纯40–80位hex或旧 `TUSHARE_TOKEN=...` 格式，不进命令参数和日志；沿用跨进程总闸及客户端20次/分。
- `FIN_DATA_OPEN_BASE_URL=http://fin-data:8002/api/open-data/v1` 与 `FIN_DATA_OPEN_TOKEN` 供collector读取固定市场目录、概念成员和精确ETF角色，header为 `X-Fin-Data-Token`，无query secret、无任意URL或重定向。
- 基准相关性和风险绩效要求 `IFUND_OPEN_BENCHMARK_ROLES_FILE` 指向只读JSON；样本 `fund-benchmark-roles.example.json` 的510300经原始资料明确为沪深300指数→000300.SH。必须与主档 `benchmark` 原文完全一致。复合基准不能擅自拿单指数代替，缺映射只使相关job失败，不影响其他源。
- 报告collector先重新读取完整官方公告索引，再使用不可变公告ID/标题/份额/解析版本的本地解析缓存，避免每次重新下载解析原PDF。`IFUND_OPEN_REPORT_CACHE_DIR` 默认 `/runtime/ifund-report-cache`，`IFUND_OPEN_REPORT_CACHE_MAX_BYTES` 默认128MB，允许8MB–16GB、LRU/90天淘汰。全目录生产建议独立可写cache卷、按容量设置上限；缓存故障可重新抓取原始固定PDF，绝不进入HTTP热路径。
- 当前申赎列表每次返回全部约2.8万基金，因此生产只跑一个 `purchase-status-all` job（240秒），一个响应一次原子落地，索引筛选基金。保留最新2批，每次有界清理最多5个旧批，避免每天800万冗余行。

## 新增和完善口径

|API|真实来源/方法及边界|
|---|---|
|234|原始定期报告的明确同级份额代码表，relationType=10；不猜ETF联接、变更生效日期或未知关联类型。|
|238|全量当前申赎列表，申购上限元及原文状态remark；不是变更事件历史，不输出虚构resumeDate/changeType。|
|241|F10投资范围原文；不猜内部分类、比例上限或生效日。|
|242|颁奖机构中国证券报第22届金牛奖完整28产品名单，发行人档案精确代码与名称匹配；实际发布日期，不哈希奖项内部ID，不把星级评级当获奖。未接入其他届次不承诺完整获奖史。|
|243|真实认购/申购/赎回原始阶梯、金额/持有期、开闭端点及%/元，不采用渠道优惠价；`fee-history` 另抓完整公告索引，严格原始管理/托管费率变更表与真实公告/执行日期。未知公告格式失败，不从当前费率猜历史；日期查询按公告日。|
|244|真实基金→公司外键、公司名、简称和成立日，不生成未知评价分数。|
|248|来源明确“同类平均”曲线与基金共同日期重基，覆盖1周/1/3/6月/1/3/5年/今年以来实际区间。缺分类成员与分母就不编人数、同类排名、内部分类及平均风险值。|
|251|完整当前经理目录先消歧；当前全部持管基金曲线的独立等权篮子，增加本月/今年以来。不是经理个人历史实绩，不把曲线日当公布日，不支持未知发布日期历史筛选。|
|252|原始F10完整管理团队任期列表、明确开始/结束/“至今”、真实任期百分比收益；联合团队不拆个人。所有active基金各自缓存齐全后才全局物化，姓名筛选/COUNT/分页。无原始发布日期不输出date，不拿抓取日期冒充公布日。|
|253|`aligned-adjusted-nav-benchmark-calendar-risk-v3-rf0-252`：实际复权NAV与明确基准的共同日期；几何/简单年化、最大回撤、波动/下行波动、beta、夏普/索提诺/卡玛/欧米伽、跟踪误差/信息/特雷诺/M2。无风险0、年化252、样本标准差，少于20收益不提供风险比率。>10日间隔须基准也无中间报价，>20日拒绝；未知VaR/专有归因模型不硬套。|
|258/261|批量最多20基金，未指定季度保留各基金实际最新季度，SQL全局排序/COUNT/分页，不猜统一季度或把缺源基金删掉。原始万股/万元乘10000转股/元。|
|260|发行人实际FOF基金投资表，份额份、公允价值元、NAV占比%；保留明确前十披露边界，联合份额共用组合，不按各份额NAV错误分拆。|
|262/268|完整平台active基金目录全部有264来源，才能离线反查；空行业页必须同一期正NAV+发行人明确“未持有股票”，四舍五入0.00%不够。只原始行业码/最新NAV比；268仅matchType4，未知分母、穿透、ETF身份筛选拒绝。|
|263/267|真实年报/中报**所有股票**明细×固定fin-data当前THS N类完整成员证据，独立披露NAV权重；香港/境外证券明确不在THS-A域，绝不用季报前十替全组合。4表原子完整目录，真实零覆盖/物理缺行/新增基金/陈旧新版本分别严格判断。THS不是CLS，未知C01不猜别名；不是历史PIT。267类型1/3表示本方法的最新完整年报/中报，类型2季度尚无完整组合证据时503。|
|264|最新明确披露行业码、名称、NAV占比；保持源GICS/其他原始分类，不改申万、不猜发布日期或总资产分母。|
|265|原始年报/中报明确单份额表，户数、户均份额、机构/个人精确份数及占比、另表明确从业人员份额及占比、真实公布和报告日期。多份额不能准确识别列时拒绝；旧F10“内部”含ETF联接，不能当员工。|
|266|原始季度报告期初、申购、赎回、期末份额，严格份额列；不从期末差额猜申赎流量。|
|269|实际披露股票身份+实际年初/截止收盘，未复权价格YTD；年内IPO缺起点不猜上市收益，可仅提供同日真实市值（万元转元）。|
|270/271|官方SSE PCF头和官方沪深篮子；昨NAV/每篮NAV/现金差额与当日估计分开，份/元；有效交易日不是publishDate。源整数合同不强截断小数比例。|
|274|实际季度PDF 3.1财务表，主代码与份额列、人民币元、真实公布日/报告期；已实现利润、当期利润、每份加权利润、净资产/单位NAV、公允价值变化差额。不把半年/全年累计利润冒充单季。|

223公告、231两融、240明确加权基准、247货币万份收益、254指数行情、255相关性、259资产比例、273半年/年度财务继续原严格源。224–230场内行情和技术由fin-data负责。单位NAV、累计NAV、复权NAV独立；货币万份收益不是NAV，PCF/托管复核/报告日期各守语义。

## 完整性和验收

每批是对应完整窗口的替换，不是补丁。只读最新权威版本并检查能力，不能复活旧值；物理COUNT/32MB/10万行门禁在筛选分页前执行。新快照删除的事实不从旧版补回。全目录反查要求每个成员都有来源，包括有独立证据的真实零，不用不完整基金集合假称全市场。

新增测试夹具为去凭据原始发行人季度/半年PDF、费用阶梯HTML和调整公告、金牛奖原始页面、经理历史页、官方933条复权NAV/929条指数行情、64支完整A股组合及808条当前N概念关系/395概念目录。测试走parser→隔离SQLite→真实源envelope，并验证不同份额利润、真实单位/日期、丢行、漏基金、未知概念、非N过滤、未知发布日期拒绝。概念全目录只在完整单基金隔离目录验证，不宣称7957只生产基金已经采齐。

已知仍需生产作业：有DDL权限身份显式迁移、完整canonical主档及角色回填、各job首次覆盖/水位、报告/行情历史窗口及完整行业/概念目录。必须看质量结果与真实source503状态，不因合同存在就宣称全字段/全历史/全目录验收完毕。

采集状态另外区分：完整官方索引已核验但当前窗口**尚未披露**时输出 `quality.status=not_disclosed, complete=true, verified_empty=false，并附实际完整索引/响应的evidence哈希（complete指核验该来源完成，非产品数据完成）`，不发布事实、不推进ready水位；网络/协议/身份/完整性失败为非0退出。当前完整奖项名单中无获奖、完整费率公告窗口无变更，才是有源证明的真实empty。持股收益 universe 是 `fund_details.position_stock>0` 的明确已观测适格子集，未知/非正配置数量明示；基准 universe 明示 configured_subset、母体和未配置数量，不承诺全基金基准均可运行。


## 空库启动与持续基础采集

`base-master`先从官方`fund_basic`完整分页取得场内/场外主档和`fund_company`。保留已有精确角色；同代码多角色仅明确唯一场内角色或已有主角色可选，不做字典序猜测；歧义数在质量报告中展示。公司只用精确唯一名称/简称关联。

`base-nav`按官方`nav_date`逐日完整分页，冷启动最近35日，之后滚动7日；每个日期独立事务提交，联网不持有事务，仅全作业成功才发布水位，内存不保留全市场长历史。单位、累计、复权净值分别存储，不把货币收益作净值。长历史需分段回填，单次最长366日：

```sh
python -m app.open_data.product_worker collect --kind base-master
python -m app.open_data.product_worker collect --kind base-nav --fund-code 110011 --start 2025-01-01 --end 2025-12-31
```

其他持续producer：`base-dividends`真实每份现金（不猜税口径），`base-splits`发行页面完整拆分表（明确1:X方向、原日期及字面无拆分），`base-holdings --holding-type stock|bond`原披露NAV比例/万股/万元，`base-tenures`完整管理团队实际任免，`base-returns`实际Tushare复权净值自然月锚。股票/债券采集分别隔离失败；异常或缺表不是零持仓。254使用去重的明确基准角色，并非拿基金代码猜指数。

基础水位`open_fund_base_watermark`和源数据同事务，保留最近两版。`/_ready`读取实际非空的基础或产品水位，不把空数据库、仅建表或失败任务视为ready。首次35日净值不声称覆盖成立以来；回填依旧只由离线worker进行。

## 分母、未披露和ETF关联

全目录行业/概念反查逐只解释母体：真实已披露进入适格子集；完整官方公告索引证明尚未披露、或主档成立日晚于报告期的不适用，单独证据ledger记录源、哈希、观测时刻及计数。这些状态不写财务事实、不伪装零暴露。抓取/解析失败继续阻塞。仅聚合作业显式`accept_verified_absence:true`接受这些证据依赖。

142基金正向使用ETF治理角色、真实完整年度股票明细公允价值/报告总资产，独立当前SW1方法；同时保留原披露NAV比，绝不相互替换。143使用官方CLS所属板块完整观察×ETF完整年/中报NAV比例，原生CLS身份才填C01；这是持仓关联，不能据此称专门跟踪概念主题，也不回灌历史PIT。正向fundCodes与反向industryCodes/conceptCodes均读取同一完整治理ETF代，支持年报下限过滤和索引分页。缺完整报告、角色或成员快照均50301。

## 源服务PAT运维（无公网创建入口）

```sh
python -m app.open_data.service_pat create --create-service-user svc-open-platform --name open-platform-reader --output /tmp/source-reader.pat
python -m app.open_data.service_pat revoke --user-id 1 --token-id 1
```

也可`create --user-id N`明确归属已有用户。DB仅存哈希/前缀，明文仅一次写新0600文件；禁止覆盖或符号链接，stdout仅无密钥元数据。容器非root用户不能直接写主机root的0700目录：先写容器`/tmp`，由运维`docker cp`到受限secret目录，校验权限后删除容器临时文件/一次性容器，不把完整token经过stdout。source DB角色SELECT及仅`api_tokens.last_used_at` UPDATE，迁移和采集使用独立权限。撤销阻止再次交换，已签发的旧协议JWT保留原1小时时效。

`base-categories`独立采集完整东方财富原分类，按exact canonical代码补充细分类，不能用Tushare粗“混合型”识别FOF，更不按名字猜ETF。主档更新不覆盖已有细分类，FOF采集依赖真实分类水位；NAV白名单统一，货币收益仍排除。
生产reader默认`IFUND_OPEN_REQUIRE_BASE_WATERMARK=1`，显式Python运行同样应设置，旧完整应用默认兼容关闭。


2026-11-06最终实数补验：全市场2026-11-05 NAV按日完整分页写入23534条，405个货币/未核验类型明确排除；分类原表28287行、28061 exact canonical匹配、25空分类保留未知、4957 canonical缺/未知细分类，实际1347 FOF scope。142实际年报342股×真实SW1输出29行业；143最新2026中报339股×4629条CLS关系输出552板块关联。二者生产collector→JWT ifund HTTP分页及下限过滤均200；552关联不表示552个被动跟踪主题。原始报告指针保留到不可变PDF ID。报告先比较报告期再比较公布/明确修订版本，摘要不能冒充完整报告，历史同期间修订先去重。


## 完整ETF正反向发布（schemaVersion 6）

47个作业新增`etf-directory`及`industry-etf-reverse`/`concept-etf-reverse`。目录由fin-data官方etf_basic完整快照（hash/原始行数/观测时刻）及全部listed治理角色交叉核验；实样1733只，等值OF/SZ别名仅在显式交易所和全部其他字段一致时去重，绝不截断或猜代码。四张新增表分别存目录、版本元信息、逐成员状态、索引事实，不把百万事实塞入单列。

```sh
python -m app.open_data.product_worker migrate --base-schema
python -m app.open_data.product_worker collect --kind etf-directory
python -m app.open_data.product_worker universe --kind verified_etf_funds
# controller 按每个真实成员逐scope持续采集并保留checkpoint：
python -m app.open_data.product_worker collect --kind industry-etf-mapping --fund-code 510300 --end 2026-11-06
python -m app.open_data.product_worker collect --kind concept-etf-mapping --fund-code 510300 --end 2026-11-06
python -m app.open_data.product_worker collect --kind industry-etf-reverse
python -m app.open_data.product_worker collect --kind concept-etf-reverse
```

只有每个ETF均有实际报告或真实未披露/不适用ledger，才能原子发布；任何missing/failure不冒充0。DOB不适用证据须与官方成立日期相符。正反向共用同一version-vector cutoff；新增成员使旧分母失效，但未改变成员复用其scope checkpoint。限定直接披露A股股票×当前SW1/CLS，不把境外证券、基金穿透或关联主题冒充专门跟踪；这些方法范围在合同说明列明。

输入不变不重写事实；发布保留2代，staging事实保留2代且旧批次头保留PDF来源档案。读取先核全代物理COUNT/分母/小型scope digest，再索引COUNT/分页；本页UTF8字节总量须≤2MB，才fetchall。

迁移角色另需**TRIGGER**权限。两条固定数据库触发器绑定INSERT的fund/行业或概念/权重与JSON，已发布代禁止late INSERT、事实全部UPDATE禁止；DELETE仅用于版本GC，缺行读时拒绝。发布者SAVEPOINT内实际探测三种非法操作且全部回滚，只有成功证明守卫的新v2代可读；reader/collector不需TRIGGER/DDL权限，不运行请求期DDL。MySQL/MariaDB实数小数权重及JWT双向HTTP已验证。

MySQL启用binlog且`log_bin_trust_function_creators=0`时，单独TRIGGER权限不足，可能返回1419。上线前由DBA按数据库安全策略配置`log_bin_trust_function_creators=1`，或由有权限的DBA预先创建上述已审核触发器后再运行迁移。CLI仅输出固定诊断`MYSQL_TRIGGER_BINLOG_DBA_CONFIGURATION_REQUIRED`，不打印原始异常或凭证，也不会自动执行`SET GLOBAL`或提升权限。

完整目录和代码能力已打通；生产首次全量报告/成员采集仍由作业持续完成，采齐前明确显示warming/not_ready，不宣称本机单基金演练就是全国全量已采齐。
