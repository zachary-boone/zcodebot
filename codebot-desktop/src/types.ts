// 后端 WebSocket 消息类型定义（与 codebot/server.py 对应）

// 后端 → 前端

export interface SubAgentStatus {
  task_id: string;
  agent_name: string;
  status: "running" | "completed" | "failed" | "cancelled";
  task_description: string;
  result: string;
  progress: {
    tool_call_count: number;
    input_tokens: number;
    output_tokens: number;
    last_activity: string;
  };
}

export type ServerMessage =
  | { type: "engine_initializing" }
  | {
      type: "engine_ready";
      provider: string;
      model: string;
      permission_mode: string;
      work_dir?: string;
    }
  | { type: "stream_text"; text: string }
  | { type: "thinking"; text: string }
  | { type: "retry"; reason: string; wait: number }
  | {
      type: "tool_use";
      tool_name: string;
      tool_id: string;
      arguments: Record<string, unknown>;
    }
  | {
      type: "tool_result";
      tool_id: string;
      tool_name: string;
      output: string;
      is_error: boolean;
      elapsed: number;
      truncated: boolean;
    }
  | { type: "turn_complete"; turn: number }
  | { type: "loop_complete"; total_turns: number }
  | { type: "plan_ready"; plan_path: string; plan_content: string; has_plan: boolean }
  | { type: "usage"; input_tokens: number; output_tokens: number }
  | { type: "error"; message: string }
  | { type: "compact"; before_tokens: number; message: string }
  | {
      type: "permission_request";
      request_id: string;
      tool_name: string;
      description: string;
      is_dangerous: boolean;
    }
  | { type: "done" }
  | { type: "cancelled" }
  | { type: "mode_changed"; mode: string }
  | { type: "session_switched"; session_id: string }
  | { type: "new_session_ready" }
  | { type: "workdir_changed"; work_dir: string }
  | {
      type: "hook";
      hook_id: string;
      event: string;
      output: string;
      success: boolean;
    }
  | {
      type: "subagent_status";
      task_id: string;
      agent_name: string;
      status: "running" | "completed" | "failed" | "cancelled";
      task_description: string;
      result: string;
      progress: {
        tool_call_count: number;
        input_tokens: number;
        output_tokens: number;
        last_activity: string;
      };
    };

// 前端 → 后端
export type ClientMessage =
  | { type: "send_message"; text: string }
  | { type: "permission_response"; request_id: string; decision: "allow" | "deny" | "allow_always" }
  | { type: "cancel" }
  | { type: "switch_mode"; mode: string }
  | { type: "switch_session"; session_id: string }
  | { type: "new_session" }
  | { type: "set_workdir"; path: string }
  | { type: "plan_decision"; decision: "yolo" | "manual" | "feedback"; feedback?: string };

export interface PendingPlan {
  plan_path: string;
  plan_content: string;
  has_plan: boolean;
}

// 前端 store 里一条消息（由多个事件聚合而成）
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  // assistant 消息的 markdown 文本（逐步追加 stream_text）
  content: string;
  // 思考过程文本
  thinking: string;
  // 当前轮次的工作阶段，仅用于过程展示，不参与最终答复。
  activity?: string;
  // 关联的工具调用
  toolCalls: ToolCall[];
  // 状态
  status: "streaming" | "complete" | "error";
  // token 用量（最近一次）
  usage?: { input_tokens: number; output_tokens: number };
}

export interface ToolCall {
  tool_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  output?: string;
  is_error?: boolean;
  elapsed?: number;
  truncated?: boolean;
  status: "running" | "complete" | "error";
}

export interface PermissionRequest {
  request_id: string;
  tool_name: string;
  description: string;
  is_dangerous: boolean;
}

// Electron preload 注入到 window.codebot 的原生能力。
// 仅在 Electron 运行时存在；纯浏览器（如 vite dev 无 electron）下 codebot 为 undefined。
// 前端调用 selectDirectory 前需判空，降级为手动输入路径。
export interface CodebotBridge {
  platform: string;
  versions: { electron: string; chrome: string; node: string };
  hasNativeDialog?: boolean;
  selectDirectory?: (opts?: { title?: string; message?: string }) => Promise<string>;
}

// 模块内用 declare global 才能把 Window 接口合并到全局类型上，
// 否则 App.tsx 里访问 window.codebot 会报 TS2339。
declare global {
  interface Window {
    codebot?: CodebotBridge;
  }
}

