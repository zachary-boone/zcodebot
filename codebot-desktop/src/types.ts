// 后端 WebSocket 消息类型定义（与 codebot/server.py 对应）

// 后端 → 前端
export type ServerMessage =
  | { type: "engine_initializing" }
  | {
      type: "engine_ready";
      provider: string;
      model: string;
      permission_mode: string;
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
  | { type: "usage"; input_tokens: number; output_tokens: number }
  | { type: "error"; message: string }
  | { type: "compact"; before_tokens: number; message: string }
  | {
      type: "permission_request";
      request_id: string;
      tool_name: string;
      description: string;
    }
  | { type: "done" }
  | { type: "cancelled" }
  | { type: "mode_changed"; mode: string }
  | { type: "session_switched"; session_id: string }
  | {
      type: "hook";
      hook_id: string;
      event: string;
      output: string;
      success: boolean;
    };

// 前端 → 后端
export type ClientMessage =
  | { type: "send_message"; text: string }
  | { type: "permission_response"; request_id: string; decision: "allow" | "deny" | "allow_always" }
  | { type: "cancel" }
  | { type: "switch_mode"; mode: string }
  | { type: "switch_session"; session_id: string };

// 前端 store 里一条消息（由多个事件聚合而成）
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  // assistant 消息的 markdown 文本（逐步追加 stream_text）
  content: string;
  // 思考过程文本
  thinking: string;
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
}
