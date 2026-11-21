# 抄表簿

骨架：抄表读数装载 → 估抄/实抄对齐 → 差额处置 → 账期封印。

- 前端 http://127.0.0.1:6010（勿用 6000，浏览器会拦）
- 接口 http://127.0.0.1:11000

## 启动

```powershell
cd backend
C:\Users\18435\AppData\Roaming\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 11000 --app-dir .
```

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 6010
```

## 覆盖率与封印口径

**证据架构（全局唯一）：正式键重放。** 唯一权威是正式实抄键集合 ∪ 应对齐（估抄）
键集合，直接由库内 `estimates` / `actuals` 的 `meter_id` 重放得到。扫描、
预检批、封印闸、封印批头、账期快照、未抄列表、总览全部走
`app/services/coverage.py` 的 `replay_coverage` 同一计算——
不存在任何旁路缓存（旧 `coverage_paint` 已删除，写入即拒）。

- **覆盖率算法**：实抄表键数 / 估抄表键数；同一表同账期多张单据合并为 1 个键。
  无估抄键时无分母，按 100% 处理（不假拒）。
- **数字与键集合自检**：覆盖率 100% ⇔ 未抄列表为空；已覆盖 + 未抄 = 估抄键数；
  已覆盖键 = 估抄键 ∩ 实抄键。任一不成立即视为「宣告撕裂」。
- **预检 → 批头 → 总览 → 未抄列表**：预检批与封印批头记录的覆盖数字/未抄清单
  必须能由当前正式键原样重放；不能重放则预检视为过期 / 撕裂，封印拒封并回滚，
  账期保持开放、未抄列表不动。禁止「数字绿了键集合仍分叉」。
- **门槛架构**：`settings.coverage_architecture` 只决定是否允许低覆盖封印。
  - `hard_gate`（默认）：`covered / estimate < threshold` → 预检不合格 → 拒封。
  - `allow_skip`：可低覆盖直封，但快照如实记 `skipped` 与真实覆盖数字/未抄列表，
    不伪造 `pass`，也不清空未抄列表。
- **过期判定**：装载使数据指纹变化、切换架构、或收紧阈值后，旧预检立即失效；
  `hard_gate` 下须重新扫描才能封印。`allow_skip` 不因过期预检假拒，快照退回 `skipped`。
- **并发 / 交错**：装载、对齐、预检、封印均走 `BEGIN IMMEDIATE` 写事务；
  封印在写锁内重放正式键并同事务写批头与账期快照，旧快照不得落成现行。
  同一账期最多一条 active 预检（库级唯一部分索引），并发双预检不会留下两套声明。
- **封印后核对**：未抄列表页 / 总览读取封印时留存的清单，同时与正式键重放核对；
  不一致则标记 `claim_tear` 并如实展示，不悄悄抹平。
- **防伪造**：非法架构/阈值被拒；损坏的架构配置回退 `hard_gate`；
  预检指纹或键集合不符视为过期/撕裂；封印前二次校验快照与闸门同口径，不一致直接拒封。

相关接口：`/api/coverage/{policy,scan,precheck,prechecks,missing,snapshot,gate}`。
前端页面：`/coverage`（预检）、`/coverage/missing`（缺失表）、`/seal`（封印闸）。

测试：`cd backend && python -m pytest tests -q`
