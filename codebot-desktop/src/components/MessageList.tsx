import { useEffect, useRef, useState } from "react";
import { ArrowDown, Copy, Check } from "lucide-react";
import { useChatStore } from "../store/chatStore";
import { MessageBubble } from "./MessageBubble";

export function MessageList() {
  const messages = useChatStore((s) => s.messages);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const errorMessage = useChatStore((s) => s.errorMessage);
  const endRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [showScrollBtn, setShowScrollBtn] = useState(false);

  const scrollToBottom = (smooth = true) => {
    endRef.current?.scrollIntoView({ behavior: smooth ? "smooth" : "auto" });
  };

  // 流式时自动滚动到底部。
  // 流式期间用 auto（瞬时定位）：smooth 在持续收到新内容时每次都要重新
  // 启动滚动动画，帧率低会感觉卡顿；流式结束后的首次定位用 smooth 收尾。
  const wasStreaming = useRef(false);
  useEffect(() => {
    if (isStreaming) {
      endRef.current?.scrollIntoView({ behavior: "auto" });
      wasStreaming.current = true;
    } else if (wasStreaming.current) {
      wasStreaming.current = false;
      endRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, isStreaming]);

  // 检测是否需要显示"滚动到底部"按钮
  const handleScroll = () => {
    const el = containerRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setShowScrollBtn(distanceFromBottom > 200);
  };

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      className="flex-1 overflow-y-auto relative"
    >
      <div className="max-w-4xl mx-auto py-4">
        {messages.length === 0 && !errorMessage && (
          <div className="flex flex-col items-center justify-center h-full text-text-tertiary mt-32">
            <div className="text-5xl mb-4">🤖</div>
            <div className="text-lg font-medium text-text-secondary mb-1">CodeBot 桌面版</div>
            <div className="text-sm mb-4">在下方输入消息开始对话</div>
            <div className="text-xs text-text-tertiary space-y-1 text-center">
              <div><kbd className="px-1.5 py-0.5 bg-bg-tertiary rounded font-mono text-[10px]">Ctrl+N</kbd> 新会话</div>
              <div><kbd className="px-1.5 py-0.5 bg-bg-tertiary rounded font-mono text-[10px]">Ctrl+B</kbd> 切换侧栏</div>
              <div><kbd className="px-1.5 py-0.5 bg-bg-tertiary rounded font-mono text-[10px]">Ctrl+,</kbd> 设置</div>
              <div><kbd className="px-1.5 py-0.5 bg-bg-tertiary rounded font-mono text-[10px]">/help</kbd> 查看帮助</div>
            </div>
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

      {/* 滚动到底部按钮 */}
      {showScrollBtn && (
        <button
          onClick={() => scrollToBottom(true)}
          className="absolute bottom-4 left-1/2 -translate-x-1/2 w-9 h-9 rounded-full bg-bg-secondary border border-border shadow-lg flex items-center justify-center text-text-secondary hover:text-text-primary hover:border-accent/50 transition-all z-20"
          title="滚动到底部"
        >
          <ArrowDown size={16} />
        </button>
      )}
    </div>
  );
}

// 消息复制按钮（挂在 MessageBubble 里用）
export function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {}
  };
  if (!text) return null;
  return (
    <button
      onClick={handleCopy}
      className="opacity-0 group-hover:opacity-100 p-1 text-text-tertiary hover:text-text-secondary transition-all"
      title="复制"
    >
      {copied ? <Check size={12} className="text-emerald-400" /> : <Copy size={12} />}
    </button>
  );
}
