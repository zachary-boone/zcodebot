// 侧栏：会话列表 + Memory 笔记 + Skills + 设置入口
import { useState, useEffect, useCallback, memo } from "react";
import {
  Plus,
  MessageSquare,
  Trash2,
  Brain,
  Package,
  Settings as SettingsIcon,
  ChevronDown,
  ChevronRight,
  Folder,
  RefreshCw,
  X,
} from "lucide-react";
import {
  fetchSessions,
  deleteSession,
  fetchMemory,
  fetchSkills,
  groupSessionsByDate,
  type SessionMeta,
  type MemoryItem,
  type SkillItem,
  type FileEntry,
} from "../hooks/useApi";

const API_BASE = "http://127.0.0.1:7800/api";

type Tab = "sessions" | "memory" | "skills" | "files";

interface Props {
  onNewSession: () => void;
  onOpenSettings: () => void;
  onClose: () => void;
  onSwitchSession: (sessionId: string) => void;
  onInsertFile: (path: string) => void;
}

export const Sidebar = memo(function Sidebar({ onNewSession, onOpenSettings, onClose, onSwitchSession, onInsertFile }: Props) {
  const [tab, setTab] = useState<Tab>("sessions");
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [expandedMem, setExpandedMem] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      if (tab === "sessions") setSessions(await fetchSessions());
      else if (tab === "memory") setMemories(await fetchMemory());
      else if (tab === "skills") setSkills(await fetchSkills());
    } catch (e) {
      console.error("sidebar refresh failed", e);
    } finally {
      setLoading(false);
    }
  }, [tab]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleDelete = async (id: string) => {
    if (!confirm("确认删除此会话？")) return;
    const ok = await deleteSession(id);
    if (ok) setSessions((prev) => prev.filter((s) => s.id !== id));
  };

  const grouped = groupSessionsByDate(sessions);

  return (
    <div className="flex flex-col w-64 h-full bg-bg-secondary border-r border-border">
      {/* 顶部：新会话 + 关闭 */}
      <div className="flex items-center gap-2 px-3 py-2.5 border-b border-border">
        <button
          onClick={onNewSession}
          className="flex-1 flex items-center justify-center gap-1.5 py-1.5 rounded-lg bg-accent text-white text-xs hover:opacity-90 transition-opacity"
        >
          <Plus size={14} />
          新会话
        </button>
        <button
          onClick={onClose}
          className="p-1.5 rounded-lg text-text-tertiary hover:text-text-secondary hover:bg-bg-tertiary transition-colors"
          title="收起侧栏"
        >
          <X size={14} />
        </button>
      </div>

      {/* Tab 切换 */}
      <div className="flex border-b border-border">
        {[
          { key: "sessions" as Tab, icon: MessageSquare, label: "会话" },
          { key: "files" as Tab, icon: Folder, label: "文件" },
          { key: "memory" as Tab, icon: Brain, label: "记忆" },
          { key: "skills" as Tab, icon: Package, label: "技能" },
        ].map(({ key, icon: Icon, label }) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`flex-1 flex flex-col items-center gap-0.5 py-2 text-[11px] transition-colors ${
              tab === key ? "text-accent border-b-2 border-accent" : "text-text-tertiary hover:text-text-secondary"
            }`}
          >
            <Icon size={15} />
            {label}
          </button>
        ))}
      </div>

      {/* 内容区 */}
      <div className="flex-1 overflow-y-auto">
        {/* 刷新按钮 */}
        <div className="flex justify-end px-2 py-1">
          <button
            onClick={refresh}
            disabled={loading}
            className="p-1 text-text-tertiary hover:text-text-secondary transition-colors disabled:opacity-30"
            title="刷新"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
          </button>
        </div>

        {tab === "sessions" && (
          <div className="px-2 pb-2">
            {Object.keys(grouped).length === 0 && !loading && (
              <div className="text-center text-text-tertiary text-xs py-8">暂无会话</div>
            )}
            {Object.entries(grouped).map(([group, items]) => (
              <div key={group} className="mb-3">
                <div className="text-[10px] text-text-tertiary px-2 py-1 font-medium">{group}</div>
                {items.map((s) => (
                  <div
                    key={s.id}
                    onClick={() => onSwitchSession(s.id)}
                    className="group flex items-start gap-2 px-2 py-1.5 rounded-lg hover:bg-bg-tertiary transition-colors cursor-pointer"
                  >
                    <MessageSquare size={12} className="mt-0.5 text-text-tertiary flex-shrink-0" />
                    <div className="flex-1 min-w-0">
                      <div className="text-xs text-text-primary truncate">
                        {s.title || "（无标题会话）"}
                      </div>
                      {s.summary && (
                        <div className="text-[10px] text-text-tertiary truncate">{s.summary}</div>
                      )}
                      <div className="text-[10px] text-text-tertiary mt-0.5">
                        {s.message_count} 条 · {s.total_tokens.toLocaleString()} tok
                      </div>
                    </div>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        handleDelete(s.id);
                      }}
                      className="opacity-0 group-hover:opacity-100 p-1 text-text-tertiary hover:text-red-400 transition-all"
                      title="删除"
                    >
                      <Trash2 size={11} />
                    </button>
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}

        {tab === "memory" && (
          <div className="px-2 pb-2">
            {memories.length === 0 && !loading && (
              <div className="text-center text-text-tertiary text-xs py-8">暂无记忆笔记</div>
            )}
            {memories.map((m) => (
              <div key={m.filename + m.scope} className="mb-1">
                <button
                  onClick={() => setExpandedMem(expandedMem === m.filename ? null : m.filename)}
                  className="w-full flex items-start gap-2 px-2 py-1.5 rounded-lg hover:bg-bg-tertiary transition-colors text-left"
                >
                  {expandedMem === m.filename ? (
                    <ChevronDown size={12} className="mt-0.5 text-text-tertiary flex-shrink-0" />
                  ) : (
                    <ChevronRight size={12} className="mt-0.5 text-text-tertiary flex-shrink-0" />
                  )}
                  <div className="flex-1 min-w-0">
                    <div className="text-xs text-text-primary truncate">
                      {m.description || m.filename}
                    </div>
                    <div className="text-[10px] text-text-tertiary">
                      <span className={m.scope === "user" ? "text-blue-400" : "text-amber-400"}>
                        {m.scope === "user" ? "用户" : "项目"}
                      </span>
                      {" · "}
                      {m.age}
                    </div>
                  </div>
                </button>
                {expandedMem === m.filename && m.content && (
                  <div className="ml-5 mr-1 mt-1 mb-2 p-2 bg-bg-tertiary rounded-lg text-[11px] text-text-secondary whitespace-pre-wrap max-h-48 overflow-y-auto border-l-2 border-border">
                    {m.content}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {tab === "skills" && (
          <div className="px-2 pb-2">
            {skills.length === 0 && !loading && (
              <div className="text-center text-text-tertiary text-xs py-8">暂无技能</div>
            )}
            {skills.map((s) => (
              <div key={s.name} className="px-2 py-1.5 rounded-lg hover:bg-bg-tertiary transition-colors">
                <div className="flex items-center gap-2">
                  <Package size={12} className="text-text-tertiary flex-shrink-0" />
                  <span className="text-xs font-mono text-text-primary">{s.name}</span>
                </div>
                {s.description && (
                  <div className="text-[10px] text-text-tertiary ml-5 mt-0.5">{s.description}</div>
                )}
                {s.source && (
                  <div className="text-[10px] text-text-tertiary ml-5 mt-0.5">
                    <span className="text-purple-400">{s.source}</span>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {tab === "files" && (
          <FileTree onInsertFile={onInsertFile} />
        )}
      </div>

      {/* 底部：设置 */}
      <div className="border-t border-border p-2">
        <button
          onClick={onOpenSettings}
          className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-text-secondary hover:text-text-primary hover:bg-bg-tertiary transition-colors text-xs"
        >
          <SettingsIcon size={14} />
          设置
        </button>
      </div>
    </div>
  );
});

// ---------------------------------------------------------------------------
// 文件树组件
// ---------------------------------------------------------------------------

interface FileNode {
  name: string;
  path: string;
  is_dir: boolean;
  children?: FileNode[];
  loaded?: boolean;
}

function FileTree({ onInsertFile }: { onInsertFile: (path: string) => void }) {
  const [tree, setTree] = useState<FileNode[]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);

  const loadDir = useCallback(async (dirPath: string): Promise<FileNode[]> => {
    try {
      const res = await fetch(`${API_BASE}/files?path=${encodeURIComponent(dirPath)}&max_depth=1`);
      if (!res.ok) return [];
      const data = await res.json();
      return (data.entries as FileEntry[])
        .sort((a, b) => {
          // 目录在前，再按名字
          if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
          return a.name.localeCompare(b.name);
        })
        .map((e) => ({
          name: e.name,
          path: dirPath ? `${dirPath}/${e.path}` : e.path,
          is_dir: e.is_dir,
        }));
    } catch {
      return [];
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    loadDir("").then((nodes) => {
      setTree(nodes);
      setLoading(false);
    });
  }, [loadDir]);

  const toggleDir = async (node: FileNode) => {
    const newExpanded = new Set(expanded);
    if (expanded.has(node.path)) {
      newExpanded.delete(node.path);
    } else {
      newExpanded.add(node.path);
      if (!node.loaded) {
        const children = await loadDir(node.path);
        node.children = children;
        node.loaded = true;
        setTree([...tree]);
      }
    }
    setExpanded(newExpanded);
  };

  const renderNode = (node: FileNode, depth: number) => {
    const pad = { paddingLeft: `${depth * 12 + 8}px` };
    if (node.is_dir) {
      const isExpanded = expanded.has(node.path);
      return (
        <div key={node.path}>
          <button
            onClick={(e) => {
              e.stopPropagation();
              toggleDir(node);
            }}
            style={pad}
            className="w-full flex items-center gap-1 py-1 pr-2 rounded hover:bg-bg-tertiary transition-colors text-left"
          >
            {isExpanded ? <ChevronDown size={11} className="text-text-tertiary" /> : <ChevronRight size={11} className="text-text-tertiary" />}
            <span className="text-amber-400 text-xs">▸</span>
            <span className="text-xs text-text-secondary truncate">{node.name}</span>
          </button>
          {isExpanded && node.children && (
            <div>
              {node.children.map((child) => renderNode(child, depth + 1))}
            </div>
          )}
        </div>
      );
    }
    return (
      <button
        key={node.path}
        onClick={(e) => {
          e.stopPropagation();
          onInsertFile(node.path);
        }}
        style={pad}
        className="w-full flex items-center gap-1 py-1 pr-2 rounded hover:bg-bg-tertiary transition-colors text-left group"
      >
        <span className="w-[11px]" />
        <span className="text-blue-400 text-xs">▤</span>
        <span className="text-xs text-text-secondary truncate group-hover:text-text-primary">{node.name}</span>
      </button>
    );
  };

  return (
    <div className="px-1 pb-2">
      {loading && <div className="text-center text-text-tertiary text-xs py-4">加载中...</div>}
      {!loading && tree.length === 0 && <div className="text-center text-text-tertiary text-xs py-8">无文件</div>}
      {tree.map((node) => renderNode(node, 0))}
    </div>
  );
}
