// 设置面板：API Key / 模型 / 权限模式（读 /api/config，暂不写回——只读展示 + 权限模式可切）
import { useState, useEffect } from "react";
import { X, Eye, EyeOff } from "lucide-react";
import { useChatStore } from "../store/chatStore";

const API_BASE = "http://127.0.0.1:7800/api";

const MODES = [
  { value: "default", label: "默认（写入询问）" },
  { value: "acceptEdits", label: "自动接受编辑" },
  { value: "plan", label: "规划模式" },
  { value: "bypass", label: "跳过所有检查" },
];

const PROTOCOL_LABELS: Record<string, string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  "openai-compat": "OpenAI 兼容",
};

interface Props {
  onClose: () => void;
  onSwitchMode: (mode: string) => void;
}

export function SettingsPanel({ onClose, onSwitchMode }: Props) {
  const engineInfo = useChatStore((s) => s.engineInfo);
  const [config, setConfig] = useState<{
    providers: Array<{ name: string; protocol: string; base_url: string; model: string; thinking: boolean; api_key: string }>;
    permission_mode: string;
  } | null>(null);
  const [showKey, setShowKey] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`${API_BASE}/config`)
      .then((r) => r.json())
      .then((data) => setConfig(data))
      .catch((e) => console.error("load config failed", e))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="w-full max-w-lg rounded-xl border border-border bg-bg-secondary shadow-2xl">
        {/* 头部 */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-border">
          <h2 className="text-base font-medium text-text-primary">设置</h2>
          <button
            onClick={onClose}
            className="p-1 rounded-lg text-text-tertiary hover:text-text-secondary hover:bg-bg-tertiary transition-colors"
          >
            <X size={16} />
          </button>
        </div>

        {/* 内容 */}
        <div className="px-5 py-4 space-y-4 max-h-[70vh] overflow-y-auto">
          {loading && <div className="text-center text-text-tertiary text-sm py-8">加载中...</div>}
          {!loading && !config && (
            <div className="text-center text-red-400 text-sm py-8">配置加载失败</div>
          )}
          {config && (
            <>
              {/* Provider 配置 */}
              <div>
                <div className="text-xs font-medium text-text-secondary mb-2">模型供应商</div>
                {config.providers.map((p, i) => (
                  <div key={i} className="bg-bg-tertiary rounded-lg p-3 space-y-2">
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-medium text-text-primary">{p.name}</span>
                      <span className="text-[10px] px-2 py-0.5 rounded bg-bg-primary text-text-tertiary font-mono">
                        {PROTOCOL_LABELS[p.protocol] || p.protocol}
                      </span>
                    </div>
                    <div className="grid grid-cols-2 gap-2 text-xs">
                      <div>
                        <div className="text-text-tertiary">模型</div>
                        <div className="text-text-secondary font-mono truncate">{p.model}</div>
                      </div>
                      <div>
                        <div className="text-text-tertiary">思维链</div>
                        <div className="text-text-secondary">{p.thinking ? "开启" : "关闭"}</div>
                      </div>
                    </div>
                    <div className="text-xs">
                      <div className="text-text-tertiary">接口地址</div>
                      <div className="text-text-secondary font-mono truncate">{p.base_url}</div>
                    </div>
                    <div className="text-xs">
                      <div className="text-text-tertiary">接口密钥</div>
                      <div className="flex items-center gap-2">
                        <code className="flex-1 font-mono text-text-secondary truncate bg-bg-primary px-2 py-1 rounded">
                          {showKey ? p.api_key : "•".repeat(Math.min(20, p.api_key.length || 8))}
                        </code>
                        <button
                          onClick={() => setShowKey((v) => !v)}
                          className="text-text-tertiary hover:text-text-secondary p-1"
                        >
                          {showKey ? <EyeOff size={14} /> : <Eye size={14} />}
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>

              {/* 权限模式 */}
              <div>
                <div className="text-xs font-medium text-text-secondary mb-2">权限模式</div>
                <div className="space-y-1">
                  {MODES.map((m) => (
                    <button
                      key={m.value}
                      onClick={() => onSwitchMode(m.value)}
                      className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left transition-colors ${
                        (engineInfo?.permission_mode || config.permission_mode) === m.value
                          ? "bg-accent/10 border border-accent/30"
                          : "bg-bg-tertiary border border-transparent hover:border-border"
                      }`}
                    >
                      <div
                        className={`w-3 h-3 rounded-full border ${
                          (engineInfo?.permission_mode || config.permission_mode) === m.value
                            ? "border-accent bg-accent"
                            : "border-text-tertiary"
                        }`}
                      />
                      <div>
                        <div className="text-sm text-text-primary">{m.label}</div>
                      </div>
                    </button>
                  ))}
                </div>
              </div>

              {/* 提示 */}
              <div className="text-[11px] text-text-tertiary bg-bg-tertiary rounded-lg p-3">
                提示：修改接口密钥和模型请编辑 <code className="font-mono text-text-secondary">.codebot/config.yaml</code> 后重启。
                权限模式可即时切换。
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
