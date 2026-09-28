// 消息状态管理（Zustand）
import { create } from "zustand";
import type { ChatMessage, PermissionRequest, PendingPlan, ToolCall, SubAgentStatus } from "../types";

interface ChatStore {
  messages: ChatMessage[];
  pendingPermission: PermissionRequest | null;
  pendingPlan: PendingPlan | null;
  isStreaming: boolean;
  engineStatus: "disconnected" | "initializing" | "ready" | "error";
  engineInfo: { provider: string; model: string; permission_mode: string } | null;
  errorMessage: string | null;
  // 当前工作目录：由后端 engine_ready / workdir_changed 推送；
  // 切换工作目录会重建 runtime 并清空会话，前端据此刷新侧栏与文件树。
  workDir: string | null;
  // 本会话累计 token 用量（后端 UsageEvent 发的是单次增量，前端在此累加）。
  // 状态栏显示用；新会话 / 切换会话 / 切换目录时清零。
  sessionUsage: { input_tokens: number; output_tokens: number };
  subAgentStatuses: SubAgentStatus[];

  // 动作
  setEngineStatus: (s: ChatStore["engineStatus"]) => void;
  setEngineInfo: (info: ChatStore["engineInfo"]) => void;
  updatePermissionMode: (mode: string) => void;
  setWorkDir: (dir: string | null) => void;
  addSessionUsage: (usage: { input_tokens: number; output_tokens: number }) => void;
  resetSessionUsage: () => void;
  addUserMessage: (text: string) => void;
  startAssistantMessage: () => string; // 返回消息 id
  appendStreamText: (msgId: string, text: string) => void;
  appendThinking: (msgId: string, text: string) => void;
  setActivity: (msgId: string, activity: string) => void;
  addToolUse: (msgId: string, tool: ToolCall) => void;
  updateToolResult: (msgId: string, toolId: string, result: Partial<ToolCall>) => void;
  setMessageUsage: (msgId: string, usage: { input_tokens: number; output_tokens: number }) => void;
  completeMessage: (msgId: string, status: "complete" | "error") => void;
  setStreaming: (s: boolean) => void;
  setPendingPermission: (p: PermissionRequest | null) => void;
  setPendingPlan: (p: PendingPlan | null) => void;
  setError: (msg: string | null) => void;
  updateSubAgentStatus: (status: SubAgentStatus) => void;
  reset: () => void;
}

let idCounter = 0;
const genId = () => `msg-${Date.now()}-${idCounter++}`;

export const useChatStore = create<ChatStore>((set) => ({
  messages: [],
  pendingPermission: null,
  pendingPlan: null,
  isStreaming: false,
  engineStatus: "disconnected",
  engineInfo: null,
  errorMessage: null,
  workDir: null,
  sessionUsage: { input_tokens: 0, output_tokens: 0 },
      subAgentStatuses: [],

  setEngineStatus: (s) => set({ engineStatus: s }),
  setEngineInfo: (info) => set({ engineInfo: info }),
  updatePermissionMode: (mode) =>
    set((state) => ({
      engineInfo: state.engineInfo ? { ...state.engineInfo, permission_mode: mode } : null,
    })),
  setWorkDir: (dir) => set({ workDir: dir }),
  addSessionUsage: (usage) =>
    set((state) => ({
      sessionUsage: {
        input_tokens: state.sessionUsage.input_tokens + usage.input_tokens,
        output_tokens: state.sessionUsage.output_tokens + usage.output_tokens,
      },
    })),
  resetSessionUsage: () => set({ sessionUsage: { input_tokens: 0, output_tokens: 0 } }),
  updateSubAgentStatus: (status: SubAgentStatus) =>
    set((state) => {
      const existing = state.subAgentStatuses.find((s) => s.task_id === status.task_id);
      if (existing) {
        return {
          subAgentStatuses: state.subAgentStatuses.map((s) =>
            s.task_id === status.task_id ? status : s
          ),
        };
      } else {
        return { subAgentStatuses: [...state.subAgentStatuses, status] };
      }
    }),
  addUserMessage: (text) =>
    set((state) => ({
      messages: [
        ...state.messages,
        { id: genId(), role: "user", content: text, thinking: "", activity: "", toolCalls: [], status: "complete" },
      ],
    })),
  startAssistantMessage: () => {
    const id = genId();
    set((state) => ({
      messages: [
        ...state.messages,
        { id, role: "assistant", content: "", thinking: "", activity: "准备开始工作…", toolCalls: [], status: "streaming" },
      ],
    }));
    return id;
  },
  appendStreamText: (msgId, text) =>
    set((state) => ({
      messages: state.messages.map((m) =>
        m.id === msgId ? { ...m, content: m.content + text } : m
      ),
    })),
  appendThinking: (msgId, text) =>
    set((state) => ({
      messages: state.messages.map((m) =>
        m.id === msgId
          ? {
              ...m,
              // 思考仅用于过程面板，设置上限避免长任务无限堆积内存。
              thinking: (m.thinking + text).slice(-20000),
            }
          : m
      ),
    })),
  setActivity: (msgId, activity) =>
    set((state) => ({
      messages: state.messages.map((m) => (m.id === msgId ? { ...m, activity } : m)),
    })),
  addToolUse: (msgId, tool) =>
    set((state) => ({
      messages: state.messages.map((m) =>
        m.id === msgId ? { ...m, toolCalls: [...m.toolCalls, tool] } : m
      ),
    })),
  updateToolResult: (msgId, toolId, result) =>
    set((state) => ({
      messages: state.messages.map((m) =>
        m.id === msgId
          ? {
              ...m,
              toolCalls: m.toolCalls.map((t) =>
                t.tool_id === toolId ? { ...t, ...result } : t
              ),
            }
          : m
      ),
    })),
  setMessageUsage: (msgId, usage) =>
    set((state) => ({
      messages: state.messages.map((m) => (m.id === msgId ? { ...m, usage } : m)),
    })),
  completeMessage: (msgId, status) =>
    set((state) => ({
      messages: state.messages.map((m) => (m.id === msgId ? { ...m, status } : m)),
    })),
  setStreaming: (s) => set({ isStreaming: s }),
  setPendingPermission: (p) => set({ pendingPermission: p }),
  setPendingPlan: (p) => set({ pendingPlan: p }),
  setError: (msg) => set({ errorMessage: msg }),
  reset: () =>
    set({
      messages: [],
      pendingPermission: null,
      pendingPlan: null,
      isStreaming: false,
      errorMessage: null,
      sessionUsage: { input_tokens: 0, output_tokens: 0 },
      subAgentStatuses: [],
    }),
}));


