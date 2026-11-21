# 凉城证据簿

用于城市降温与节水行动的观测口径、暴露人口、保护缺口和比较分析。它保留资料修订来源，区分缺测与零风险，支持行动组合的假设比较与带审核证据的本地发布。分析是描述性证据，不自动证明行动具有因果效果。

运行环境为 Python 3.11 及以上，仅使用标准库和自带 SQLite，无第三方依赖。资料不需联网。命令从本目录执行；Windows 可将 `python` 换成已安装的 Python 路径。

```bash
python -m unittest discover -s tests -v
python -m compileall -q climate_ledger
python acceptance.py
python -m climate_ledger --database city.sqlite3 ingest examples/city.json
python -m climate_ledger --database city.sqlite3 analyze examples/exposure.json
python -m climate_ledger --database city.sqlite3 compare examples/study.json
python -m climate_ledger --database city.sqlite3 plan examples/portfolio.json
python -m climate_ledger --database city.sqlite3 equity examples/exposure.json
python -m climate_ledger --database city.sqlite3 priorities examples/exposure.json
python -m climate_ledger --database city.sqlite3 diagnose examples/exposure.json
python -m climate_ledger --database city.sqlite3 audit
python -m climate_ledger --database city.sqlite3 ingest examples/forecast.json
python -m climate_ledger --database city.sqlite3 decision-preview examples/decision.json
python -m climate_ledger --database city.sqlite3 decision-confirm examples/decision.json --select cool-hill
python -m climate_ledger --database city.sqlite3 decision-activate july-heat-reserve --actions cool-hill --at 2026-07-02T08:00
python -m climate_ledger --database city.sqlite3 decision-cancel july-heat-reserve --actions shared-river --at 2026-07-01T10:00
python -m climate_ledger --database city.sqlite3 decision-show july-heat-reserve
python -m climate_ledger --database city.sqlite3 decision-verify july-heat-reserve
```

`examples/` 全部是本地合成城市资料，不代表真实居民、观测站或研究结论。`city.json` 中资料包由 `id`、`source` 和 `records` 构成，每条资料含 `kind` 与 `data`。资料类型包括行政区、指标、站点、人口、行动及逐日观测。精确字段以该合成资料及 `formats.py` 的 CSV 列契约为准。

新对象从修订1开始；同修订只能重送相同内容，高修订由同一资料来源提交。整个资料包通过业务关联检查后才入账；一个对象不合格会拒绝整包。资料包编号绑定原始内容摘要，重送不新增水位，同编号更换内容会拒绝。观测身份是站点、指标与日期，其他对象使用稳定编号。此版本不支持删除资料、改变来源所有权或并行来源竞选。

所有时间区间首尾均包含，按本地业务日期解释，没有小时级时区换算。站点在有效期内归属一个固定行政区，指定支持指标和覆盖权重。同区同指标同时有效站点权重总和不能超过1。有效覆盖是有效观测的绝对权重和，缺站不能通过重新归一化假装完整；通过覆盖门槛后才以有效权重均值形成区日值。温度支持 C/F，水量支持 L/m3，能源支持 kWh/MWh，转换到指标标准单位后检查合理值域。`valid` 可用，`suspect` 排除，`missing` 必须配 `null`。高异常方向使用大于等于阈值，低异常方向使用小于等于阈值。

人口按行政区、人群、有效期登记，人口为零是已知资料，同组有效期不重叠。该行政区所有已登记群体都被视为分析需要的固定分组；某日少一组即人口资料不完整。新增具有不同历史覆盖的新群体需要先明确全期分母，不能把缺失默认为零。脆弱度是1到10的业务权重，不是患病概率。

行动可以处于计划、生效、暂停或结束状态，只有生效且在有效期内的行动参与保护。每个行动对应一个行政区、指标和设施。同设施相交日期不能同时承诺多项生效行动。先按行动优先级、编号分配，再按人群脆弱度与群体名分配，保护数不超过尚未保护的有效人口。没有危险的日期不消耗保护容量。当前仅以互斥人群计数估算保护，不记录个人身份。

暴露结果保留逐日来源、质量、人口与行动依据；不完整区日的暴露是未知，汇总只标明已知部分并清空完整口径比例。加权负担是未保护人数、阈值距离与人群脆弱度的乘积，不跨不同指标量纲相加。缺观测会结束当前可确认的连续事件，但终点标为未知，不能宣称危险已经结束。公平性结果利用指标合理值域形成负担上下界，区间不是置信区间，分母缺失时不作确定排序。

