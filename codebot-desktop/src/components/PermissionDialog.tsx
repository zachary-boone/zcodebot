// 权限询问弹窗
import { Shield, ShieldAlert } from "lucide-react";
import type { PermissionRequest } from "../types";

interface Props {
  request: PermissionRequest;
  onRespond: (requestId: string, decision: "allow" | "deny" | "allow_always") => void;
}

export function PermissionDialog({ request, onRespond }: Props) {
  const isDangerous = request.is_dangerous;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div
        className={`w-full max-w-lg rounded-xl border shadow-2xl bg-bg-secondary ${
          isDangerous ? "border-red-500/50" : "border-border"
        }`}
      >
        <div className="flex items-center gap-3 px-5 py-4 border-b border-border">
          {isDangerous ? (
            <ShieldAlert className="text-red-400" size={22} />
          ) : (
            <Shield className="text-accent" size={22} />
          )}
          <h2 className="text-base font-medium text-text-primary">
            权限请求 · {request.tool_name}
          </h2>
        </div>
        <div className="px-5 py-4">
          <pre className="text-sm text-text-secondary whitespace-pre-wrap font-mono bg-bg-tertiary rounded-lg p-3 max-h-60 overflow-auto">
            {request.description}
          </pre>
          {isDangerous && (
            <p className="mt-3 text-sm text-red-400 flex items-center gap-2">
              <ShieldAlert size={16} />
              该操作可能危险，请仔细确认
            </p>
          )}
        </div>
        <div className="flex justify-end gap-2 px-5 py-3 border-t border-border">
          <button
            onClick={() => onRespond(request.request_id, "deny")}
            className="px-4 py-1.5 text-sm rounded-lg border border-border text-text-secondary hover:bg-bg-tertiary transition-colors"
          >
            拒绝
          </button>
          <button
            onClick={() => onRespond(request.request_id, "allow_always")}
            className="px-4 py-1.5 text-sm rounded-lg border border-border text-text-secondary hover:bg-bg-tertiary transition-colors"
          >
            允许并记住
          </button>
          <button
            onClick={() => onRespond(request.request_id, "allow")}
            className="px-4 py-1.5 text-sm rounded-lg bg-accent text-white hover:opacity-90 transition-opacity"
          >
            允许本次
          </button>
        </div>
      </div>
    </div>
  );
}
