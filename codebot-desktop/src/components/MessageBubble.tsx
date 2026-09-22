// 单条消息渲染：区分 user / assistant
import { memo, useState, type ComponentPropsWithoutRef, type ReactNode } from "react";
import { CodeBlock, StreamingMarkdown } from "streaming-markdown-react";
import ReactMarkdown from "react-markdown";
import { Check, Copy, Brain, ChevronDown, ChevronRight } from "lucide-react";
import type { ChatMessage } from "../types";
import { ToolCallBlock } from "./ToolCallBlock";

interface Props {
  message: ChatMessage;
}

type MarkdownCodeProps = ComponentPropsWithoutRef<"code"> & {
  node?: unknown;
};

function MarkdownCode({ children, className, node: _node, ...props }: MarkdownCodeProps) {
  const content = String(children ?? "");
  const language = /language-([\w-]+)/.exec(className ?? "")?.[1];

  // react-markdown no longer supplies the old `inline` prop. A fenced code
  // block retains its trailing newline (or a language class), while inline
  // code does not.
  if (!language && !content.includes("\n")) {
    return (
      <code className={className} {...props}>
        {children}
      </code>
    );
  }

  return (
    <CodeBlock
      code={content.replace(/\n$/, "")}
      language={language}
      className={className}
    />
  );
}

function MarkdownPre({ children }: { children?: ReactNode }) {
  // CodeBlock already provides its own block container.
  return <>{children}</>;
}

const markdownComponents = {
  code: MarkdownCode,
  pre: MarkdownPre,
};

function MessageBubbleBase({ message }: Props) {
  const isUser = message.role === "user";
  const isStreaming = message.status === "streaming";
  // 当前执行中的消息默认展开，让用户无需切换会话就能看到实时过程；
  // 历史消息保持折叠，避免重新打开会话时占满正文区域。
  const [showThinking, setShowThinking] = useState(isStreaming);

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
    <div className="px-4 py-1.5">
      {(message.thinking || message.activity || isStreaming) && (
        <div className="mb-2 max-w-[88ch] rounded-lg border border-border bg-bg-tertiary/70 overflow-hidden">
          <button
            type="button"
            onClick={() => setShowThinking((value) => !value)}
            className="w-full flex items-center gap-2 px-3 py-2 text-xs text-text-secondary hover:bg-bg-secondary transition-colors"
          >
            {showThinking ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            <Brain size={13} className="text-accent" />
            <span>{message.activity || (isStreaming ? "正在工作" : "工作过程")}</span>
          </button>
          {showThinking && (
            <div className="border-t border-border px-3 py-2 text-xs leading-6 text-text-tertiary whitespace-pre-wrap break-words max-h-56 overflow-y-auto">
              {message.thinking || message.activity || "正在分析任务并准备执行…"}
            </div>
          )}
        </div>
      )}
      {/* Markdown 内容 */}
      {message.content && (
        <div className="markdown-body">
          {isStreaming ? (
            <StreamingMarkdown status="streaming" components={markdownComponents}>
              {message.content}
            </StreamingMarkdown>
          ) : (
            <ReactMarkdown components={markdownComponents}>
              {message.content}
            </ReactMarkdown>
          )}
          {/* 文本流式光标：内容边生成边显示闪烁光标，增强流式感 */}
          {isStreaming && message.content && <span className="stream-cursor" />}
        </div>
      )}

      {/* 工具调用块 */}
      {message.toolCalls.length > 0 && (
        <div className="mt-1 space-y-1">
          {message.toolCalls.map((tc) => (
            <ToolCallBlock key={tc.tool_id} tool={tc} />
          ))}
        </div>
      )}

      {/* 底部信息 */}
      {message.status === "complete" && (
        <div className="mt-2 flex items-center gap-3">
          {message.usage && (
            <span className="text-xs text-text-tertiary">
              {message.usage.input_tokens + message.usage.output_tokens} Token
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
