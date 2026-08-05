import { useEffect, useState } from "react";
import { Menu } from "lucide-react";
import { useWebSocket } from "./hooks/useWebSocket";
import { useChatStore } from "./store/chatStore";
import { useThemeStore } from "./store/themeStore";
import { StatusBar } from "./components/StatusBar";
import { MessageList } from "./components/MessageList";
import { ChatInput } from "./components/ChatInput";
import { PermissionDialog } from "./components/PermissionDialog";
import { Sidebar } from "./components/Sidebar";
import { SettingsPanel } from "./components/SettingsPanel";

export default function App() {
  const { sendMessage, cancel, respondPermission, switchMode } = useWebSocket();
  const isStreaming = useChatStore((s) => s.isStreaming);
  const engineStatus = useChatStore((s) => s.engineStatus);
  const pendingPermission = useChatStore((s) => s.pendingPermission);
  const reset = useChatStore((s) => s.reset);
  const initTheme = useThemeStore((s) => s.init);

  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [settingsOpen, setSettingsOpen] = useState(false);

  useEffect(() => {
    initTheme();
  }, [initTheme]);

  const disabled = engineStatus !== "ready";

  const handleSend = (text: string) => {
    // 前端拦截部分命令
    const trimmed = text.trim();
    if (trimmed === "/clear") {
      reset();
      return;
    }
    if (trimmed === "/help") {
      // 注入一条帮助消息到消息列表
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
| \`@\` | 引用文件（补全） |
| \`/\` | 斜杠命令（补全） |

## 提示

- 点击状态栏可切换权限模式和主题
- 侧栏可查看历史会话、记忆笔记、已安装技能`,
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

  return (
    <div className="flex h-screen">
      {/* 侧栏 */}
      {sidebarOpen && (
        <Sidebar
          onNewSession={() => {
            reset();
          }}
          onOpenSettings={() => setSettingsOpen(true)}
          onClose={() => setSidebarOpen(false)}
        />
      )}

      {/* 主区 */}
      <div className="flex flex-col flex-1 min-w-0">
        {/* 侧栏开关（侧栏关闭时显示） */}
        {!sidebarOpen && (
          <button
            onClick={() => setSidebarOpen(true)}
            className="absolute top-2 left-2 z-30 p-1.5 rounded-lg bg-bg-secondary border border-border text-text-tertiary hover:text-text-secondary transition-colors"
            title="打开侧栏"
          >
            <Menu size={16} />
          </button>
        )}
        <StatusBar onSwitchMode={switchMode} />
        <MessageList />
        <ChatInput
          onSend={handleSend}
          onCancel={cancel}
          isStreaming={isStreaming}
          disabled={disabled}
        />
      </div>

      {pendingPermission && (
        <PermissionDialog request={pendingPermission} onRespond={respondPermission} />
      )}
      {settingsOpen && (
        <SettingsPanel
          onClose={() => setSettingsOpen(false)}
          onSwitchMode={(m) => {
            switchMode(m);
          }}
        />
      )}
    </div>
  );
}