比较研究在前后两期内分别按同日、天气差容限寻找对照，同阶段的对照区日最多使用一次。对照存在行动、前期行动区已启用、后期行动区尚未启用、人口为零或资料不完整的日期会排除并说明原因。估计是后期和前期配对单位人口负担差的变化，配对不足不出数值。候选行动组合以实际联合保护结果比较，遵守预算和设施承诺，已保护群体不重复计效。组合结果只是假设，不修改已入账行动；一次最多比较16个候选。

预报是来源发布的一组联合情景，每个版本声明发布时刻、适用日期、涉及区与指标、各情景概率（之和为1）及该情景下各区日读数，并要求区日网格完整。同一情景中的各区变化属于共同气象条件过程，只能在来源给出的情景上整体评估，不能拆成各区独立概率后拼出从未发布的组合。预报不是观测，不填补暴露分析的缺测区日。决策时点只能引用发布时刻不晚于决策时刻的版本；后来发布、预测日期更早的版本不能倒流进入旧决定。预报修订不改写已经确认的组合及其所依据的概率。

时点决策按当时已知资料选择并确认适应行动组合。业务方在决策条件中给出预算、候选行动的预留费、启用费、取消截止时刻、逾期取消费用，以及哪些联合情景属于需要保障的严重气象条件和这些情景下各群体的最低保护比例。引擎在预算与设施承诺下检查保障，再在可行组合中比较概率加权的未保护负担；没有可行方案时显式说明保障缺口，不把未知人口或缺少情景的区日当成零风险。不同区的候选可共用设施，但同日容量和已确认预留不能重复出售，保护人数仍受真实群体人数与脆弱度分配限制。`decision-preview` 同时预览多个组合也不占用名额；`decision-confirm` 以当时剩余预算和设施占用重新判断，两个预览不能花掉同一笔资金。

确认后的资源以事件溯源记录，原决定不可改写。取消会释放尚未到截止时刻的可退预留费，逾期取消则记取消费用，已发生费用不会被抹掉；只有预留没有启用的行动仍是承诺资源。新预报到达后可以用 `decision-adjust` 比较调整方案，调整先结算旧预留的取消成本再确认新组合，并保留完整确认顺序。实际气象条件到来后，`decision-verify` 只以有效观测核对各群体实际危险与已兑现服务：只预留未启用或没有实际服务的行动不计实际保护，观测缺失时不借预报计算确定结果，也不识别兑现情景。核验按同一决定并列给出当时预计负担、费用承诺与实际未保护负担，并把差异分解为气象条件偏差、人口修订和未兑现服务三个来源；预报表现按情景给出误差与危险方向命中，不做事后重选。相关分析不能把预测驱动的选择当作随机分组。决策事件带哈希链并纳入 `audit` 与 `backup`，恢复后当时已知资料、确认顺序和实际核验依据都可复查。


`draft` 根据分析条件生成草稿并绑定入账水位、资料清单和摘要，独立审核人用摘要确认后才允许 `publish`。分析不完整、无有效人口或研究配对不足时不能审核发布。资料更新使所有旧草稿过期；旧发布内容仍保留，并标明不再是当前资料。专题发布需要提供期望的前序编号，避免覆盖别人刚签发的版本。`export` 以原子替换写出发布文件，`verify` 检查内容摘要；这是文件完整性检查，不是数字签名或外部身份认证。

`reconcile` 在相同分析条件下核对两个入账水位，说明哪些资料修订和区日结果变化，不能将该差异当作时间趋势。`export-csv` 导出资料快照，`ingest-csv` 可读单类资料，数组列使用 JSON 数组。`backup` 生成可直接重新打开的一致数据库副本，拒绝覆盖已有文件。`audit` 检查历史资料关联、修订顺序和发布证据链。数据账是本机受信文件；本工具没有网络账号系统，不应用于替代多人访问权限控制。

目录中 `observatory/` 负责指标、站点和观测有效性，`residents.py` 负责人口分母，`adaptation.py` 负责行动资格和保护容量，`forecast.py` 负责联合情景预报与情景天气，`decision.py`、`decisions.py` 负责时点组合推演与承诺事件，`verification.py` 负责事后核验与差异归因，`analysis/` 组织暴露、研究、组合与差异分析，`publication.py` 负责审核与专题发布，`ledger.py` 维护不可变修订，`application.py` 连接业务事务。测试按这些业务边界组织，包含跨边界更正、失败回滚、发布冲突、预报时点纪律、设施重复出售拦截和备份恢复。
