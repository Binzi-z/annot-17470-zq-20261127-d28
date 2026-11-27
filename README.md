# 紧急名单的时间窗口差异审计

题面见 `task/PROMPT.txt`。两份虚构离线应急名单在夏令时**回拨日**挂钟文本
相同，但同一本地时间存在两个 UTC 映射，两个班次因此拿到不同的时段权限。
本仓库用 **Windows Python 3.12 标准库**（无第三方依赖、不依赖 zoneinfo/
tzdata）对两版名单做差异审计。

## 运行

```powershell
python -X utf8 -m unittest discover -s tests   # 65 个测试
python -X utf8 audit_report.py                 # 场景审计 + 穷举 + 证据哈希
python -X utf8 audit_report.py --big           # 追加六万条单组变更实测
```

## 模块

| 文件 | 职责 |
| --- | --- |
| `audit/zones.py` | 输入中**固定的合成时间规则表**；整数分钟时区运算、回拨 fold=0/1 双映射、春令时前跳缺口 |
| `audit/model.py` | 记录模型、`anchor`/`re_root` 别名身份代簿记、输入装载与校验 |
| `audit/engine.py` | 重放引擎：grant/deny/revoke/re_root 语义、可验证片段切分、**最少支撑记录**、扫描计数 |
| `audit/diffaudit.py` | 两版扩大/收窄/空区间审计、组级摘要预筛、定向审计、sha256 规范化证据 |
| `audit/enumerate.py` | 独立穷举：小整数分钟 × 合法身份代 × 两种 fold 映射 |
| `audit/bigdata.py` | 确定性生成每版 60000 业务记录，只改一个组，实测全量 vs 索引扫描条数 |
| `data/synthetic_input.json` | 手工合成场景（回拨双映射、联合覆盖、换根、拒绝、撤销、重叠、空区间） |
| `audit_report.py` | 命令行报告；证据清单写入 `artifacts/evidence_manifest.json` |

## 题面要点如何落实

- **两种相同本地时间的不同 UTC 映射**：回拨日本地 `01:xx` 小时出现两次，
  `fold=0` 落旧偏移 `+60`、`fold=1` 落新偏移 `+0`，相差 60 分钟；同一本地
  分钟两种映射都可双向回验（`test_zones.py`、`test_enumerate.py`）。
- **两个实际独立签署人联合覆盖**：仅当两个签名在签署时刻挂在**不同 root**
  下才成立；同 root 两别名、同一别名两次均不成立（`test_engine.py`）。
- **别名换根 / 显式拒绝 / 晚到撤销**：分别制造 `reroot`/`deny`/`revoke`
  空洞，连续时间被切成若干可验证片段；旧身份代在换根时刻起立即失效，
  由旧代签名的撤销也被忽略（`data/synthetic_input.json` 的 shift-B/D/E）。
- **最少支撑记录**：按记录数升序穷举首个并集铺满的组合，并对扩大时段做
  逐条必要性校验，重叠的多余记录不进支撑集。
- **不遗漏重叠或可能为空的区间**：半开整数分钟区间代数 `subtract_many`
  保留全部切分；整组被 deny 盖满时标注 `empty_in`，无差异也返回空列表；
  测试断言每个 horizon 内片段与空洞互不重叠且分钟数恰好铺满。
- **独立穷举**：春前跳日与秋回拨日各 1440 个连续小整数分钟，覆盖
  0/1/2 个合法 fold 三类；身份代做 C(n,2) 全配对，按 root 独立性分类。
- **六万条只改一个组的实测扫描条数**：全量路径每版扫描 60002 条；
  组级摘要预筛只命中 `G0500`，索引重放每版仅扫描该组 60 条（合计 120）。
- **完整重放与旧版证据一致**：规范化 JSON + sha256 生成每版快照与清单
  哈希，二次完整重放逐字节一致；篡改任一记录会改变对应版本快照哈希。
