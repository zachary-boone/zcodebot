import { useEffect, useState, useCallback, useRef } from "react";
import { Menu } from "lucide-react";
import { useWebSocket } from "./hooks/useWebSocket";
import { useChatStore } from "./store/chatStore";
import { useThemeStore } from "./store/themeStore";
import { fetchSessionMessages } from "./hooks/useApi";
import { StatusBar } from "./components/StatusBar";
import { MessageList } from "./components/MessageList";
import { ChatInput } from "./components/ChatInput";
import { PermissionDialog } from "./components/PermissionDialog";
import { Sidebar } from "./components/Sidebar";
import { SettingsPanel } from "./components/SettingsPanel";
import { WorkDirDialog } from "./components/WorkDirDialog";

export default function App() {
  const { sendMessage, cancel, respondPermission, switchMode, switchSession, newSession, setWorkDir } = useWebSocket();
  const isStreaming = useChatStore((s) => s.isStreaming);
  const engineStatus = useChatStore((s) => s.engineStatus);
  const pendingPermission = useChatStore((s) => s.pendingPermission);
  const workDir = useChatStore((s) => s.workDir);
  const errorMessage = useChatStore((s) => s.errorMessage);
  const reset = useChatStore((s) => s.reset);
  const initTheme = useThemeStore((s) => s.init);

  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [insertText, setInsertText] = useState<{ text: string; id: number } | null>(null);
  // 工作目录切换对话框 + 切换中状态（后端重建 runtime 期间显示 loading）
  const [workDirDialogOpen, setWorkDirDialogOpen] = useState(false);
  const [workDirSwitching, setWorkDirSwitching] = useState(false);

  useEffect(() => {
    initTheme();
  }, [initTheme]);

  // 后端重建完成后会发 workdir_changed，workDir 随之更新——
  // 监听到变化就关闭对话框、清除 loading。注意 workDirSwitching 守卫
  // 避免首次连接 workDir 从 null 变成实际路径时误触发关闭逻辑。
  useEffect(() => {
    if (workDirSwitching && workDir) {
      setWorkDirSwitching(false);
      setWorkDirDialogOpen(false);
    }
  }, [workDir, workDirSwitching]);

  // 失败保护：后端路径校验失败 / 重建 runtime 报错时只发 error 不发 workdir_changed，
  // 此时 switching 会一直卡在 true。监听 errorMessage 出现即清除 loading（保留对话框
  // 让用户看到错误并修改路径重试）。同时加 25s 超时兜底防止永久卡死。
  useEffect(() => {
    if (!workDirSwitching) return;
    if (errorMessage) {
      setWorkDirSwitching(false);
    }
    const timer = setTimeout(() => setWorkDirSwitching(false), 25000);
    return () => clearTimeout(timer);
  }, [workDirSwitching, errorMessage]);

  const disabled = engineStatus !== "ready";

  // 切换会话：清空当前消息，从 REST 加载历史，发 WS 让后端也切
  const handleSwitchSession = useCallback(async (sessionId: string) => {
    reset();
    try {
      const msgs = await fetchSessionMessages(sessionId);
      // 用历史消息填充 store
      useChatStore.setState((state) => {
        const chatMsgs = msgs.map((m) => ({
          id: `hist-${m.role}-${Math.random().toString(36).slice(2, 8)}`,
          role: m.role as "user" | "assistant",
          content: m.content,
          thinking: m.thinking || "",
          toolCalls: (m.tool_uses || []).map((tu) => ({
            tool_id: tu.tool_id,
            tool_name: tu.tool_name,
            arguments: tu.arguments,
            status: "complete" as const,
          })),
          status: "complete" as const,
        }));
        return { messages: chatMsgs };
      });
    } catch (e) {
      console.error("load session messages failed", e);
    }
    switchSession(sessionId);
  }, [reset, switchSession]);

  // 文件树点击：插入 @path 到输入框（用递增 id 触发，避免 setTimeout 竞态）
  const insertIdRef = useRef(0);
  const handleInsertFile = useCallback((path: string) => {
    insertIdRef.current += 1;
    setInsertText({ text: `@${path} `, id: insertIdRef.current });
  }, []);

  const handleSend = (text: string) => {
    const trimmed = text.trim();
    if (trimmed === "/clear") {
      reset();
      return;
    }
    if (trimmed === "/help") {
      useChatStore.setState((state) => ({
        messages: [
          ...state.messages,
          {
            id: `help-${Date.now()}`,
            role: "assistant" as const,
            content: `## 可用命令

| 命令 | 说明 |
|------|------|
| \`/clear\` | 清空当前对话 |
| \`/help\` | 显示帮助 |
| \`/mode\` | 切换权限模式 |
| \`/skills\` | 列出技能（见侧栏"技能"标签） |
| \`/agents\` | 列出子 agent |
| \`/compact\` | 压缩上下文 |

## 快捷键

| 快捷键 | 功能 |
|--------|------|
| \`Enter\` | 发送消息 |
| \`Shift+Enter\` | 换行 |
| \`Ctrl+N\` | 新会话 |
| \`Ctrl+B\` | 切换侧栏 |
| \`Ctrl+L\` | 清空对话 |
| \`Ctrl+,\` | 打开设置 |
| \`@\` | 引用文件（补全） |
| \`/\` | 斜杠命令（补全） |

## 提示

- 点击侧栏会话可切换历史会话
- 侧栏"文件"标签可浏览项目文件，点击插入 @引用
- 顶部状态栏点击文件夹图标可**切换工作目录**，引擎会在新目录下重建（会话按目录隔离）`,
            thinking: "",
            toolCalls: [],
            status: "complete" as const,
          },
        ],
      }));
      return;
    }
    sendMessage(text);
  };

  const handleNewSession = useCallback(() => {
    reset();
    newSession();
  }, [reset, newSession]);
  const handleOpenSettings = useCallback(() => setSettingsOpen(true), []);
  const handleCloseSidebar = useCallback(() => setSidebarOpen(false), []);

  // 切换工作目录：打开自定义路径输入对话框（不使用 Electron 原生 dialog，
  // 因为在禁用 GPU 加速的环境下 dialog.showOpenDialog 会触发渲染崩溃）。
  // 用户在对话框里输入/粘贴绝对路径并确认后，再发 set_workdir 给后端。
  const handleSwitchWorkDir = useCallback(() => {
    setWorkDirDialogOpen(true);
  }, []);

  // 对话框确认：发 WS set_workdir，进入 switching 状态等后端重建。
  // 后端回 workdir_changed 后上面的 useEffect 会清除 switching 并关闭对话框。
  const handleConfirmWorkDir = useCallback(
    (path: string) => {
      setWorkDirSwitching(true);
      setWorkDir(path);
    },
    [setWorkDir]
  );

  // 全局快捷键
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      // 只在 Ctrl 组合键时处理
      if (!e.ctrlKey && !e.metaKey) return;
      const key = e.key.toLowerCase();
      if (key === "n") {
        e.preventDefault();
        handleNewSession();
      } else if (key === "b") {
        e.preventDefault();
        setSidebarOpen((v) => !v);
      } else if (key === "l") {
        e.preventDefault();
        reset();
      } else if (key === ",") {
        e.preventDefault();
        setSettingsOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [handleNewSession]);

  return (
    <div className="flex h-screen">
      {sidebarOpen && (
        <Sidebar
          onNewSession={handleNewSession}
          onOpenSettings={handleOpenSettings}
          onClose={handleCloseSidebar}
          onSwitchSession={handleSwitchSession}
          onInsertFile={handleInsertFile}
        />
      )}

      <div className="flex flex-col flex-1 min-w-0 relative">
        {!sidebarOpen && (
          <button
            onClick={() => setSidebarOpen(true)}
            className="absolute top-2 left-2 z-30 p-1.5 rounded-lg bg-bg-secondary border border-border text-text-tertiary hover:text-text-secondary transition-colors"
            title="打开侧栏 (Ctrl+B)"
          >
            <Menu size={16} />
          </button>
        )}
        <StatusBar onSwitchMode={switchMode} onSwitchWorkDir={handleSwitchWorkDir} />
        <MessageList />
        <ChatInput
          onSend={handleSend}
          onCancel={cancel}
          isStreaming={isStreaming}
          disabled={disabled}
          insertText={insertText?.text}
          insertId={insertText?.id}
        />
      </div>

      {pendingPermission && (
        <PermissionDialog request={pendingPermission} onRespond={respondPermission} />
      )}
      {settingsOpen && (
        <SettingsPanel
          onClose={() => setSettingsOpen(false)}
          onSwitchMode={(m) => switchMode(m)}
        />
      )}
      {workDirDialogOpen && (
        <WorkDirDialog
          currentWorkDir={workDir}
          switching={workDirSwitching}
          onClose={() => {
            if (!workDirSwitching) setWorkDirDialogOpen(false);
          }}
          onConfirm={handleConfirmWorkDir}
        />
      )}
    </div>
  );
}
