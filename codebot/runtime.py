"""CodeBot 引擎运行时 —— 把 CLI 与桌面版共用的引擎初始化逻辑抽到一处。

CLI（__main__.py）和 FastAPI 桥接服务（server.py）都调 build_runtime()，
保证两边构造的 Agent / Registry / PermissionChecker / TeamManager 完全一致。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codebot.config import AppConfig, WorktreeConfig
from codebot.hooks import HookEngine
from codebot.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)


@dataclass
class Runtime:
    """一次完整的 CodeBot 引擎运行时集合。build_runtime() 的返回值。"""

    agent: Any  # codebot.agent.Agent，避免循环 import 用 Any
    client: Any
    conversation: Any  # codebot.conversation.ConversationManager
    registry: Any
    provider: Any  # ProviderConfig
    permission_checker: PermissionChecker
    task_manager: Any
    team_manager: Any
    agent_loader: Any
    worktree_manager: Any
    trace_manager: Any
    hook_engine: HookEngine | None
    config: AppConfig
    # 当前工作目录。CLI 用 os.getcwd()；桌面版可在运行时通过 server.set_workdir 切换。
    # 重建 runtime 时此字段会被刷新，REST 接口与 WS 都依赖它定位文件 / 会话 / memory。
    work_dir: str = ""
    # pending permission requests: request_id(str) -> asyncio.Future
    # 由 server.py 填充并管理；CLI 不用
    pending_permissions: dict[str, Any] = field(default_factory=dict)


async def build_runtime(
    config: AppConfig,
    permission_mode: PermissionMode,
    hook_engine: HookEngine | None,
    work_dir: str | None = None,
) -> Runtime:
    """构造一套完整的 CodeBot 引擎运行时。

    复用 __main__.py:_run_prompt 里的初始化逻辑，抽成可复用函数。
    work_dir 默认当前目录；server.py 可显式传入。
    """
    # 延迟 import：这些模块较重，避免 runtime 模块 import 时全量加载
    from codebot.agent import Agent
    from codebot.client import create_client, resolve_context_window
    from codebot.conversation import ConversationManager
    from codebot.memory.instructions import load_instructions
    from codebot.tools import create_default_registry
    from codebot.agents.loader import AgentLoader
    from codebot.agents.task_manager import TaskManager
    from codebot.agents.trace import TraceManager
    from codebot.tools.agent_tool import AgentTool
    from codebot.tools.impl.tool_search import ToolSearchTool
    from codebot.teams.manager import TeamManager
    from codebot.tools.team_create import TeamCreateTool
    from codebot.tools.team_delete import TeamDeleteTool
    from codebot.worktree import WorktreeManager

    provider = config.providers[0]
    client = create_client(provider)
    # 尽力拉取模型 context window（失败静默降级到映射表）
    await resolve_context_window(provider)

    work_dir = work_dir or os.getcwd()
    home = Path.home()

    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(work_dir),
        rule_engine=RuleEngine(
            user_rules_path=home / ".codebot" / "permissions.yaml",
            project_rules_path=Path(work_dir) / ".codebot" / "permissions.yaml",
            local_rules_path=Path(work_dir) / ".codebot" / "permissions.local.yaml",
        ),
        mode=permission_mode,
    )

    instructions = load_instructions(work_dir)
    registry = create_default_registry()
    registry.register(ToolSearchTool(registry, protocol=provider.protocol))

    agent = Agent(
        client=client,
        registry=registry,
        protocol=provider.protocol,
        work_dir=work_dir,
        permission_checker=checker,
        context_window=provider.get_context_window(),
        instructions_content=instructions,
        hook_engine=hook_engine,
    )

    wt_cfg = config.worktree or WorktreeConfig()
    wt_manager = WorktreeManager(
        repo_root=work_dir,
        symlink_directories=wt_cfg.symlink_directories,
    )
    trace_manager = TraceManager()
    task_manager = TaskManager()
    agent_loader = AgentLoader(work_dir, enable_verification=config.enable_verification_agent)
    agent_loader.load_all()
    team_manager = TeamManager(worktree_manager=wt_manager, trace_manager=trace_manager)

    agent_tool = AgentTool(
        agent_loader=agent_loader,
        task_manager=task_manager,
        trace_manager=trace_manager,
        parent_agent=agent,
        enable_fork=config.enable_fork,
        provider_config=provider,
        worktree_manager=wt_manager,
        team_manager=team_manager,
    )
    registry.register(agent_tool)
    registry.register(TeamCreateTool(
        team_manager=team_manager,
        parent_agent=agent,
        teammate_mode="in-process",
        is_interactive=False,
        enable_coordinator_mode=config.enable_coordinator_mode,
    ))
    registry.register(TeamDeleteTool(team_manager=team_manager, parent_agent=agent))

    # 团队/子 agent 完成通知的排空回调（与 CLI 保持一致）
    def drain_mailbox_only() -> list[str]:
        return team_manager.drain_lead_mailbox()

    agent.notification_fn = drain_mailbox_only

    conversation = ConversationManager()

    return Runtime(
        agent=agent,
        client=client,
        conversation=conversation,
        registry=registry,
        provider=provider,
        permission_checker=checker,
        task_manager=task_manager,
        team_manager=team_manager,
        agent_loader=agent_loader,
        worktree_manager=wt_manager,
        trace_manager=trace_manager,
        hook_engine=hook_engine,
        config=config,
        work_dir=work_dir,
    )
