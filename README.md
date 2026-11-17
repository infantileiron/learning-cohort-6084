# 常青 · 企业学习留存队列

按首学周核对真实回访，在成熟观察周生成可追溯的干预名单。原生HTML/CSS/JS前端，Python真实计算，独立五服务Docker。

## 真实业务与模块

模拟客户常青企业大学，运营组服务200名学员。原工作流从学习平台导出 `常青_学习事件_20261115.xlsx`，用工作簿手工找首学日期、统计每周回访，再复制缺席名单到周会纪要。三个痛点：同周多事件重复计人数、截断历史把老学员误算新客、未观察完整的周被当成零留存。上游是学习事件；本项目替代队列计算、成熟判断与成员核对；下游由学习负责人签核辅导安排。

五个业务模块：事件台账（录入/完整字段修订）、队列配置、留存矩阵（冻结版本/成熟遮罩/成员来源）、干预清单（站内草案/管理员签核/导出）、课程规则（可发布前置关系/按版本重放/学分与证书沿革）。业务实体为Event、Cohort、Intervention、RuleVersion、RuleRun、Credit、Certificate，另有User和Audit。

|角色|权限与典型动作|
|---|---|
|admin|业务全部读写、审计、独占干预签核与规则发布|
|analyst|事件录入修订、提交计算、起草清单、导出；创建规则草稿、发起/重试重算|
|viewer|业务只读与下载；后端禁止写入和计算提交|

## 启动与访问

```sh
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```

