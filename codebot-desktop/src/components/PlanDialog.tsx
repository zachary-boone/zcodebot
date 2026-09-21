import { useEffect, useState } from "react";
import { FileText, Play, Shield, MessageSquare, X } from "lucide-react";
import type { PendingPlan } from "../types";

interface Props {
  plan: PendingPlan | null;
  onDecide: (decision: "yolo" | "manual" | "feedback", feedback?: string) => void;
  onDismiss?: () => void;
}

export function PlanDialog({ plan, onDecide, onDismiss }: Props) {
  const [feedbackMode, setFeedbackMode] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    setFeedbackMode(false);
    setFeedback("");
    setDismissed(false);
  }, [plan?.plan_path, plan?.plan_content]);

  useEffect(() => {
    if (!plan || dismissed) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setDismissed(true);
        onDismiss?.();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [plan, dismissed, onDismiss]);

  if (!plan || dismissed) return null;
  const canExecute = plan.has_plan;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="w-full max-w-2xl max-h-[85vh] rounded-xl border border-border bg-bg-secondary shadow-2xl flex flex-col">
        <div className="flex items-center gap-3 px-5 py-4 border-b border-border">
          <FileText className="text-accent" size={22} />
          <div className="min-w-0 flex-1">
            <h2 className="text-base font-medium text-text-primary">计划已完成，等待确认</h2>
            <div className="text-[11px] text-text-tertiary font-mono truncate" title={plan.plan_path}>
              {plan.plan_path}
            </div>
          </div>
          <button
            onClick={() => {
              setDismissed(true);
              onDismiss?.();
            }}
            className="p-1 rounded-lg text-text-tertiary hover:text-text-secondary hover:bg-bg-tertiary"
            title="关闭（保留规划模式）"
          >
            <X size={16} />
          </button>
        </div>

        <div className="px-5 py-4 overflow-y-auto min-h-0">
          {plan.has_plan ? (
            <pre className="text-sm text-text-secondary whitespace-pre-wrap font-mono bg-bg-tertiary rounded-lg p-3 max-h-[48vh] overflow-auto">
              {plan.plan_content}
            </pre>
          ) : (
            <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-300">
              Agent 未写出计划文件，只能反馈让它补充计划。
            </div>
          )}
          {feedbackMode && (
            <textarea
              autoFocus
              value={feedback}
              onChange={(event) => setFeedback(event.target.value)}
              placeholder="告诉 Agent 需要调整什么..."
              className="mt-3 w-full min-h-24 resize-y rounded-lg bg-bg-input border border-border px-3 py-2 text-sm text-text-primary placeholder:text-text-tertiary focus:outline-none focus:border-accent/50"
            />
          )}
        </div>

        <div className="flex flex-wrap justify-end gap-2 px-5 py-3 border-t border-border">
          {feedbackMode ? (
            <>
              <button
                onClick={() => setFeedbackMode(false)}
                className="px-3 py-1.5 text-sm rounded-lg border border-border text-text-secondary hover:bg-bg-tertiary"
              >
                返回
              </button>
              <button
                disabled={!feedback.trim()}
                onClick={() => onDecide("feedback", feedback.trim())}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-accent text-white hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <MessageSquare size={14} />
                提交反馈
              </button>
            </>
          ) : (
            <>
              <button
                onClick={() => setFeedbackMode(true)}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg border border-border text-text-secondary hover:bg-bg-tertiary"
              >
                <MessageSquare size={14} />
                告诉我改什么
              </button>
              <button
                disabled={!canExecute}
                onClick={() => onDecide("manual")}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg border border-border text-text-secondary hover:bg-bg-tertiary disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <Shield size={14} />
                执行（逐个确认）
              </button>
              <button
                disabled={!canExecute}
                onClick={() => onDecide("yolo")}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-accent text-white hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <Play size={14} />
                执行（跳过检查）
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
