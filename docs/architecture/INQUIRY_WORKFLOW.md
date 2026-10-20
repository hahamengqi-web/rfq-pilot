# 询盘工作流运行说明

新入口为内部 Web 服务的 `/inquiries`。客户产品与售后客服通过单独的客户门户运行；内部 Web 的 `/customer` 仍返回 410，避免把客户入口挂到内部工作台。

## 实际运行流程

1. 邮件通过托管邮箱接入或 `.eml` 导入；完整原始字节先写 S3，失败则不推进收件游标。WhatsApp 由内部人员录入原消息编号、号码、时间和正文，不冒充已经接通 WhatsApp 官方 API。
2. 明确的询盘进入 RFQ 抽取；模糊意图、串线或后续回复进入人工意图核实。明显自动通知和非询盘邮件仅归档。每个首次出现的联系方式产生客户编号，尚未购买也有编号。
3. 检查产品、数量、单位、国家/地区和企业名称。启用自动补充开关后，邮件 RFQ 缺字段时不进入人工 RFQ 审核，而是直接向原始发件人发送固定模板补问；客户回复后合并原邮件重新抽取，每笔询盘最多 3 轮。企业名称足以识别主体时，背调可以并行执行，但交易字段补齐前不能进入报价。自动补问只能回复同一收件账户收到的原始发件人，报价邮件仍受收件人白名单控制。
4. 人工核实 RFQ 与企业/个人类型后，背调以 Plan-and-Execute 运行：Planner 根据原始客户消息制定受限步骤，Executor 调用官网受控爬取、地图、企业级海关与新闻接口，Evaluator 检查证据和缺口，最后由独立报告模型输出报告。步骤数有上限，未完成项交人工处理。
5. 报告版本同时保存原始消息快照、调查计划、执行日志、证据编号和结论；邮件原始字节仍以 S3 归档对象为准，报告快照不能替代原件。
6. 报告保存到主业务库，携带客户编号、询盘编号、报告版本、事实与证据对应关系。企业报告分别展示公开联系方式和询盘联系人；客户自述不会仅因出现于邮件就变成已核实事实。
7. 人工审批背调。有未核实项时必须说明为什么允许继续，审批不会抹去这些缺口。
8. 询盘回复 Agent 创建受限执行计划并调用产品目录工具，查询产品、库存、MOQ 和参考美元价格，然后暂停等待业务员确认最终销售条件。库存查询不等于预留库存。
9. 业务员录入确认的 SKU、数量、单位、最终销售单价、币种、费用、付款条件、贸易术语、交付安排及带时区的有效期。回复 Agent 调用确定性报价计算工具，再调用邮件草稿工具生成回复；金额不由模型计算，也不叠加未确认的利润或折扣。
10. 报价和邮件草稿保存为不可变快照。人工审批绑定该版本及内容哈希，审批后邮件工具才归档完整邮件并通过 SMTP 发送报价回复。演示模式只写入带标记的本地模拟邮箱，不实际外发。
11. 客户回复进入意图核实。明确同意后还需审核人选择回复证据并确认具体报价版本。客户编号、产品、数量、单价、费用、总价、交易条件、报价哈希及客户回复引用一起保存为 `acceptance` 记录。客户同意报价不等于已经付款或履约成交。

## 状态与暂停恢复

`InquiryService` 是业务状态的唯一写入边界，`InquiryWorker` 执行队列里的调查、计算和邮件任务。人工操作需要登录身份、预期版本和幂等键。审批仅允许 `admin` / `quote_reviewer`；普通员工和业务员可以核实 RFQ、录入谈判结果。

暂停时不保持模型连接或 Python 协程。恢复时从主业务库读取询盘和对应版本，调查中的短期上下文再从 Redis 读取。Redis 过期可重新调查，已经保存的报告和人工审批不依赖 Redis。

读取或计算任务中断后可恢复；SMTP 已开始但结果不明确时进入 `delivery_unknown`，不会自动重发。审核人先核对邮箱服务端记录，再记录已发送或未发送。批准发送后收到新客户消息会使旧待执行任务失效；发送进行中收到新消息则保留待核实状态。

