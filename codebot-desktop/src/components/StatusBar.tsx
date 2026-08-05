// 顶部状态栏：模型信息、工作目录切换、权限模式切换、主题切换
import { Shield, Sun, Moon, ChevronDown, Folder, FolderOpen } from "lucide-react";
import { useState, useRef, useEffect } from "react";
import { useChatStore } from "../store/chatStore";
import { useThemeStore } from "../store/themeStore";

const MODES = [
  { value: "default", label: "默认（写入询问）" },
  { value: "acceptEdits", label: "自动接受编辑" },
  { value: "plan", label: "规划模式" },
  { value: "bypass", label: "跳过所有检查" },
];

interface Props {
  onSwitchMode: (mode: string) => void;
  onSwitchWorkDir: () => void;
}

// 把绝对路径截断成 ".../last_two_segments" 形式，避免状态栏被超长路径撑爆。
function shortenPath(p: string): string {
  if (!p) return "";
  const parts = p.replace(/\\/g, "/").split("/").filter(Boolean);
  if (parts.length <= 2) return p;
  return ".../" + parts.slice(-2).join("/");
}

export function StatusBar({ onSwitchMode, onSwitchWorkDir }: Props) {
  const engineStatus = useChatStore((s) => s.engineStatus);
  const engineInfo = useChatStore((s) => s.engineInfo);
  const errorMessage = useChatStore((s) => s.errorMessage);
  const workDir = useChatStore((s) => s.workDir);
  const totalTokens = useChatStore((s) =>
    s.messages.reduce((sum, m) => sum + (m.usage ? m.usage.input_tokens + m.usage.output_tokens : 0), 0)
  );
  const theme = useThemeStore((s) => s.theme);
  const toggleTheme = useThemeStore((s) => s.toggle);

  const [modeOpen, setModeOpen] = useState(false);
  const modeRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (modeRef.current && !modeRef.current.contains(e.target as Node)) setModeOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const statusColor =
    engineStatus === "ready" ? "text-emerald-400" : engineStatus === "error" || errorMessage ? "text-red-400" : "text-text-tertiary";

  const statusText =
    engineStatus === "initializing" ? "引擎启动中..." : engineStatus === "ready" ? "已连接" : engineStatus === "error" ? "引擎错误" : "未连接";

  return (
    <div className="flex items-center justify-between px-4 py-2 border-b border-border bg-bg-secondary text-xs">
      <div className="flex items-center gap-3 min-w-0">
        <span className="font-medium text-text-primary flex-shrink-0">CodeBot</span>
        {engineInfo && (
          <span className="text-text-tertiary hidden sm:inline flex-shrink-0">
            · {engineInfo.provider} / {engineInfo.model}
          </span>
        )}
        {/* 工作目录：点击切换。鼠标悬停时显示完整路径（title）。 */}
        <button
          onClick={onSwitchWorkDir}
          className="flex items-center gap-1 text-text-tertiary hover:text-text-secondary transition-colors min-w-0 group"
          title={workDir ? `点击切换工作目录\n当前: ${workDir}` : "点击设置工作目录"}
        >
          {workDir ? <Folder size={12} className="flex-shrink-0" /> : <FolderOpen size={12} className="flex-shrink-0" />}
          <span className="font-mono truncate max-w-[200px] sm:max-w-[280px]">
            {workDir ? shortenPath(workDir) : "未设置工作目录"}
          </span>
        </button>
      </div>
      <div className="flex items-center gap-3 sm:gap-4">
        {/* token 累计 */}
        {totalTokens > 0 && (
          <span className="text-text-tertiary hidden sm:inline">{totalTokens.toLocaleString()} tok</span>
        )}

        {/* 权限模式切换 */}
        {engineInfo && (
          <div ref={modeRef} className="relative">
            <button
              onClick={() => setModeOpen((v) => !v)}
              className="flex items-center gap-1 text-text-tertiary hover:text-text-secondary transition-colors"
            >
              <Shield size={12} />
              <span>{engineInfo.permission_mode}</span>
              <ChevronDown size={11} />
            </button>
            {modeOpen && (
              <div className="absolute right-0 top-full mt-1 w-48 bg-bg-secondary border border-border rounded-lg shadow-xl py-1 z-50">
                {MODES.map((m) => (
                  <button
                    key={m.value}
                    onClick={() => {
                      onSwitchMode(m.value);
                      setModeOpen(false);
                    }}
                    className={`w-full text-left px-3 py-1.5 hover:bg-bg-tertiary transition-colors ${
                      engineInfo.permission_mode === m.value ? "text-accent" : "text-text-secondary"
                    }`}
                  >
                    <div className="font-mono text-xs">{m.value}</div>
                    <div className="text-[10px] text-text-tertiary">{m.label}</div>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {/* 主题切换 */}
        <button
          onClick={toggleTheme}
          className="text-text-tertiary hover:text-text-secondary transition-colors"
          title={theme === "dark" ? "切换到浅色" : "切换到深色"}
        >
          {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
        </button>

        {/* 连接状态 */}
        <span className={`flex items-center gap-1 ${statusColor}`}>
          <span className={`w-1.5 h-1.5 rounded-full ${
            engineStatus === "ready" ? "bg-emerald-400" : engineStatus === "error" ? "bg-red-400" : "bg-text-tertiary"
          }`} />
          {statusText}
        </span>
      </div>
    </div>
  );
}
