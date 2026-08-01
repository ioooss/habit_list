"""Version-controlled system roles and their least-privilege permissions."""

from __future__ import annotations

from typing import Final

PERMISSIONS: Final[dict[str, str]] = {
    "admin.manage": "创建、停用管理员并分配角色",
    "config.read": "读取非敏感运行配置",
    "config.write": "创建配置草稿",
    "config.publish": "发布、回滚或紧急停用配置",
    "safety.read": "读取匿名化安全案例和策略",
    "safety.write": "修改安全策略草稿",
    "metrics.read": "读取聚合质量、成本与性能指标",
    "jobs.read": "读取后台任务和死信状态",
    "jobs.retry": "重试允许重试的后台任务",
    "privacy.read": "读取隐私工单状态，不含用户正文",
    "privacy.execute": "批准和执行导出/删除工单",
    "account.status.read": "读取账号状态，不含内容",
    "account.status.write": "封禁或恢复账号",
    "audit.read": "读取管理员审计事件",
}

ROLE_DEFINITIONS: Final[dict[str, tuple[str, str, frozenset[str]]]] = {
    "super_admin": (
        "Super Admin",
        "管理员、全局发布和紧急停用",
        frozenset(PERMISSIONS),
    ),
    "product_operator": (
        "Product Operator",
        "Prompt、模型、功能开关和灰度",
        frozenset(
            {
                "config.read",
                "config.write",
                "config.publish",
                "metrics.read",
                "jobs.read",
                "jobs.retry",
            }
        ),
    ),
    "safety_reviewer": (
        "Safety Reviewer",
        "安全策略和匿名化案例复核",
        frozenset({"safety.read", "safety.write", "metrics.read", "audit.read"}),
    ),
    "support": (
        "Support",
        "账号状态和用户主动提交的支持材料",
        frozenset({"account.status.read", "privacy.read"}),
    ),
    "analyst": (
        "Analyst",
        "仅聚合指标和质量报表",
        frozenset({"metrics.read"}),
    ),
}


__all__ = ["PERMISSIONS", "ROLE_DEFINITIONS"]
