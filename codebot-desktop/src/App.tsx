import { useEffect } from "react";
import { useWebSocket } from "./hooks/useWebSocket";
import { useChatStore } from "./store/chatStore";
import { useThemeStore } from "./store/themeStore";
import { StatusBar } from "./components/StatusBar";
import { MessageList } from "./components/MessageList";
import { ChatInput } from "./components/ChatInput";
import { PermissionDialog } from "./components/PermissionDialog";

export default function App() {
  const { sendMessage, cancel, respondPermission, switchMode } = useWebSocket();
  const isStreaming = useChatStore((s) => s.isStreaming);
  const engineStatus = useChatStore((s) => s.engineStatus);
  const pendingPermission = useChatStore((s) => s.pendingPermission);
  const initTheme = useThemeStore((s) => s.init);

  // 初始化主题
  useEffect(() => {
    initTheme();
  }, [initTheme]);

  const disabled = engineStatus !== "ready";

  return (
    <div className="flex flex-col h-screen">
      <StatusBar onSwitchMode={switchMode} />
      <MessageList />
      <ChatInput
        onSend={sendMessage}
        onCancel={cancel}
        isStreaming={isStreaming}
        disabled={disabled}
      />
      {pendingPermission && (
        <PermissionDialog
          request={pendingPermission}
          onRespond={respondPermission}
        />
      )}
    </div>
  );
}
