// 输入框：Enter 发送，Shift+Enter 换行，@ 文件引用补全，/ 命令补全，流式时显示停止按钮
import { useRef, useEffect, useState, useCallback } from "react";
import { Send, Square } from "lucide-react";

const API_BASE = "http://127.0.0.1:7800/api";

// 内置斜杠命令
const SLASH_COMMANDS = [
  { cmd: "/help", desc: "查看帮助" },
  { cmd: "/clear", desc: "清空对话" },
  { cmd: "/mode", desc: "切换权限模式" },
  { cmd: "/skills", desc: "列出技能" },
  { cmd: "/agents", desc: "列出子 agent" },
  { cmd: "/compact", desc: "压缩上下文" },
];

interface FileEntry {
  name: string;
  path: string;
  is_dir: boolean;
}

interface Props {
  onSend: (text: string) => void;
  onCancel: () => void;
  isStreaming: boolean;
  disabled: boolean;
  insertText?: string | null;
}

export function ChatInput({ onSend, onCancel, isStreaming, disabled, insertText }: Props) {
  const [text, setText] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // 补全状态
  const [showFileComplete, setShowFileComplete] = useState(false);
  const [showCmdComplete, setShowCmdComplete] = useState(false);
  const [fileList, setFileList] = useState<FileEntry[]>([]);
  const [fileQuery, setFileQuery] = useState("");
  const [cmdList, setCmdList] = useState(SLASH_COMMANDS);
  const [activeIdx, setActiveIdx] = useState(0);
  const [atPos, setAtPos] = useState(-1); // @ 在 text 里的位置
  const [slashPos, setSlashPos] = useState(-1); // / 在 text 里的位置

  // 自动调整高度
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
  }, [text]);

  // 外部插入文本（文件树点击）
  useEffect(() => {
    if (insertText) {
      setText((prev) => prev + insertText);
      requestAnimationFrame(() => {
        const ta = textareaRef.current;
        if (ta) {
          ta.focus();
          const pos = ta.value.length;
          ta.setSelectionRange(pos, pos);
        }
      });
    }
  }, [insertText]);

  // 检测 @ 或 / 触发补全
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    const pos = ta.selectionStart;
    const before = text.slice(0, pos);
    // @ 文件补全：匹配最后一个未关闭的 @
    const atMatch = before.match(/@([^@\s]*)$/);
    if (atMatch) {
      setAtPos(pos - atMatch[0].length);
      setFileQuery(atMatch[1]);
      setShowFileComplete(true);
      setShowCmdComplete(false);
      fetchFiles(atMatch[1]);
      return;
    }
    // / 命令补全：行首或空格后的 /
    const slashMatch = before.match(/(^|\s)(\/\w*)$/);
    if (slashMatch) {
      // slashMatch[2] 是 "/help" 这样的完整串（含 /）
      // slashPos 指向 / 本身的位置，insertCmd 时 before 切到 / 之前，替换整个 /xxx
      setSlashPos(pos - slashMatch[2].length);
      const q = slashMatch[2].slice(1).toLowerCase(); // 去掉 / 做过滤
      const filtered = SLASH_COMMANDS.filter((c) => c.cmd.toLowerCase().includes(q));
      setCmdList(filtered.length ? filtered : SLASH_COMMANDS);
      setShowCmdComplete(true);
      setShowFileComplete(false);
      setActiveIdx(0);
      return;
    }
    setShowFileComplete(false);
    setShowCmdComplete(false);
  }, [text]);

  const fetchFiles = useCallback(async (query: string) => {
    try {
      const path = query.includes("/") ? query.slice(0, query.lastIndexOf("/") + 1) : "";
      const url = `${API_BASE}/files?path=${encodeURIComponent(path)}&max_depth=1`;
      const res = await fetch(url);
      if (!res.ok) return;
      const data = await res.json();
      const q = query.includes("/") ? query.slice(query.lastIndexOf("/") + 1) : query;
      const filtered = (data.entries as FileEntry[]).filter((e) =>
        e.name.toLowerCase().includes(q.toLowerCase())
      );
      setFileList(filtered.slice(0, 10));
      setActiveIdx(0);
    } catch {
      setFileList([]);
    }
  }, []);

  const insertFile = (entry: FileEntry) => {
    const ta = textareaRef.current;
    if (!ta || atPos < 0) return;
    const pos = ta.selectionStart;
    const before = text.slice(0, atPos);
    const after = text.slice(pos);
    const newText = before + "@" + entry.path + (entry.is_dir ? "/" : " ") + after;
    setText(newText);
    setShowFileComplete(false);
    const newPos = (before + "@" + entry.path + (entry.is_dir ? "/" : " ")).length;
    requestAnimationFrame(() => {
      ta.focus();
      ta.setSelectionRange(newPos, newPos);
    });
  };

  const insertCmd = (cmd: string) => {
    const ta = textareaRef.current;
    if (!ta || slashPos < 0) return;
    const pos = ta.selectionStart;
    const before = text.slice(0, slashPos);
    const after = text.slice(pos);
    const newText = before + cmd + " " + after;
    setText(newText);
    setShowCmdComplete(false);
    const newPos = (before + cmd + " ").length;
    requestAnimationFrame(() => {
      ta.focus();
      ta.setSelectionRange(newPos, newPos);
    });
  };

  const handleSubmit = () => {
    const trimmed = text.trim();
    if (!trimmed || disabled || isStreaming) return;
    onSend(trimmed);
    setText("");
    setShowFileComplete(false);
    setShowCmdComplete(false);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // 补全列表激活时，上下键选择、Tab/Enter 确认
    if (showFileComplete && fileList.length > 0) {
      if (e.key === "ArrowDown") { e.preventDefault(); setActiveIdx((i) => (i + 1) % fileList.length); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); setActiveIdx((i) => (i - 1 + fileList.length) % fileList.length); return; }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        e.preventDefault();
        insertFile(fileList[activeIdx]);
        return;
      }
      if (e.key === "Escape") { e.preventDefault(); setShowFileComplete(false); return; }
    }
    if (showCmdComplete && cmdList.length > 0) {
      if (e.key === "ArrowDown") { e.preventDefault(); setActiveIdx((i) => (i + 1) % cmdList.length); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); setActiveIdx((i) => (i - 1 + cmdList.length) % cmdList.length); return; }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        e.preventDefault();
        insertCmd(cmdList[activeIdx].cmd);
        return;
      }
      if (e.key === "Escape") { e.preventDefault(); setShowCmdComplete(false); return; }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  return (
    <div className="border-t border-border bg-bg-tertiary px-4 py-3 relative">
      {/* 文件补全弹窗 */}
      {showFileComplete && fileList.length > 0 && (
        <div className="absolute bottom-full left-4 right-4 max-w-2xl mx-auto mb-1 bg-bg-secondary border border-border rounded-lg shadow-xl py-1 max-h-60 overflow-y-auto z-50">
          {fileList.map((f, i) => (
            <button
              key={f.path}
              onMouseEnter={() => setActiveIdx(i)}
              onClick={() => insertFile(f)}
              className={`w-full text-left px-3 py-1.5 text-xs flex items-center gap-2 ${
                i === activeIdx ? "bg-bg-tertiary" : ""
              }`}
            >
              <span className={f.is_dir ? "text-amber-400" : "text-blue-400"}>
                {f.is_dir ? "▸" : "▤"}
              </span>
              <span className="font-mono text-text-secondary">{f.name}</span>
              {f.path !== f.name && <span className="text-text-tertiary truncate">{f.path}</span>}
            </button>
          ))}
        </div>
      )}
      {/* 命令补全弹窗 */}
      {showCmdComplete && cmdList.length > 0 && (
        <div className="absolute bottom-full left-4 right-4 max-w-2xl mx-auto mb-1 bg-bg-secondary border border-border rounded-lg shadow-xl py-1 z-50">
          {cmdList.map((c, i) => (
            <button
              key={c.cmd}
              onMouseEnter={() => setActiveIdx(i)}
              onClick={() => insertCmd(c.cmd)}
              className={`w-full text-left px-3 py-1.5 text-xs flex items-center gap-3 ${
                i === activeIdx ? "bg-bg-tertiary" : ""
              }`}
            >
              <span className="font-mono text-accent w-20">{c.cmd}</span>
              <span className="text-text-tertiary">{c.desc}</span>
            </button>
          ))}
        </div>
      )}

      <div className="flex items-end gap-2 max-w-4xl mx-auto">
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={disabled ? "引擎启动中..." : "输入消息，Enter 发送，Shift+Enter 换行，@ 引用文件，/ 命令"}
          disabled={disabled}
          rows={1}
          className="flex-1 resize-none rounded-xl bg-bg-input border border-border px-4 py-3 text-sm text-text-primary placeholder:text-text-tertiary focus:outline-none focus:border-accent/50 transition-colors disabled:opacity-50"
        />
        {isStreaming ? (
          <button
            onClick={onCancel}
            className="flex items-center justify-center w-10 h-10 rounded-xl bg-red-500/20 text-red-400 hover:bg-red-500/30 transition-colors"
            title="停止生成"
          >
            <Square size={16} />
          </button>
        ) : (
          <button
            onClick={handleSubmit}
            disabled={!text.trim() || disabled}
            className="flex items-center justify-center w-10 h-10 rounded-xl bg-accent text-white hover:opacity-90 transition-opacity disabled:opacity-30 disabled:cursor-not-allowed"
            title="发送"
          >
            <Send size={16} />
          </button>
        )}
      </div>
    </div>
  );
}
