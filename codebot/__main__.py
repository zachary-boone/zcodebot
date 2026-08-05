from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from codebot.config import ConfigError, load_config
from codebot.hooks import HookConfigError, HookEngine, load_hooks
from codebot.permissions import PermissionMode


def main() -> None:
    # 先确保 .codebot/ 目录存在，否则下面写 debug.log 会因目录不存在而崩溃
    Path(".codebot").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        filename=".codebot/debug.log",
        filemode="w",
    )

    parser = argparse.ArgumentParser(prog="codebot", description="CodeBot AI coding assistant")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in PermissionMode],
        default=None,
        help="Permission mode (overrides config.yaml)",
    )
    parser.add_argument(
        "-p",
        metavar="PROMPT",
        default=None,
        help="Run non-interactively: execute the prompt and print the result to stdout",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Web serving mode (textual serve): use plain platform driver, "
        "skip NoAltScreenDriver's extra ANSI sequences that interfere with "
        "the textual-serve protocol handshake.",
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    mode_str = args.mode if args.mode else config.permission_mode
    permission_mode = PermissionMode(mode_str)

    try:
        hooks = load_hooks(config.raw_hooks)
    except HookConfigError as e:
        print(f"Hook config error: {e}", file=sys.stderr)
        sys.exit(1)

    hook_engine = HookEngine(hooks) if hooks else None

    if args.p is not None:
        asyncio.run(_run_prompt(config, permission_mode, hook_engine, args.p))
        return

    from codebot.app import CodeBotApp

    if args.web:
        # Web serving (textual serve) 子进程环境：使用纯净的平台 driver，
        # 避免 NoAltScreenDriver 去除 alt-screen / 写空行等额外 ANSI 序列
        # 干扰 textual_serve 的终端协议握手。
        if sys.platform == "win32":
            from textual.drivers.windows_driver import WindowsDriver
            driver_class = WindowsDriver
        else:
            from textual.drivers.linux_driver import LinuxDriver
            driver_class = LinuxDriver
    else:
        from codebot.driver import NoAltScreenDriver
        driver_class = NoAltScreenDriver

    app = CodeBotApp(
        providers=config.providers,
        permission_mode=permission_mode,
        mcp_servers=config.mcp_servers,
        hook_engine=hook_engine,
        enable_fork=config.enable_fork,
        enable_verification_agent=config.enable_verification_agent,
        worktree_config=config.worktree,
        teammate_mode=config.teammate_mode,
        enable_coordinator_mode=config.enable_coordinator_mode,
        driver_class=driver_class,
    )
    app.run()


async def _run_prompt(config, permission_mode, hook_engine, prompt: str) -> None:
    from codebot.agent import Agent
    from codebot.client import create_client, resolve_context_window
    from codebot.conversation import ConversationManager
    from codebot.memory.instructions import load_instructions
    from codebot.permissions import (
        DangerousCommandDetector,
        PathSandbox,
        PermissionChecker,
        RuleEngine,
    )
    from codebot.tools import create_default_registry
    from codebot.agents.loader import AgentLoader
    from codebot.agents.task_manager import TaskManager
    from codebot.agents.trace import TraceManager
    from codebot.tools.agent_tool import AgentTool
    from codebot.tools.impl.tool_search import ToolSearchTool
    from codebot.teams.manager import TeamManager
    from codebot.teams.models import BackendType
    from codebot.tools.team_create import TeamCreateTool
    from codebot.tools.team_delete import TeamDeleteTool
    from codebot.worktree import WorktreeManager
    from codebot.config import WorktreeConfig

    provider = config.providers[0]
    client = create_client(provider)
    # 第 2 层：尽力从 provider 自动拉取模型的 context window（缓存在 provider 上）。
    # 不会抛异常或阻塞启动；失败则退化到映射表。
    await resolve_context_window(provider)
    work_dir = os.getcwd()
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

    def drain_notifications() -> list[str]:
        notes: list[str] = []
        for t in task_manager.poll_completed():
            notes.append(
                f"<task-notification>\n<task_id>{t.id}</task_id>\n"
                f"<status>{t.status}</status>\n<result>{t.result}</result>\n"
                f"</task-notification>"
            )
        notes.extend(team_manager.drain_lead_mailbox())
        return notes

    def drain_mailbox_only() -> list[str]:
        return team_manager.drain_lead_mailbox()

    agent.notification_fn = drain_mailbox_only

    conv = ConversationManager()
    last_result = await agent.run_to_completion(prompt, conv)
    print(last_result, flush=True)

    if not team_manager._teams:
        return

    import sys
    for i in range(90):
        await asyncio.sleep(2)
        running = {k: not t.done() for k, t in task_manager._async_tasks.items()}
        completed_ids = [t.id for t in task_manager._tasks.values() if t.status != "running"]
        print(f"[poll {i}] running={running} completed={completed_ids} teams={list(team_manager._teams.keys())} queue_size={task_manager._notify_queue.qsize()}", file=sys.stderr, flush=True)
        notes = drain_notifications()
        if not notes:
            has_running = any(v for v in running.values())
            if not has_running:
                print(f"[poll {i}] no running tasks, breaking", file=sys.stderr, flush=True)
                break
            continue
        for note in notes:
            conv.add_system_reminder(note)
        last_result = await agent.run_to_completion(
            "Teammate notifications received. Process them and continue.", conv
        )
        print(last_result, flush=True)


if __name__ == "__main__":
    main()

