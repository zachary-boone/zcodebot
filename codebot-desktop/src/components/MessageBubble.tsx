// 单条消息渲染：区分 user / assistant
import { memo, useState, type ComponentPropsWithoutRef, type ReactNode } from "react";
import { CodeBlock, StreamingMarkdown } from "streaming-markdown-react";
import { Check, Loader, Copy } from "lucide-react";
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
      {/* Markdown 内容 */}
      {message.content && (
        <div className="markdown-body">
          <StreamingMarkdown
            status={isStreaming ? "streaming" : "success"}
            components={markdownComponents}
          >
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
      {isStreaming && !message.content && (
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