前端 [localhost:18213](http://localhost:18213)，后端 [localhost:19213/docs](http://localhost:19213/docs)，MySQL宿主端口17713。演示邮箱 `admin@demo.local`、`analyst@demo.local`、`viewer@demo.local`，密码均 `Demo123!`。`SEED_DEMO=true`启动自动seed；自然键只插入缺失记录，不覆盖人工修订。种子包含200学员、2026-09-21起8个首学周、重复周内回访和截至日以后的未来事件。

## 一天的工作剧本

1. 08:50 viewer检查CQ-001到CQ-200的事件台账。
2. 09:00 analyst核对学习平台导出的事件唯一编号与上海本地时间。
3. 09:10 录入 `CQ-201-WEB`、学员CQ-201、2026-10-06 09:00、数据素养课程。
4. 09:20 如需修订，填记录ID和完整字段；同事件编号原内容重传不会新增。
5. 09:35 打开“课程规则”检查当前发布版本与逐人缺项；需要时 analyst 起草新规则、admin 发布并触发重算。
6. 09:40 建“11月首学队列复核”：起周2026-09-21、止周2026-11-09、截至完整业务日2026-11-14、跟踪5周。
7. 09:45 worker计算，点击刷新；核对分母、W0到W5、未成熟遮罩。
8. 10:05 点击成熟列“查看未回访”，写“核对排班后安排站内学习辅导”，保存冻结成员。
9. 10:15 admin签核；10:25 viewer下载 `learning-cohort-<id>.json` 和 `learning-members-<id>.json`。

## 计算口径与边界

事件输入必须带时区；后端归一化UTC字符串，分周统一换算Asia/Shanghai。日期截至日表示已完整收集至上海本地日结束，包含该日全天；当日尚未结束时应选择上一完整业务日。演示默认截至2026-11-14；这是业务数据完整性约定，不自动探测采集watermark。未来事件从分析中排除并报告数量。ISO周从周一到周日，用周一日期作为队列键，跨年显示ISO年/周。

先扫描**所有截至日前历史**，每学员首学周=min(其事件周)，再按方案首学起止周过滤，不将窗口前老学员重新分配。第k周分母为首学队列唯一学员数，分子为该队列在目标周至少回访一次的唯一学员数；同周多事件只计1次。目标周周日≤截至日才成熟；成熟率=分子/分母，未成熟率保留null，成熟无回访为0。

提交时冻结所有事件、配置和checksum。修订原事件不会改变旧矩阵；需新建方案。无截至日前事件、窗口中无首学队列，任务置failed并说明。日期须为ISO周一且起周≤止周≤截至日；首学窗口最多一年，跟踪1–8周。事件同编号同内容重传返回原记录；同编号内容冲突409，需显式修订。数据库唯一键防重复。

干预清单只能选已成功方案、成熟非W0列且有未回访成员的单元格。成员由服务端从冻结结果生成，忽略客户端自报名单；未成熟/空清单409。签核后说明和名单不可覆盖。仅站内保留行动记录，不向学员外发消息。

## 课程前置、学分与证书

只看回访无法判断课程是否按顺序学完。课程规则是一份可发布的“课程 + 前置关系”清单：

- 规则校验拒绝未知课程、自环、重复先修和任何环（DFS 三色检测），并按稳定拓扑序给出补学顺序。
- 发布即冻结：旧发布版自动归档，仍可随时按旧版本重放核查；新版本另行重算。
- 学分只在事件发生时判定：该事件的全部先修课程必须在**严格更早**时刻已取得学分（UTC 比较，含秒）。
  同刻不算先修；前置达标前的提前学习记录保留但不追认，之后重新学习才计。
- 每个学分带事件编号与先修事件证据；每张证书带规则版本、校验和、截至日、学分依据和 SHA-256 证明。
- 逐人输出已获学分、缺项、不追认记录和按拓扑序的补学清单（可立即开始 / 需先完成第几步）。
- 规则换版或事件更正会自动创建重算；重算后失格者原证撤销（保留撤销时间、原因、依据，公开接口仍可核查），
  恢复达标且规则未变则另发新证；同一学员任一时刻最多一张 active 证。
- 重算状态：pending（待核算）→ running（重放历史，带进度）→ done（核算完成）/ failed（可重试）/ superseded（作废）。
  只由“当前发布版本 + 最新截至日”的重算改写证书；更早截至日或已归档版本的重放只做历史回看。
- 事件写入、规则发布与颁证共用账本行锁（`system_locks` + `SELECT … FOR UPDATE`）；核算在锁内重验输入校验和，
  任何创建后的事件更正或规则切换都会使旧任务作废，绝不签发基于失效学分的证书。
- worker 启动把残留 running 任务退回 pending，重试沿用同一任务与幂等证书约束，不会重复发证。

API：`POST/GET /rules`、`POST /rules/{id}/publish`、`POST/GET /runs`、`GET /runs/{id}`、
`POST /runs/{id}/retry`、`POST /runs/{id}/process`（同步核算，便于本地验证）、`GET /runs/{id}/learners/{learner}`、
`GET /certificates`、`GET /verify/{public_id}`（免登录公开核查，撤销后仍返回历史与撤销依据）。

种子默认发布“常青课程前置 v1”(数据素养 → 服务沟通 → 安全规范)，并提交截至 2026-11-14 的重算任务。

## 工程与本地开发

|部分|实现|
|---|---|
|后端|Python3.11、FastAPI、SQLAlchemy、PyJWT、PyMySQL|
|前端|原生ES modules、CSS、Nginx1.27|
|持久化|MySQL8、Redis7、命名产物卷|
|worker|Redis任务队列、DB状态、心跳文件、启动恢复queued/running|

目录：backend/src下routes/controllers/services/models/schemas/middlewares/config/utils/constants；frontend/src下api/stores/pages/components/common/hooks/utils/constants/types/router；backend/scripts/seed_demo.py；backend/migrations/001_initial.sql、002_course_rules.sql。所有计算/校验在Python，前端仅展示/交互。共有表格、表单字段、状态标签组件，form hook和HTML转义工具；请求ID与安全响应头两个中间件，写入业务审计和全局错误处理。

共享枚举：ROLES、STATES、COURSES、STAGES、RULE_STATUSES、RUN_STATES在后端src/constants/enums.py与前端src/constants/enums.js；schema/business.py约束课程与配置，models/entities.py存储状态。实例数据与队列名隔离；backend/worker依赖db和Redis健康，worker还等backend，frontend等backend。数据库、Redis和/artifacts均命名卷；五服务各有healthcheck。worker每轮刷新/tmp/heartbeat，结果在共享/artifacts。Nginx `/api/`去前缀代理到后端根路由。

环境变量见.env.example：COMPOSE_PROJECT_NAME、FRONTEND_PORT、BACKEND_PORT、DB_PORT、DB_PASSWORD、JWT_SECRET、SEED_DEMO、NETWORK_SUBNET。子网10.253.114.0/24规避本机默认地址池耗尽，移机可调整。部署时替换演示密码与JWT密钥。

本地开发可只起db/redis，backend安装requirements、设置DATABASE_URL/REDIS_URL/JWT_SECRET后 `uvicorn src.main:app --reload`；前端使用Nginx同源代理。ORM首次建表，MySQL DDL在migrations内；无在线版本迁移执行器。`requirements-dev.txt`（pytest/httpx）与 `backend/tests` 提供重放、并发与证书沿革测试：`cd backend && python -m pytest tests -q`。清理本项目：`docker compose down -v --remove-orphans`。

范围：逐条表单/API输入，无CSV批导、删除或课程级独立留存；队列留存仍以“学习回访”统一定义，课程顺序与学分另由课程规则重放。证书为站内业务凭证（非外部认证），不发送外发消息；重算为单 worker 串行，靠账本锁与幂等约束保证不重复颁证，不宣称多 worker exactly-once。无外部学员通讯或生产学习平台连接。Google Fonts离线时回退serif/sans-serif。测试/截图在../../test-artifacts/gb-3114，源码不放证据。MIT许可证，见LICENSE。
