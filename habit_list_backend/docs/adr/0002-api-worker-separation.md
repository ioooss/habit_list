# ADR 0002：API 与后台任务分离

- 状态：Accepted
- 日期：2026-08-01

## 背景

旧架构在 FastAPI lifespan 内启动 APScheduler。多副本 API 会重复执行记忆巩固和 Outbox 消费，滚动发布也会让任务所有权不清晰。

## 决策

- `PROCESS_ROLE=api` 只提供 HTTP 服务。
- `PROCESS_ROLE=worker` 独立拥有调度器，并通过原子心跳文件提供容器健康检查。
- 本地开发保留 `PROCESS_ROLE=all`，生产配置明确禁止它。
- PostgreSQL Outbox 领取使用 `FOR UPDATE SKIP LOCKED`，允许后续横向扩展 Worker。

## 结果

API 可独立扩容且不会复制调度任务，Worker 故障可单独检测和恢复。当前调度仍由单个 Worker 负责；扩展到多个 Worker 前，所有周期任务还需要数据库级 leader lock 或幂等执行审计。
