// 工具调用块：可折叠，显示工具名/状态/输入参数/输出结果
// 按工具类型区分渲染：写文件类用 diff 视图，Bash 用终端样式
import { memo, useState, useEffect } from "react";
import ReactDiffViewer from "react-diff-viewer-continued";
import { ChevronRight, ChevronDown, Check, AlertCircle, Loader, FileEdit, Terminal, FileSearch, Folder } from "lucide-react";
import type { ToolCall } from "../types";

const API_BASE = "http://127.0.0.1:7800/api";

// 需要显示 diff 的工具（有 file_path 参数且会修改文件）
const DIFF_TOOLS = new Set(["WriteFile", "EditFile", "write_file", "edit_file"]);
// Bash 类工具
const BASH_TOOLS = new Set(["Bash", "bash", "shell"]);

interface Props {
  tool: ToolCall;
}

function getFilePath(args: Record<string, unknown>): string | null {
  return (args.file_path as string) || (args.path as string) || (args.file as string) || null;
}

function ToolCallBlockBase({ tool }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [oldContent, setOldContent] = useState<string | null>(null);

  const isDiff = DIFF_TOOLS.has(tool.tool_name);
  const isBash = BASH_TOOLS.has(tool.tool_name);
  const filePath = getFilePath(tool.arguments);

  // 对写文件类工具，自动展开并尝试读取原文件内容做 diff
  useEffect(() => {
    if (isDiff && filePath && expanded && oldContent === null) {
      fetch(`${API_BASE}/files/content?path=${encodeURIComponent(filePath)}`)
        .then((r) => (r.ok ? r.text() : Promise.reject()))
        .then((text) => setOldContent(text))
        .catch(() => setOldContent(""));
    }
  }, [isDiff, filePath, expanded, oldContent]);

  const statusIcon =
    tool.status === "running" ? (
      <Loader size={13} className="animate-spin text-accent" />
    ) : tool.status === "error" ? (
      <AlertCircle size={13} className="text-red-400" />
    ) : (
      <Check size={13} className="text-emerald-400" />
    );

  const toolIcon = isDiff ? (
    <FileEdit size={13} className="text-blue-400" />
  ) : isBash ? (
    <Terminal size={13} className="text-amber-400" />
  ) : tool.tool_name.toLowerCase().includes("search") || tool.tool_name.toLowerCase().includes("grep") ? (
    <FileSearch size={13} className="text-purple-400" />
  ) : (
    <Folder size={13} className="text-text-tertiary" />
  );

  // 写文件类工具：写文件后原文件已变，用"新增内容"vs"原内容"对比
  const newContent = (tool.arguments.content as string) || (tool.arguments.new_string as string) || "";

  return (
    <div className="border border-border rounded-lg bg-bg-tertiary overflow-hidden my-0.5">
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-xs hover:bg-bg-secondary transition-colors"
      >
        {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        {statusIcon}
        {toolIcon}
        <span className="font-mono text-text-secondary">{tool.tool_name}</span>
        {filePath && (
          <span className="text-text-tertiary truncate">{filePath}</span>
        )}
        {tool.elapsed !== undefined && tool.status !== "running" && (
          <span className="text-text-tertiary ml-auto">{tool.elapsed}s</span>
        )}
      </button>
      {expanded && (
        <div className="px-3 py-2 border-t border-border text-xs">
          {/* 输入参数（非 diff 工具显示完整参数） */}
          {!isDiff && (
            <div className="mb-2">
              <div className="text-text-tertiary mb-1">输入</div>
              <pre className="font-mono text-text-secondary overflow-x-auto whitespace-pre-wrap break-all bg-bg-primary rounded p-2">
                {formatArgs(tool.arguments, isBash)}
              </pre>
            </div>
          )}

          {/* diff 视图（写文件类） */}
          {isDiff && newContent && (
            <div className="mb-2">
              <div className="text-text-tertiary mb-1">
                {filePath} {oldContent === null ? "（读取原文件中...）" : oldContent === "" ? "（新文件）" : ""}
              </div>
              {oldContent !== null && (
                <div className="rounded overflow-hidden border border-border">
                  <ReactDiffViewer
                    oldValue={oldContent}
                    newValue={newContent}
                    splitView={false}
                    hideLineNumbers={false}
                    useDarkTheme={true}
                    styles={{
                      variables: {
                        dark: {
                          diffViewerBackground: "transparent",
                          diffViewerColor: "var(--text-secondary)",
                          addedBackground: "rgba(52, 211, 153, 0.12)",
                          addedColor: "#6ee7b7",
                          removedBackground: "rgba(248, 113, 113, 0.12)",
                          removedColor: "#fca5a5",
                          wordAddedBackground: "rgba(52, 211, 153, 0.25)",
                          wordRemovedBackground: "rgba(248, 113, 113, 0.25)",
                          addedGutterBackground: "rgba(52, 211, 153, 0.08)",
                          removedGutterBackground: "rgba(248, 113, 113, 0.08)",
                          gutterBackground: "transparent",
                          gutterColor: "var(--text-tertiary)",
                          codeFoldGutterBackground: "transparent",
                          codeFoldBackground: "rgba(255,255,255,0.03)",
                          emptyLineBackground: "transparent",
                        },
                      },
                      contentText: { fontSize: "12px", fontFamily: "JetBrains Mono, Consolas, monospace" },
                      lineNumber: { fontSize: "11px", color: "var(--text-tertiary)" },
                    }}
                  />
                </div>
              )}
            </div>
          )}

          {/* 输出（非 diff 工具） */}
          {tool.output !== undefined && !isDiff && (
            <div>
              <div className="text-text-tertiary mb-1">
                输出{tool.truncated && "（已截断）"}
              </div>
              <pre
                className={`font-mono overflow-x-auto whitespace-pre-wrap break-all p-2 rounded ${
                  tool.is_error
                    ? "text-red-300 bg-red-950/30"
                    : isBash
                    ? "text-emerald-300 bg-black/40"
                    : "text-text-secondary"
                }`}
              >
                {tool.output}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function formatArgs(args: Record<string, unknown>, isBash: boolean): string {
  if (isBash) {
    // Bash 工具只显示 command
    const cmd = (args.command as string) || (args.cmd as string) || "";
    if (cmd) return `$ ${cmd}`;
  }
  return JSON.stringify(args, null, 2);
}

export const ToolCallBlock = memo(ToolCallBlockBase);
