import { useEffect, useRef } from "react";
import { useChatStore } from "../store/chatStore";
import { MessageBubble } from "./MessageBubble";

export function MessageList() {
  const messages = useChatStore((s) => s.messages);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const errorMessage = useChatStore((s) => s.errorMessage);
  const endRef = useRef<HTMLDivElement>(null);

  // 流式时自动滚动到底部
  useEffect(() => {
    if (isStreaming) {
      endRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, isStreaming]);

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-4xl mx-auto py-4">
        {messages.length === 0 && !errorMessage && (
          <div className="flex flex-col items-center justify-center h-full text-text-tertiary mt-32">
            <div className="text-4xl mb-3">🤖</div>
            <div className="text-lg font-medium text-text-secondary mb-1">CodeBot 桌面版</div>
            <div className="text-sm">在下方输入消息开始对话</div>
          </div>
        )}
        {messages.map((m) => (
          <MessageBubble key={m.id} message={m} />
        ))}
        {errorMessage && (
          <div className="mx-4 my-2 px-4 py-2 rounded-lg bg-red-950/40 border border-red-500/30 text-red-300 text-sm">
            {errorMessage}
          </div>
        )}
        <div ref={endRef} />
      </div>
    </div>
  );
}
