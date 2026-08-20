// WebSocket 连接 + 事件分发 hook
import { useEffect, useRef, useCallback } from "react";
import { useChatStore } from "../store/chatStore";
import type { ClientMessage, ServerMessage, ToolCall } from "../types";

const WS_URL = "ws://127.0.0.1:7800/ws/chat";
const RECONNECT_DELAY = 2000;

// 回答内容流式渲染节流参数。
// DeepSeek 等模型生成最终回答时 token 速度极快，chunk 间隔 < 16ms，
// React 会把同一帧内的多次 set 合并渲染，看起来"一下子蹦出来"。
// 这里把 stream_text 增量累积到缓冲，按固定节奏（约 30fps）flush 到 store，
// 每次只追加一小段，形成打字机般的流式效果。
const STREAM_FLUSH_INTERVAL_MS = 33; // ~30fps
const STREAM_FLUSH_MAX_CHARS = 60; // 单次 flush 超过此字符数则立即渲染，避免长文本堆积

export function useWebSocket() {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const currentAssistantId = useRef<string | null>(null);
  const shouldReconnect = useRef(true);
  // 流式文本节流缓冲：pendingStream 累积增量，flushTimer 定时渲染
  const pendingStream = useRef("");
  const flushTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const store = useChatStore();
  const notifySessionsChanged = useCallback(() => {
    window.dispatchEvent(new CustomEvent("codebot:sessions-changed"));
  }, []);

  const flushStreamText = useCallback(() => {
    if (flushTimer.current) {
      clearTimeout(flushTimer.current);
      flushTimer.current = null;
    }
    if (pendingStream.current && currentAssistantId.current) {
      store.appendStreamText(currentAssistantId.current, pendingStream.current);
      pendingStream.current = "";
    }
  }, [store]);

  const scheduleFlush = useCallback(() => {
    if (flushTimer.current) return;
    flushTimer.current = setTimeout(flushStreamText, STREAM_FLUSH_INTERVAL_MS);
  }, [flushStreamText]);

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
          // 后端在首次 ready 与切换工作目录重建后会发 engine_ready；
          // work_dir 可选字段，存在时同步到 store 供 UI 显示。
          if (msg.work_dir) {
            store.setWorkDir(msg.work_dir);
          }
          break;
        case "stream_text":
          if (currentAssistantId.current) {
            // 节流渲染：先累积到缓冲，超阈值立即 flush，否则定时 flush
            pendingStream.current += msg.text;
            if (pendingStream.current.length >= STREAM_FLUSH_MAX_CHARS) {
              flushStreamText();
            } else {
              scheduleFlush();
            }
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
          // 后端发的是本次 turn 的增量 token：
          // 1) 累加到全局 sessionUsage（状态栏显示累计）
          // 2) 记录到当前消息的 usage 字段（消息底部显示本次用量）
          store.addSessionUsage({
            input_tokens: msg.input_tokens,
            output_tokens: msg.output_tokens,
          });
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
          // 结束前 flush 流式缓冲，确保最后一段内容不丢失
          flushStreamText();
          if (currentAssistantId.current) {
            store.completeMessage(currentAssistantId.current, "complete");
            currentAssistantId.current = null;
          }
          store.setStreaming(false);
          notifySessionsChanged();
          break;
        case "cancelled":
          flushStreamText();
          if (currentAssistantId.current) {
            store.completeMessage(currentAssistantId.current, "complete");
            currentAssistantId.current = null;
          }
          store.setStreaming(false);
          notifySessionsChanged();
          break;
        case "mode_changed":
          store.updatePermissionMode(msg.mode);
          break;
        case "session_switched":
          // 会话已在后端切换，前端清空当前消息（历史由 REST 加载）
          store.resetSessionUsage();
          break;
        case "new_session_ready":
          // 正常新会话时前端已 reset；删除当前活动会话时后端也会发此事件，
          // 这里统一清空消息/统计，保证界面回到干净状态。
          store.setError(null);
          store.resetSessionUsage();
          useChatStore.setState({ messages: [] });
          break;
        case "workdir_changed":
          // 工作目录已切换：后端会紧接着发 engine_ready 重建 UI 状态。
          // 这里先清空当前会话消息与挂起的权限请求（它们属于旧 work_dir），
          // 并刷新 store 的 workDir，让侧栏 / 文件树 / 状态栏立即反映新目录。
          store.setWorkDir(msg.work_dir);
          store.setPendingPermission(null);
          store.setStreaming(false);
          store.resetSessionUsage();
          useChatStore.setState({ messages: [] });
          // 通知侧栏重新拉取会话列表（新 work_dir 下的 .codebot/sessions/）
          notifySessionsChanged();
          break;
        case "error":
          flushStreamText();
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
    [store, notifySessionsChanged, flushStreamText, scheduleFlush]
  );

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;
    store.setEngineStatus("disconnected");

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    // 心跳：30s 发 ping，保持连接
    const pingTimer = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "ping" }));
      }
    }, 30000);

    ws.onopen = () => {
      console.log("[ws] connected");
      store.setEngineStatus("initializing");
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
      clearInterval(pingTimer);
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

  const switchSession = useCallback(
    (sessionId: string) => {
      send({ type: "switch_session", session_id: sessionId });
    },
    [send]
  );

  const newSession = useCallback(() => {
    send({ type: "new_session" });
  }, [send]);

  const setWorkDir = useCallback(
    (path: string) => {
      // 切换工作目录：后端会取消运行中的 agent、关闭旧 session、os.chdir、
      // 重建 runtime，然后回 workdir_changed + engine_ready。前端只需发出请求，
      // 真正的状态更新在 handleMessage 的 workdir_changed 分支里完成。
      send({ type: "set_workdir", path });
    },
    [send]
  );

  useEffect(() => {
    shouldReconnect.current = true;
    connect();
    return () => {
      shouldReconnect.current = false;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      // 清理流式节流定时器与未 flush 的缓冲
      if (flushTimer.current) clearTimeout(flushTimer.current);
      pendingStream.current = "";
      wsRef.current?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { sendMessage, cancel, respondPermission, switchMode, switchSession, newSession, setWorkDir, ws: wsRef };
}
