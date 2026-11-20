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

全局只贯彻一种架构，由 `settings.coverage_architecture` 决定（设置页可切换）：

| 架构 | 低覆盖日能否封印 | 快照 `coverage_state` |
| --- | --- | --- |
| `hard_gate` 覆盖率硬闸（默认） | 否，阈值不足拒封 | `pass`（达标后封印） |
| `allow_skip` 允许低覆盖直封 | 是，可跳过覆盖率 | `skipped`；若已扫过则如实写实算值 |

- **覆盖率算法**：实抄表键数 / 估抄表键数；同一表同账期多张单据合并为 1 个键。
  无估抄键时无分母，按 100% 处理（不假拒）。
- **联动链路**：扫描 → 预检批（含 coverage 与缺失表清单）→ 封印闸 → 账期快照 → 总览 / 缺失表页，
  全部走 `app/services/coverage.py` 同一计算。
- **过期判定**：装载使数据指纹变化、切换架构、或收紧阈值后，旧预检立即失效；
  `hard_gate` 下须重新扫描才能封印。`allow_skip` 不会因过期预检假拒，但快照退回 `skipped`。
- **并发**：装载、对齐、封印均走 `BEGIN IMMEDIATE` 写事务，封印在写锁内重算覆盖率，
  不会用过期覆盖率半截落封；重复封印只有一个成功。
- **防伪造**：非法架构/阈值被拒；损坏的架构配置回退 `hard_gate`；预检指纹不符视为过期；
  封印前二次校验快照与闸门同口径，不一致直接拒封。

相关接口：`/api/coverage/{policy,scan,precheck,prechecks,missing,snapshot,gate}`。
前端页面：`/coverage`（预检）、`/coverage/missing`（缺失表）、`/seal`（封印闸）。

测试：`cd backend && .venv/bin/python -m pytest tests -q`