## 数据保存位置

| 数据 | 保存位置 |
| --- | --- |
| 客户编号、客户自述资料、询盘、状态、审批、操作记录 | 主业务库的 `inquiry_customer`、`inquiry_case`、`inquiry_event` 等表 |
| 背调报告、证据快照、报价、发送记录、客户同意快照 | 主业务库 `inquiry_artifact`，按类型递增版本，不覆盖旧版 |
| 调查阶段、计划、工具执行结果、证据和步数 | Redis，按租户 + 询盘 + 调查版本隔离，默认 24 小时过期 |
| 邮件正文引用、消息编号、S3 定位信息 | 主业务库 `inquiry_message` |
| 收件及发件的完整 RFC822 字节、附件 | 私有 S3 bucket，以内容哈希定位；元数据带 SHA-256，可记录 S3 VersionId |
| IMAP 游标与重启后待执行任务 | 主业务库 `inquiry_mail_cursor` / `inquiry_job` |

生产使用项目已有的 MySQL 配置；本地开发可选择 SQLite。新表位于同一个所选业务数据库，不创建另一套长期记忆数据库。建表是增量的 `CREATE TABLE IF NOT EXISTS`，不删除旧表。

## 配置与启动

安装更新后的依赖：`uv sync --frozen`。在实际环境填写以下参数，不要把真实凭据写进示例文件：

```dotenv
INQUIRY_WORKFLOW_ENABLED=true
# 必须与内部身份系统的租户一致；单个服务进程处理一个配置租户
INQUIRY_TENANT_ID=default
DUE_DILIGENCE_REDIS_URL=redis://127.0.0.1:6379/3
DUE_DILIGENCE_MEMORY_TTL_SECONDS=86400
DUE_DILIGENCE_MAX_STEPS=6
DUE_DILIGENCE_SEARCH_PROVIDER=tavily
DUE_DILIGENCE_SEARCH_API_KEY=
DUE_DILIGENCE_MAPS_API_KEY=
DUE_DILIGENCE_CUSTOMS_API_KEY=
DUE_DILIGENCE_CUSTOMS_BASE_URL=
DUE_DILIGENCE_NEWS_API_KEY=
DUE_DILIGENCE_CRAWL_MAX_PAGES=12
DUE_DILIGENCE_CRAWL_MAX_DEPTH=2
DUE_DILIGENCE_REPORT_BASE_URL=https://api.deepseek.com
DUE_DILIGENCE_REPORT_MODEL=deepseek-v4-pro
DUE_DILIGENCE_REPORT_API_KEY=
DUE_DILIGENCE_REPORT_MAX_TOKENS=8192
DUE_DILIGENCE_REPORT_TIMEOUT_SECONDS=180
DUE_DILIGENCE_REPORT_MAX_RETRIES=2
DUE_DILIGENCE_REPORT_REPAIR_ATTEMPTS=1
DUE_DILIGENCE_REPORT_REASONING_EFFORT=high
DUE_DILIGENCE_REPORT_FALLBACK_MODEL=deepseek-flash
DUE_DILIGENCE_REPORT_FALLBACK_MAX_RETRIES=1
# 兼容旧配置；使用 DUE_DILIGENCE_SEARCH_API_KEY 后可以留空
TAVILY_API_KEY=
EMAIL_S3_BUCKET=
EMAIL_S3_ENDPOINT_URL=
EMAIL_S3_PREFIX=nanoclaw/email
AWS_REGION=us-east-1
# 使用云角色或 SDK 标准凭据链；本地可配置以下变量
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
EMAIL_S3_KMS_KEY_ID=
# 配置真实邮箱、凭据和 allowed_recipients 后再启用
INQUIRY_SMTP_ENABLED=false
NANOCLAW_EMAIL_CLARIFICATION_AUTO_REPLY_ENABLED=false
```

