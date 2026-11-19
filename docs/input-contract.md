# 翻新家电保修暴露与准备金充足性分析资料契约

以下为项目本地合成资料，不代表真实居民、机构、金额或政策。所有数据为 UTF-8 JSON Lines，每行是一个完整对象；金额采用整数分，null 表示未知或不适用，不能自动等同于零。时间戳含 +08:00 偏移，日期为当地业务日。known_at 或 reported_at 控制资料何时可得，业务发生时间不意味着当时已经获知。

## 业务含义

months从sold_at按自然月计算保修届满；申请occurred_at确定是否发生在保期间，reported_at确定何时获知。cohort_complete=false表示只有选中过来的销售或赔案记录，不能据此估计总体发病率。重复症状只有明确previous_claim且在repeat_days内才重开同案，缺关联不凭相似文本自动合并。退款和换机结束原保障但不删除已有支出，换机如形成新保障需新销售段依据。paid维修从未来准备需求中剔除但仍属于已实现保修成本，pending诊断不能凭未支付视为零。recovery_confirmed=false的追偿不得抵减确定现金需求。资金事实available是总可用额，other_commitment已占用部分，方案之间不允许重复认领。

## 原始记录

### sales

| 字段 | 含义 |
| --- | --- |
| id | 销售保障段 |
| device | 实物身份，重售可再次出现 |
| batch | 翻新批次 |
| kind | 设备类别 |
| age_years | 销售时机龄 |
| sold_at | 保障起点 |
| observed_until | 观察截点 |
| contract | 保障条款 |
| cohort_complete | 是否拥有完整销售名册 |
| known_at | 获知时间 |

### repairs

| 字段 | 含义 |
| --- | --- |
| id | 处置身份 |
| claim | 保修申请 |
| action | 维修退款换机 |
| cost_cents | 实际费用 |
| paid | 是否支付 |
| component_group | 可能共同风险零件组 |
| occurred_at | 实际处置时点 |
| known_at | 获知时点 |

### claims

| 字段 | 含义 |
| --- | --- |
| id | 保修申请 |
| sale | 销售保障段 |
| occurred_at | 症状发生 |
| reported_at | 申报到达 |
| symptom | 症状 |
| previous_claim | 明确关联旧案，空表示未确认 |
| decision | 资料所知在保判断 |
| closed_at | 结案时间，未结为空 |

### contracts

| 字段 | 含义 |
| --- | --- |
| id | 保障条款 |
| months | 保障月数 |
| repeat_days | 相同故障重新打开旧案的天数 |
| refund_ends_cover | 退款是否结束保障 |
| replacement_ends_cover | 换机是否结束原保障 |
| covered_symptoms | 保障症状 |

### costs

| 字段 | 含义 |
| --- | --- |
| id | 成本或追偿证据 |
| component_group | 适用零件组 |
| valid_from | 生效日 |
| valid_to | 终止日不含 |
| repair_cost_cents | 当前标准维修价 |
| recovery_cents | 拟追偿单次额 |
| recovery_confirmed | 是否确认追偿 |
| known_at | 证据获知 |

### funds

| 字段 | 含义 |
| --- | --- |
| id | 资金事实 |
| kind | 可用资金或其他既有占用 |
| cents | 金额分 |
| scope | 适用保障范围 |
| known_at | 获知时点 |

