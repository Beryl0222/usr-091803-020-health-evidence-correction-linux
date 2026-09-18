# 健康科普证据纠错

本项目服务于健康科普内容与证据审校。医学说法、引用资料、专家审校和更正版本共同维护公众可见结论。 系统应支持清晰的领域对象、事件记录和责任追溯，运行入口提供稳定的健康检查，便于本地联调和运维巡检。

运行 `python3 service.py --check` 可检查基础配置；执行 `python3 service.py --port 8000` 后访问 `/health` 可以确认服务身份。

## 领域规则

- **说法逐条关联证据**：每条医学说法必须分别关联指南、论文或个人经验，并标记支持、反驳、尚无定论与适用人群；研究限制（如样本自报、未全面检测激素）随引用展示，不可省略。
- **修订留痕与更正**：编辑修订必须写明原因；公众曾看到的版本全部保留；结论被修改或删除时，自动向相关订阅者发送带证据出处的更正。
- **跨平台同步去重**：以内容身份（content_uid）为准，同一内容来自不同平台只登记平台标识，不重复创建。
- **复核与声明不可覆盖**：专家复核与利益冲突声明仅可追加；审核人只能处理获分配的医学领域。
- **评论隐私**：含个体健康信息的评论一律不进入公开资料库，公开库不带作者标识。
- **时间还原**：可按任意发布日期还原当时内容，并给出与当前结论之间带证据出处的差异摘要。
- **责任追溯**：所有改变公众可见结论的操作写入事件记录（`GET /events`）。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/contents` | 发布内容（内联证据与说法） |
| GET | `/contents/{uid}` | 当前内容与全部历史版本 |
| POST | `/contents/{uid}/revisions` | 修订（必填 `reason`，返回更正结果） |
| GET | `/contents/{uid}/at?date=YYYY-MM-DD` | 还原当日公众看到的版本 |
| GET | `/contents/{uid}/diff?since=YYYY-MM-DD` | 当时与当前结论的差异摘要（带证据出处） |
| POST | `/sync` | 跨平台同步，按内容身份去重 |
| POST | `/reviewers` `/reviews` `/coi-declarations` | 审核人、专家复核、利益冲突声明 |
| POST | `/comments` | 评论（可标记含个体健康信息） |
| GET | `/public-dataset` | 公开资料库（已排除个体健康信息） |
| POST | `/subscribers`，GET `/subscribers/{id}/corrections` | 订阅与更正收件箱 |
| GET | `/events` | 事件记录 |

## 测试

`python3 -m unittest service_contract test_domain test_api` 或 `npm test`。
