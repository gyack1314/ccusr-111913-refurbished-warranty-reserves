# 翻新家电保修暴露与准备金充足性分析

本项目把回收业务的本地合成资料串成一条可核查的链路：资料进入 → 批次风险 → 申请处置 → 准备金认定 → 后续充足性核对。它只形成内部准备金认定，不实际付款，也不提供财务合规结论。合成资料没有现实居民或企业信息，不能作为真实政策、交易或认证依据。

## 环境

使用 Python 3.11。科学计算依赖版本固定在 requirements.txt；已有相同依赖环境时无需重复安装。新环境执行 `python3 -m pip install -r requirements.txt`。

## 快速开始

所有命令在项目根目录执行，默认读取 `data/`，默认截点为 `2026-09-02T23:59:59+08:00`（可由 `--cutoff` 覆盖）。

```bash
python3 -m warranty_reserve materials    # 资料进入：文件摘要、字段、截点可见性与价格证据
python3 -m warranty_reserve risk         # 批次风险、结局构成与向后滚动验证
python3 -m warranty_reserve reserve      # 准备金构成、资金缺口与支出尾部
python3 -m warranty_reserve scenarios --file examples/new_batches.json
python3 -m warranty_reserve month-end --out outputs/month.json
python3 -m warranty_reserve change --month outputs/month.json --kind release \
  --amount-cents 500 --reason "供应商直接换机" --source-ids S123 --out outputs/month2.json
python3 -m warranty_reserve reopen --month outputs/month.json --original A006 \
  --new-id A006-R --original-amount-cents 15000 --amount-cents 24000 --reason "售后补录维修"
python3 -m warranty_reserve validate     # 只看向后滚动验证
python3 -m warranty_reserve all          # 一次输出上述主要摘要
```

每个子命令都支持 `--json` 输出机器可读结果、`--data-root 目录` 切换到独立资料根（目录下需有 `data/manifest.json`）、`--scope` 指定范围。模拟类参数：`--simulations`、`--common-mode-probability`、`--common-mode-hit-rate`、`--severity-sigma`。

## 口径摘要

- **资料时点**：销售、申报、处置、成本、资金都按 `known_at`/`reported_at` 裁到截点；业务发生时间不等于当时已知。
- **名册完整性**：批次风险率只用 `cohort_complete=true` 的保障段做分母；转来的不完整资料仍可见、可计价，但不冒充完整销售名册。
- **工单关联**：重复报修必须由 `previous_claim` 且在 `repeat_days` 内确认；症状文本相似不自动合并。一段保障只取一个主结局，不按工单行数重复计段。
- **结局**：首次故障、同一问题重复维修、退款退出、换机退出、确认不在保、待决维修、待诊断分别列示。是否在保以售后 `decision` 为准；合同期间与症状范围只做一致性核对，不反向改写售后认定。退款/换机按实物结束原保障，但不删除已发生费用；未结案不等于无故障，未决维修照常计入风险。
- **实物隔离**：同一设备可重售；后售段只按自己的销售日与合同起保，不用前段历史承接未来风险。资料中重叠的后售段会单列提示。
- **准备金**：已付维修只展示、不再计提；已报未付无论结案与否都计入。仍在保的保障段分两类计提：从未有效申报的按合同月龄首次故障风险；已有维修的只按“维修后再出险”的弱证据上限，避免把首次故障率重复套在已出险设备上。只在确认范围内抵减追偿，未确认追偿单列备忘。
- **价格与证据**：维修价、诊断结论、追偿只能取截点前已知记录。售后认定与合同推导不一致时保留售后认定，并单列为一致性核对项，不反向改写。
- **尾部**：模拟包含零件组共同冲击与费用波动，同时给出独立对照，避免默认完全独立；分位数只用于资金安全观察，不是精算置信声明。
- **认定承接**：月度认定冻结模型版本、价格来源、保障范围和截点；后续变化必须带来源，以释放/补提列示；重开旧案保留原认定关联，重复挂接被拒绝。
- **资金**：可用额是总额，已有占用从余额扣除；各批次/新批次方案共享同一余额，不能重复认领。

## 数据与本地状态

数据保存在 `data/`，字段含义和适用条件见 [资料契约](docs/input-contract.md)。`data/manifest.json` 声明每份原始文件的身份和摘要；新增资料可在独立目录保存新清单，不应覆盖用于历史解释的原始文件。运行时写入使用 `.local/` 或 `outputs/`（已在 .gitignore 中），不要改写 `data/` 下的原始记录；无需外网账号、访问令牌或真实支付服务。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m support.inspect_data
```

`tests/test_materials.py` 只检查运输格式与摘要；`tests/test_analysis.py` 覆盖领域口径：截点可见性、名册完整性、结局互斥、重复关联、追偿确认、资金去重、情景台数缩放、滚动验证的实物隔离，以及认定冻结、释放/补提和重开案件关联。后者是业务口径验证，但仍不证明合成资料能外推到真实业务。

## 当前合成资料的阅读要点

- 截点仍有 10 段在保，且都已有有效申报与维修；因此首次故障 IBNR 为 0，只有维修后再出险的限制性备抵。这是资料事实，不是“没有风险”。
- 45 段退款退出、45 段不在保（其中 30 段合同推导本应在保）；后者保留售后认定并列入一致性核对，提醒转来资料与完整名册可能不一致。
- 15 台设备后售段与前段保修期重叠；风险按段分别计量，不能让后段替前段承接未来。
- 转移资料的细节导致滚动验证只有 2 个可用切点，只能说明方向一致，不能当作模型可靠性证明。

## 容器使用

`docker build -t recovery-data .` 准备锁定依赖环境。`docker run --rm recovery-data` 运行资料体检；`docker run --rm recovery-data python -m unittest discover -s tests -v` 运行测试；`docker run --rm recovery-data python -m warranty_reserve all` 运行分析摘要。镜像基础已固定摘要。容器与测试检查的是资料格式与合成口径，不能称为真实业务验收。