Planner、Evaluator 与 RFQ 抽取复用项目现有模型配置；填写 `DUE_DILIGENCE_REPORT_API_KEY` 后，最终报告改由 DeepSeek V4 Pro 生成，未填写时回退到主模型。报告接口强制 JSON 对象和输出上限；DeepSeek 默认采用 high 推理强度，瞬时失败进行指数退避，V4 Pro 重试耗尽后自动降级为 DeepSeek Flash，格式错误最多修复一次。实际使用模型与降级状态会写入报告元数据。S3 默认请求服务端 AES256 加密，设置 KMS Key 后改用 KMS；S3 兼容服务必须支持所选加密方式。bucket 需预先创建并配置适当访问权限，程序不会创建或开放 bucket。

### 背调治理与恢复

- 外部查询只能包含公司名称、企业域名、国家/城市、公开地址或注册编号；完整邮箱、电话、邮件正文、采购数量和价格不会发送给搜索、地图、新闻或海关服务。
- Planner 返回的查询文本不会直接执行。Executor 会按工具白名单重新生成脱敏查询。
- 官网与地图先用于公司主体交叉核验。评分达到 80 才自动执行海关和新闻调查；60–79 进入人工确认；低于 60 阻止背调审批并要求补充公司资料。
- `supported` 由代码验证。客户邮件、搜索摘要、抓取失败页面和国家级汇总海关数据不能单独证明公司事实。
- `inquiry_research_run` 保存完整检查点，`inquiry_research_step` 保存每一步状态，`inquiry_research_evidence` 保存证据质量与内容哈希。Redis 只作为可丢弃缓存。
- `(tenant_id, inquiry_id, research_revision)` 是背调任务唯一键。进程中断后从 MySQL 最后一个检查点继续，不重新执行已经完成的步骤。

通过现有邮箱管理界面配置托管邮箱并开启 `NANOCLAW_EMAIL_MANAGED_ACCOUNTS_ENABLED`。WhatsApp 来源没有天然的发件邮箱，录入谈判条件时选择已配置的发送邮箱编号。报价接收邮箱必须在该邮箱的 `allowed_recipients` 中。

启动 `python main.py`，进入内部工作台的“询盘工作台”。登录开启时沿用内部登录；登录未开启时仅允许已有本机管理权限路径。不要把没有身份认证的内部页面直接作为客户门户。

启用新流程后，旧的 RFQ 确认即生成报价、旧报价批准发送工具和旧 SMTP 入队入口会返回 `inquiry_workflow_required`。原有工具可继续进行只读查询，但不能跳过背调或谈判直接发送新报价。旧队列不会被自动迁移或继续发送。

## 当前边界

- 同一个规范化邮箱或 WhatsApp 号码可复用客户编号；不同邮箱、不同渠道尚不自动认定为同一客户。历史 `ops_customer` 联系方式是独立且部分加密的模型，当前不会猜测匹配或伪造历史购买记录。需要先明确可信映射和解密接口，才能迁移旧客户关系。
- 历史查询覆盖本流程已保存的报告及客户同意报价；它不将“同意报价”标记成实际订单、付款或履约记录。
- 询盘回复 Agent 是受状态机约束的计划与工具执行器：只能读取已审批报告、查询产品、调用确定性报价计算并生成草稿；它不能自行确定最终售价、审批回复或绕过邮件门禁。背调与回复两个 Agent 通过持久化业务状态交接，不进行无边界自主协商。
- 新流程不自动议价、不承诺联系人代表权限、不预留库存；对附带 PDF/Excel 的询盘只保存原件与附件元数据，目前不自动解析附件中的 RFQ。
- 待人工阶段需要在工作台查看，本次没有增加微信/QQ 审批通知或直接操作 WhatsApp 的集成。
- 测试使用临时业务库和模拟 Redis、S3、模型及邮箱。真实 MySQL、S3、Redis、网络调查和邮箱需在部署环境完成连通性与端到端验收。

## 验证

`python -m pytest test/test_inquiry_workflow.py -q` 覆盖暂停恢复、编号复用、跨租户隔离、版本冲突、幂等操作、审批门禁、固定净价计算、原件归档、调查证据、补充邮件与不确定发送处理。完整回归使用 `python -m pytest -q`。
