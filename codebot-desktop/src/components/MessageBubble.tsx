// 单条消息渲染：区分 user / assistant
import { memo, useState } from "react";
import { StreamingMarkdown } from "streaming-markdown-react";
import { ChevronRight, ChevronDown, Check, AlertCircle, Loader, Copy, Brain } from "lucide-react";
import type { ChatMessage } from "../types";
import { ToolCallBlock } from "./ToolCallBlock";

interface Props {
  message: ChatMessage;
}

function MessageBubbleBase({ message }: Props) {
  // 思考过程默认展开（推理模型的思考是用户最关心的部分），可手动折叠。
  // streaming 期间即使被折叠，新内容到达时也会自动重新展开。
  const [showThinking, setShowThinking] = useState(true);
  const isUser = message.role === "user";
  const isStreaming = message.status === "streaming";

  if (isUser) {
    return (
      <div className="flex justify-end px-4 py-3">
        <div className="max-w-[80%] rounded-2xl bg-bg-message px-4 py-3 text-text-primary whitespace-pre-wrap">
          {message.content}
        </div>
      </div>
    );
  }

  return (
    <div className="px-4 py-3">
      {/* 思考过程（默认展开，可折叠；流式时自动展开） */}
      {message.thinking && (
        <div className="mb-2">
          <button
            onClick={() => setShowThinking((v) => !v)}
            className="flex items-center gap-1.5 text-xs text-text-tertiary hover:text-text-secondary transition-colors group"
          >
            {showThinking ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            <Brain size={13} className={isStreaming ? "text-accent animate-pulse" : ""} />
            <span>思考过程</span>
            {/* 流式期间显示"推理中"动画 */}
            {isStreaming && (
              <span className="flex items-center gap-1 text-text-tertiary/80">
                <span className="flex gap-0.5">
                  <Dot className="animate-bounce [animation-delay:0ms]" />
                  <Dot className="animate-bounce [animation-delay:150ms]" />
                  <Dot className="animate-bounce [animation-delay:300ms]" />
                </span>
                推理中
              </span>
            )}
            {/* 思考字数（折叠时也展示，提示有内容） */}
            <span className="text-[10px] text-text-tertiary/70 group-hover:opacity-100">
              {message.thinking.length.toLocaleString()} 字
            </span>
          </button>
          {showThinking && (
            <div className="mt-1 ml-5 text-xs text-text-tertiary whitespace-pre-wrap border-l-2 border-border pl-3 max-h-52 overflow-y-auto">
              {message.thinking}
              {/* 思考流式光标 */}
              {isStreaming && <span className="thinking-cursor" />}
            </div>
          )}
        </div>
      )}

      {/* Markdown 内容 */}
      {message.content && (
        <div className="markdown-body">
          <StreamingMarkdown status={isStreaming ? "streaming" : "success"}>
            {message.content}
          </StreamingMarkdown>
          {/* 文本流式光标：内容边生成边显示闪烁光标，增强流式感 */}
          {isStreaming && message.content && <span className="stream-cursor" />}
        </div>
      )}

      {/* 工具调用块 */}
      {message.toolCalls.length > 0 && (
        <div className="mt-2 space-y-1.5">
          {message.toolCalls.map((tc) => (
            <ToolCallBlock key={tc.tool_id} tool={tc} />
          ))}
        </div>
      )}

      {/* 流式光标：还没内容时显示思考中占位 */}
      {isStreaming && !message.content && !message.thinking && (
        <div className="flex items-center gap-2 text-text-tertiary text-sm">
          <Loader size={14} className="animate-spin" />
          <span>思考中...</span>
        </div>
      )}

      {/* 底部信息 */}
      {message.status === "complete" && (
        <div className="mt-2 flex items-center gap-3">
          {message.usage && (
            <span className="text-xs text-text-tertiary">
              {message.usage.input_tokens + message.usage.output_tokens} tokens
            </span>
          )}
          <CopyButton text={message.content} />
        </div>
      )}
    </div>
  );
}

// 思考区"推理中"的跳动小圆点
function Dot({ className = "" }: { className?: string }) {
  return (
    <span className={`w-0.5 h-0.5 rounded-full bg-current inline-block ${className}`} />
  );
}

function CopyButton({ text }: { text: string }) {
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
      className="text-text-tertiary hover:text-text-secondary transition-colors"
      title="复制消息"
    >
      {copied ? <Check size={12} className="text-emerald-400" /> : <Copy size={12} />}
    </button>
  );
}

export const MessageBubble = memo(MessageBubbleBase);
