# 用户每日额度与今日剩余

## 2026-10-24 英语纳管补充

用户本次明确要求修复英语每日额度，因此下文历史R5“英语排除”不再适用于当前版本。英语已纳入管理，专用扣费仍在英语扩展内处理，策略读取同一中央表；没有接入通用consume。每日额度次日生效、今日剩余即时生效、0和恢复默认、幂等与跨日退款均已验证。

新增管理员备注与用户搜索；英语昵称适配器仅在管理员认证通过后读取既有原子状态文件，未设置昵称显示账号编号，不从昵称关联账号。普通用户不能读取名单或修改备注、额度。

新release `20261024-english-quota-share` 已部署，43项Python测试与管理端构建、lint和浏览器表单验证通过；中央扩展19项、英语Linux全量361项及线上18项合成账号HTTPS检查通过。41个其他运行服务、12个其他域名及受保护配置不变；三个相关服务重启，中央核心源码与其他产品适配器未改。详细证据见相邻项目 `../english/docs/quota-share-fix-20261024.md`。

Status: deployed and verified

## 意图与范围

管理员按用户、应用分别设置长期每日额度与当日剩余额度，测试通过后部署到111.228.44.125。各应用使用服务器当前代码为基线；小C英语(seth-english)的代码、现有额度逻辑、接口与进程保持原状。

## 验收

- R1/A1: 每日配置持久化，未配置沿用产品默认(棋牌50，其余现有产品5)，0为有效配置，可恢复默认。修改先结算当天旧配置，从下一次UTC日切生效，不改变当天剩余。
- R2/A2: 今日设置为绝对剩余值，原子更新余额与日切日期，用户当天尚未访问时管理员补额不被下次请求重置。
- R3/A3: 管理界面分开展示、编辑每日配置与今日剩余；明确UTC日切(北京时间08:00)、长期设置下次日切生效。保存成功重新读取服务端状态，失败不假报成功。
- R4/A4: 四个通用扣费产品及棋牌、厨房桥接均读取相同持久配置；保留现有客户端响应合同、并发不超扣、预留/提交/退款幂等、跨日退款不增加新日余额。
- R5/A5: seth-english明确显示未纳入本次每日配置管理，保留原有接口响应；不改英语文件/DB行/服务进程。
- R6/A6: 新管理接口每次验证最新数据库管理员身份、目标权限、严格非负整数0–100000，防跨应用写入；普通用户不能改每日/今日额度。通用扣费不接受负cost，不绕过专用桥接。
- R7/A7: 生产先私有一致性备份、记录源码与服务基线；隔离测试不消耗真实用户额度、不调用付费模型。发布失败回退代码/配置，不用共享数据库备份覆盖发布后用户数据。

## 设计与影响

新增独立中央额度服务，使用现有SSO验证函数和SQLite；仅精确接管通用consume、旧管理员quota setter及新增/admin/quotas路径，不重启核心SSO/英语。新增user_app_quota_policies(user_id,app_id,daily_quota)与管理员审计表，不改旧权限表列顺序。读取/修改在BEGIN IMMEDIATE锁后采样UTC日期，今天结算后再保存次日配置。

棋牌/厨房桥接从线上实际版本下载到deploy/adapters，记录SHA-256后作增量修改。棋牌当天授权表继续表示当天授权总量，与长期daily_quota独立；厨房桥接读取长期配置，default_daily_credits保留产品默认5且现有四个响应字段保持原样；仅新管理接口返回daily_quota，保持既有客户端兼容。厨房授权写入遵循现有write_guard事务；英语行禁止新服务结算/改写。

前端：GET /admin/quotas/users -> 原用户结构扩展apps字段quota,daily_quota,daily_quota_override,default_daily_quota,quota_reset_date,quota_managed。POST /admin/quotas/users/{id}/apps/{app}/daily {daily_quota:number|null}；POST对应/remaining {quota:number}。旧/auth/users/{id}/apps/{app}/quota保持{quota}绝对余额合同。

## 规范与验证

Personal Dev OS security/testing/commenting/release/review/quality-gate；项目AGENTS.md。测试覆盖默认、配置、恢复默认、0、跨日、首次访问前补额、当日额度不被长期配置覆盖、并发、专用桥接退款、厨房触发器、英语排除、失效/撤权/非管理员/非法输入。前端lint/typecheck/build与实际浏览器保存流程；独立审查及上线HTTPS端到端验收。

## 发布证据

2026-10-23 已发布 `20261023T135628Z-daily-quotas`，管理入口 https://admin.lazylady.cc。发布包 SHA-256：`77b3c5f6b8cd9655706f179269c0dd8e91ccafc06fe377ad12de71e5bf077c46`。

- 新增38项测试与原桥接72项回归，本地和服务器生产依赖环境均全部通过；涵盖R1/R2/R4/R5/R6的日切、补额、并发、退款及权限合同。
- R3：lint、TypeScript/Vite构建通过；浏览器验证整数/上限/0/默认值、提交防重、失败重试、英语只读和移动端显示。现存未使用catch变量警告与大JS包提示不影响构建。
- R7：发布/回退沙箱演练通过，回退后新增数据库记录仍保留。线上一致性备份及发布清单在 `/root/sso-quota-releases/20261023T135628Z-daily-quotas/`，目录0700、数据库0600。
- 线上真实HTTPS冒烟73项通过：6应用每日/今日、新旧setter实际路由、通用扣费、非管理员拒绝、输入校验、专用桥接与英语排除。测试使用随机临时身份，清理成功，未调用付费模型。
- 线上5个静态文件与本地dist逐个SHA-256相同；额度服务、两桥接、SSO、英语、棋牌业务及Nginx均active。部署桥接与本地deploy/adapters、侧车与本地quota_service.py逐个SHA-256相同。
- 核心SSO/英语源码SHA及SSO、英语、棋牌业务PID在发布前后保持不变。共享数据库私有备份与凭证不入库。

## 运维

额度侧车 `sso-quota.service` 监听127.0.0.1:5191；Nginx精确接管新增管理API、通用consume及非英语旧setter。代码位于 `/opt/sso-quota/current`，前端位于 `/var/www/sso-admin`，均为版本目录软链。棋牌仍由原桥接服务运行，厨房在原桥接模块中增量更新。

回退命令（服务器root执行）：

```sh
python3 /root/sso-quota-test-runtime/payload/deploy/release.py --rollback 20261023T135628Z-daily-quotas
```

只回退源码、前端、服务和路由，不恢复共享数据库；旧版本不会读取新增每日策略。再次发布前，从当前线上版本更新source-baseline及release.py的EXPECTED并审查增量，禁止覆盖线上后续变更。

Capture: project memory updated。Workflow delta: project memory updated。
