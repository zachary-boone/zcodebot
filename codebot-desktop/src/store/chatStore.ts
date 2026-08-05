// 消息状态管理（Zustand）
import { create } from "zustand";
import type { ChatMessage, PermissionRequest, ToolCall } from "../types";

interface ChatStore {
  messages: ChatMessage[];
  pendingPermission: PermissionRequest | null;
  isStreaming: boolean;
  engineStatus: "disconnected" | "initializing" | "ready" | "error";
  engineInfo: { provider: string; model: string; permission_mode: string } | null;
  errorMessage: string | null;
  // 当前工作目录：由后端 engine_ready / workdir_changed 推送；
  // 切换工作目录会重建 runtime 并清空会话，前端据此刷新侧栏与文件树。
  workDir: string | null;

  // 动作
  setEngineStatus: (s: ChatStore["engineStatus"]) => void;
  setEngineInfo: (info: ChatStore["engineInfo"]) => void;
  updatePermissionMode: (mode: string) => void;
  setWorkDir: (dir: string | null) => void;
  addUserMessage: (text: string) => void;
  startAssistantMessage: () => string; // 返回消息 id
  appendStreamText: (msgId: string, text: string) => void;
  appendThinking: (msgId: string, text: string) => void;
  addToolUse: (msgId: string, tool: ToolCall) => void;
  updateToolResult: (msgId: string, toolId: string, result: Partial<ToolCall>) => void;
  setMessageUsage: (msgId: string, usage: { input_tokens: number; output_tokens: number }) => void;
  completeMessage: (msgId: string, status: "complete" | "error") => void;
  setStreaming: (s: boolean) => void;
  setPendingPermission: (p: PermissionRequest | null) => void;
  setError: (msg: string | null) => void;
  reset: () => void;
}

let idCounter = 0;
const genId = () => `msg-${Date.now()}-${idCounter++}`;

export const useChatStore = create<ChatStore>((set) => ({
  messages: [],
  pendingPermission: null,
  isStreaming: false,
  engineStatus: "disconnected",
  engineInfo: null,
  errorMessage: null,
  workDir: null,

  setEngineStatus: (s) => set({ engineStatus: s }),
  setEngineInfo: (info) => set({ engineInfo: info }),
  updatePermissionMode: (mode) =>
    set((state) => ({
      engineInfo: state.engineInfo ? { ...state.engineInfo, permission_mode: mode } : null,
    })),
  setWorkDir: (dir) => set({ workDir: dir }),
  addUserMessage: (text) =>
    set((state) => ({
      messages: [
        ...state.messages,
        { id: genId(), role: "user", content: text, thinking: "", toolCalls: [], status: "complete" },
      ],
    })),
  startAssistantMessage: () => {
    const id = genId();
    set((state) => ({
      messages: [
        ...state.messages,
        { id, role: "assistant", content: "", thinking: "", toolCalls: [], status: "streaming" },
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
        m.id === msgId ? { ...m, thinking: m.thinking + text } : m
      ),
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
  setError: (msg) => set({ errorMessage: msg }),
  reset: () => set({ messages: [], pendingPermission: null, isStreaming: false, errorMessage: null }),
}));
