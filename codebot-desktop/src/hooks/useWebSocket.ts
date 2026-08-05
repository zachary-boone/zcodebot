// WebSocket 连接 + 事件分发 hook
import { useEffect, useRef, useCallback } from "react";
import { useChatStore } from "../store/chatStore";
import type { ClientMessage, ServerMessage, ToolCall } from "../types";

const WS_URL = "ws://127.0.0.1:7800/ws/chat";
const RECONNECT_DELAY = 2000;

export function useWebSocket() {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const currentAssistantId = useRef<string | null>(null);
  const shouldReconnect = useRef(true);

  const store = useChatStore();

  const handleMessage = useCallback(
    (msg: ServerMessage) => {
      switch (msg.type) {
        case "engine_initializing":
          store.setEngineStatus("initializing");
          break;
        case "engine_ready":
          store.setEngineStatus("ready");
          store.setEngineInfo({
            provider: msg.provider,
            model: msg.model,
            permission_mode: msg.permission_mode,
          });
          break;
        case "stream_text":
          if (currentAssistantId.current) {
            store.appendStreamText(currentAssistantId.current, msg.text);
          }
          break;
        case "thinking":
          if (currentAssistantId.current) {
            store.appendThinking(currentAssistantId.current, msg.text);
          }
          break;
        case "tool_use": {
          if (currentAssistantId.current) {
            const tool: ToolCall = {
              tool_id: msg.tool_id,
              tool_name: msg.tool_name,
              arguments: msg.arguments,
              status: "running",
            };
            store.addToolUse(currentAssistantId.current, tool);
          }
          break;
        }
        case "tool_result":
          if (currentAssistantId.current) {
            store.updateToolResult(currentAssistantId.current, msg.tool_id, {
              output: msg.output,
              is_error: msg.is_error,
              elapsed: msg.elapsed,
              truncated: msg.truncated,
              status: msg.is_error ? "error" : "complete",
            });
          }
          break;
        case "usage":
          if (currentAssistantId.current) {
            store.setMessageUsage(currentAssistantId.current, {
              input_tokens: msg.input_tokens,
              output_tokens: msg.output_tokens,
            });
          }
          break;
        case "permission_request":
          store.setPendingPermission({
            request_id: msg.request_id,
            tool_name: msg.tool_name,
            description: msg.description,
          });
          break;
        case "done":
          if (currentAssistantId.current) {
            store.completeMessage(currentAssistantId.current, "complete");
            currentAssistantId.current = null;
          }
          store.setStreaming(false);
          break;
        case "cancelled":
          if (currentAssistantId.current) {
            store.completeMessage(currentAssistantId.current, "complete");
            currentAssistantId.current = null;
          }
          store.setStreaming(false);
          break;
        case "mode_changed":
          store.updatePermissionMode(msg.mode);
          break;
        case "error":
          if (currentAssistantId.current) {
            store.completeMessage(currentAssistantId.current, "error");
            currentAssistantId.current = null;
          }
          store.setError(msg.message);
          store.setStreaming(false);
          break;
        case "retry":
          // 简单提示，暂不做倒计时 UI
          break;
        case "turn_complete":
        case "loop_complete":
        case "compact":
        case "hook":
          // MVP 阶段先忽略这些次要事件
          break;
      }
    },
    [store]
  );

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;
    store.setEngineStatus("disconnected");

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      console.log("[ws] connected");
    };
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data) as ServerMessage;
        handleMessage(msg);
      } catch (err) {
        console.error("[ws] parse error", err);
      }
    };
    ws.onclose = () => {
      console.log("[ws] closed");
      wsRef.current = null;
      store.setEngineStatus("disconnected");
      if (shouldReconnect.current) {
        reconnectTimer.current = setTimeout(connect, RECONNECT_DELAY);
      }
    };
    ws.onerror = (err) => {
      console.error("[ws] error", err);
    };
  }, [handleMessage, store]);

  const send = useCallback((msg: ClientMessage) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(msg));
    }
  }, []);

  const sendMessage = useCallback(
    (text: string) => {
      store.addUserMessage(text);
      store.setStreaming(true);
      store.setError(null);
      currentAssistantId.current = store.startAssistantMessage();
      send({ type: "send_message", text });
    },
    [send, store]
  );

  const cancel = useCallback(() => {
    send({ type: "cancel" });
  }, [send]);

  const respondPermission = useCallback(
    (requestId: string, decision: "allow" | "deny" | "allow_always") => {
      store.setPendingPermission(null);
      send({ type: "permission_response", request_id: requestId, decision });
    },
    [send, store]
  );

  const switchMode = useCallback(
    (mode: string) => {
      send({ type: "switch_mode", mode });
    },
    [send]
  );

  useEffect(() => {
    shouldReconnect.current = true;
    connect();
    return () => {
      shouldReconnect.current = false;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { sendMessage, cancel, respondPermission, switchMode, ws: wsRef };
}
