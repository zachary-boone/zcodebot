// 单条消息渲染：区分 user / assistant
import { memo, useState } from "react";
import { StreamingMarkdown } from "streaming-markdown-react";
import { ChevronRight, ChevronDown, Check, AlertCircle, Loader, Copy } from "lucide-react";
import type { ChatMessage } from "../types";
import { ToolCallBlock } from "./ToolCallBlock";

interface Props {
  message: ChatMessage;
}

function MessageBubbleBase({ message }: Props) {
  const [showThinking, setShowThinking] = useState(false);
  const isUser = message.role === "user";

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
      {/* 思考过程（可折叠） */}
      {message.thinking && (
        <div className="mb-2">
          <button
            onClick={() => setShowThinking((v) => !v)}
            className="flex items-center gap-1 text-xs text-text-tertiary hover:text-text-secondary transition-colors"
          >
            {showThinking ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            <span>思考过程</span>
          </button>
          {showThinking && (
            <div className="mt-1 ml-5 text-xs text-text-tertiary italic whitespace-pre-wrap border-l-2 border-border pl-3">
              {message.thinking}
            </div>
          )}
        </div>
      )}

      {/* Markdown 内容 */}
      {message.content && (
        <div className="markdown-body">
          <StreamingMarkdown status={message.status === "streaming" ? "streaming" : "success"}>
            {message.content}
          </StreamingMarkdown>
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

      {/* 流式光标 */}
      {message.status === "streaming" && !message.content && (
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
