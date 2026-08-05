// 会话与 memory 数据获取
const API_BASE = "http://127.0.0.1:7800/api";

export interface SessionMeta {
  id: string;
  title: string;
  summary: string;
  message_count: number;
  total_tokens: number;
  created_at: string;
  last_active: string;
}

export interface FileEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
}

export interface MemoryItem {
  filename: string;
  scope: "user" | "project";
  description: string;
  type: string;
  age: string;
  content: string;
}

export interface SkillItem {
  name: string;
  description: string;
  source: string;
}

// /api/browse 目录浏览结果（切换工作目录对话框用）
export interface BrowseResult {
  path: string;
  parent: string;
  dirs: Array<{ name: string; path: string }>;
  home: string;
  drives: string[];
}

export async function browseDirectory(path: string): Promise<BrowseResult> {
  const res = await fetch(`${API_BASE}/browse?path=${encodeURIComponent(path)}`);
  if (!res.ok) {
    let detail = "浏览目录失败";
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      // ignore
    }
    throw new Error(detail);
  }
  return res.json();
}

export async function fetchSessions(): Promise<SessionMeta[]> {
  const res = await fetch(`${API_BASE}/sessions`);
  if (!res.ok) throw new Error("加载会话失败");
  const data = await res.json();
  return data.sessions;
}

export async function deleteSession(id: string): Promise<boolean> {
  const res = await fetch(`${API_BASE}/sessions/${id}`, { method: "DELETE" });
  if (!res.ok) return false;
  const data = await res.json();
  return data.deleted;
}

export interface SessionMessage {
  role: string;
  content: string;
  thinking?: string;
  tool_uses?: Array<{ tool_name: string; tool_id: string; arguments: Record<string, unknown> }>;
}

export async function fetchSessionMessages(id: string): Promise<SessionMessage[]> {
  const res = await fetch(`${API_BASE}/sessions/${id}/messages`);
  if (!res.ok) throw new Error("加载会话消息失败");
  const data = await res.json();
  return data.messages;
}

export async function fetchMemory(): Promise<MemoryItem[]> {
  const res = await fetch(`${API_BASE}/memory`);
  if (!res.ok) throw new Error("加载 memory 失败");
  const data = await res.json();
  return data.memories;
}

export async function fetchSkills(): Promise<SkillItem[]> {
  const res = await fetch(`${API_BASE}/skills`);
  if (!res.ok) throw new Error("加载 skills 失败");
  const data = await res.json();
  return data.skills;
}

// 按日期分组会话
export function groupSessionsByDate(sessions: SessionMeta[]): Record<string, SessionMeta[]> {
  const groups: Record<string, SessionMeta[]> = {};
  const now = new Date();
  for (const s of sessions) {
    const d = new Date(s.last_active);
    const diffDays = Math.floor((now.getTime() - d.getTime()) / 86400000);
    let key: string;
    if (diffDays === 0) key = "今天";
    else if (diffDays === 1) key = "昨天";
    else if (diffDays < 7) key = "本周";
    else if (diffDays < 30) key = "本月";
    else key = "更早";
    if (!groups[key]) groups[key] = [];
    groups[key].push(s);
  }
  return groups;
}
