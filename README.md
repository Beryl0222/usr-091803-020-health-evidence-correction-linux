# 健康科普证据纠错

本项目服务于健康科普内容与证据审校。医学说法、引用资料、专家审校和更正版本共同维护公众可见结论。 系统应支持清晰的领域对象、事件记录和责任追溯，运行入口提供稳定的健康检查，便于本地联调和运维巡检。

运行 `python3 service.py --check` 可检查基础配置；执行 `python3 service.py --port 8000` 后访问 `/health` 可以确认服务身份。

## 领域模块

- `domain.py` —— 纯领域对象与规则，不依赖外部存储：
  - 每条医学说法（`Claim`）必须关联至少一条证据（指南 / 论文 / 个人经验），关联（`ClaimLink`）上标明立场（支持 / 反驳 / 尚无定论）与适用人群；
  - 说法结论由证据推导（`derive_claim_status`）：指南与论文优先，仅有个人经验时一律“尚无定论”，研究证据立场不一致时为“证据分歧”；
  - 公众曾看到的每个版本（`ContentVersion`）都是不可变快照，永久保留；
  - 评论中的个体健康信息识别（`contains_personal_health_info`）为可替换的确定性规则。
- `health_service.py` —— 业务规则与责任追溯（`HealthEvidenceService`）：
  - 跨平台同步按内容身份（`content_identity`，可显式指定）去重，只登记新的平台发布记录；
  - 修订 / 撤稿必须写明原因，旧版本保留，结论变化时自动向看过旧版本的订阅者发送更正（`Correction` / `Notification`）；
  - 专家复核与利益冲突声明只增不改；审核人只能复核获分配的医学领域；
  - 含个体健康信息的评论只进入受限队列，不进入公开资料库；
  - `content_as_of` 按任意发布日期还原当时内容，`diff_summary` 给出与当前结论之间、两侧均附证据出处（含研究限制与适用人群）的差异摘要；
  - 所有变更写入事件日志（`events()`），记录操作者以便追溯。

## 测试

`npm test`（等价于 `python3 -m unittest -v service_contract test_domain`）运行全部测试：
`service_contract` 保障运行入口契约，`test_domain` 覆盖上述领域规则及“更年期脑萎缩”纠错场景。
