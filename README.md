# 银发服务体验认证

服务用于记录不同银发人群的真实体验证据、适用范围和认证变更，避免笼统宣传掩盖差异。

运行 `python3 service.py --check` 可核对服务配置；执行 `python3 service.py --port 8000` 后访问 `/health` 可确认服务身份。`npm test` 运行全部契约测试。

## 认证规则如何落地

- **经同意的测试分层**：分层固定为四个维度——年龄阶段（低龄/中龄/高龄）、健康状况（健康/慢病/行动受限）、居住状态（同住/独居/机构）、辅助需求（无需/部分/全程）。参与者须就具体服务方案给予未撤回的同意，证据才计入覆盖。
- **证据五项信号**：关键任务、可达性障碍、人员响应、价格披露、中途退出（安全原因退出一票阻断）。每个等级须声明适用分层，且每个分层达到最少独立参与者人数；低龄健康者的良好体验不能外推到独居、行动受限或慢病人群。
- **原始反馈不可改写**：证据、反馈、事件、决定均为只增记录，进入按方案串联的 SHA-256 哈希链；更正以“追加更正”方式留痕。供应商没有反馈写入入口，认证人员视图提供 `ledger_verification` 校验链完整性。
- **版本变更与证据复用**：场地/内容/人员/价格变更按受影响维度判定，未受影响且同意有效的证据标记为 `reused`，受影响维度逐分层列入复测清单；旧证书不自动覆盖新版本。
- **严重事件与整改复验**：事件上报即暂停相关等级并撤销现有证书；暂停只能在整改复验通过后解除，申诉成立也不能替代复验。阻断性缺陷不得用例外放行，重要缺陷的例外须含理由、补偿措施和有效期。
- **申诉回避**：裁决人若参与过原评审或原事件/缺陷记录，服务端返回 409 强制回避。
- **三类视图**：
  - `GET /api/plans/{id}/consumer`：适用人群、有效期、已验证能力；
  - `GET /api/plans/{id}/supplier`：脱敏缺陷清单（不含参与者、证据、报告人标识）；
  - `GET /api/plans/{id}/certifier`：从等级追溯方案版本、代表性覆盖、证据来源版本、例外决定、事件、整改复验与哈希链校验。

## HTTP 接口（摘要）

所有业务接口通过 `X-User-Id` 与 `X-User-Role`（`supplier` / `certifier` / `participant`）识别身份。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/api/plans` | 供应商登记方案与等级声明 |
| POST | `/api/plans/{id}/versions` | 登记方案/场地变更（自动推导复测维度） |
| POST | `/api/participants` | 登记分层参与者 |
| POST | `/api/participants/{id}/consent` | 参与者对方案给予同意 |
| POST | `/api/consents/{id}/withdraw` | 撤回同意（证据保留可查但不计覆盖） |
| POST | `/api/plans/{id}/evidence` | 认证人员录入试用证据（须先有有效同意） |
| POST | `/api/evidence/{id}/feedback` | 参与者本人追加原始反馈或更正 |
| POST | `/api/plans/{id}/defects` | 登记缺陷（minor/major/blocking） |
| POST | `/api/exceptions` | 对重要缺陷作出限期例外决定 |
| POST | `/api/plans/{id}/incidents` | 上报严重事件（立即暂停+撤销证书） |
| POST | `/api/plans/{id}/rectifications` | 供应商提交整改 |
| POST | `/api/reverifications` | 认证人员整改复验 |
| POST | `/api/plans/{id}/decisions` | 作出认证/拒绝决定 |
| POST | `/api/appeals`、`/api/appeals/{id}/rule` | 供应商申诉与独立裁决 |
| GET | `/api/plans/{id}/levels/{level}/coverage` | 代表性覆盖矩阵 |
| GET | `/api/plans/{id}/levels/{level}/reuse` | 证据复用与复测清单 |
| GET | `/api/plans/{id}/{consumer,supplier,certifier}` | 三类视图 |

数据存储为进程内结构，重启即清空；`ALLOW_RESET=1` 时开放 `POST /internal/reset` 供测试使用。
