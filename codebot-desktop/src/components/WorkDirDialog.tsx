// 切换工作目录对话框：可视化目录浏览器（纯 HTML + 后端 /api/browse）。
//
// 为什么不直接用 Electron 原生 dialog？禁用 GPU 加速的环境（虚拟机/无显卡/
// 远程桌面）下 dialog.showOpenDialog 的模态窗口会触发渲染崩溃导致 electron
// 退出。这里用后端目录浏览接口 + 前端列表渲染，跨平台一致且稳定流畅。
import { useState, useEffect, useRef, useCallback } from "react";
import {
  X,
  FolderInput,
  Folder,
  FolderOpen,
  ChevronUp,
  Home,
  HardDrive,
  Loader2,
  AlertCircle,
  RefreshCw,
  ArrowRight,
} from "lucide-react";
import { useChatStore } from "../store/chatStore";
import { browseDirectory, type BrowseResult } from "../hooks/useApi";

interface Props {
  currentWorkDir: string | null;
  onClose: () => void;
  onConfirm: (path: string) => void;
  // 切换中：后端收到 set_workdir 后重建 runtime 需要时间，期间显示 loading
  switching: boolean;
}

export function WorkDirDialog({ currentWorkDir, onClose, onConfirm, switching }: Props) {
  const errorMessage = useChatStore((s) => s.errorMessage);
  const setError = useChatStore((s) => s.setError);

  // 当前浏览的位置 + 该位置的目录列表
  const [browsePath, setBrowsePath] = useState(currentWorkDir || "");
  const [browse, setBrowse] = useState<BrowseResult | null>(null);
  const [loadingBrowse, setLoadingBrowse] = useState(false);
  const [browseError, setBrowseError] = useState<string | null>(null);
  // 输入框文本（可手动编辑跳转，与浏览位置解耦）
  const [inputPath, setInputPath] = useState(currentWorkDir || "");
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async (path: string) => {
    setLoadingBrowse(true);
    setBrowseError(null);
    try {
      const result = await browseDirectory(path);
      setBrowse(result);
      setBrowsePath(result.path);
      setInputPath(result.path);
    } catch (e) {
      setBrowseError(e instanceof Error ? e.message : "浏览失败");
    } finally {
      setLoadingBrowse(false);
    }
  }, []);

  // 打开时加载当前工作目录（或主目录）的子目录
  useEffect(() => {
    load(currentWorkDir || "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 弹出时聚焦输入框并全选，方便直接编辑路径跳转
  useEffect(() => {
    const t = setTimeout(() => {
      inputRef.current?.focus();
    }, 30);
    return () => clearTimeout(t);
  }, []);

  // 输入框 Enter 跳转到手动输入的路径
  const handleJump = () => {
    const p = inputPath.trim();
    if (!p || p === browsePath) return;
    load(p);
  };

  // 点击目录进入子目录（输入框同步刷新）
  const handleEnterDir = (dirPath: string) => {
    if (switching) return;
    load(dirPath);
    listRef.current?.scrollTo({ top: 0 });
  };

  // 上级目录
  const handleGoUp = () => {
    if (!browse?.parent || switching) return;
    load(browse.parent);
  };

  // 快捷位置：主目录 / 盘符
  const handleQuick = (path: string) => {
    if (switching) return;
    load(path);
  };

  const canConfirm = browsePath.length > 0 && browsePath !== currentWorkDir && !switching;

  const handleConfirm = () => {
    if (!canConfirm) return;
    onConfirm(browsePath);
  };

  // Esc 关闭
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !switching) onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [switching, onClose]);

  const selected = browsePath === currentWorkDir;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="w-full max-w-xl rounded-xl border border-border bg-bg-secondary shadow-2xl flex flex-col max-h-[80vh]">
        {/* 头部 */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-border flex-shrink-0">
          <div className="flex items-center gap-2">
            <FolderInput size={16} className="text-accent" />
            <h2 className="text-base font-medium text-text-primary">切换工作目录</h2>
          </div>
          <button
            onClick={onClose}
            disabled={switching}
            className="p-1 rounded-lg text-text-tertiary hover:text-text-secondary hover:bg-bg-tertiary transition-colors disabled:opacity-30"
          >
            <X size={16} />
          </button>
        </div>

        <div className="px-5 py-3 space-y-3 overflow-y-auto flex-1">
          {/* 当前路径 + 输入跳转 */}
          <div>
            <label className="block text-[11px] text-text-tertiary mb-1">路径</label>
            <div className="flex items-center gap-1.5">
              <input
                ref={inputRef}
                type="text"
                value={inputPath}
                onChange={(e) => {
                  setInputPath(e.target.value);
                  if (browseError) setBrowseError(null);
                  if (errorMessage) setError(null);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    handleJump();
                  }
                }}
                disabled={switching}
                placeholder="输入绝对路径后回车跳转"
                spellCheck={false}
                autoComplete="off"
                className="flex-1 min-w-0 px-3 py-2 rounded-lg bg-bg-tertiary border border-border text-sm text-text-primary font-mono focus:outline-none focus:border-accent transition-colors disabled:opacity-50"
              />
              <button
                onClick={handleJump}
                disabled={switching || loadingBrowse}
                className="p-2 rounded-lg bg-bg-tertiary border border-border text-text-secondary hover:text-text-primary hover:border-accent transition-colors disabled:opacity-40"
                title="跳转到输入的路径 (Enter)"
              >
                <ArrowRight size={14} />
              </button>
            </div>
          </div>

          {/* 快捷位置：主目录 / 我的电脑 */}
          <div className="flex items-center gap-1.5 flex-wrap">
            <button
              onClick={() => browse?.home && handleQuick(browse.home)}
              disabled={switching || !browse?.home}
              className="flex items-center gap-1 px-2 py-1 rounded-md bg-bg-tertiary border border-border text-[11px] text-text-secondary hover:text-text-primary hover:border-accent transition-colors disabled:opacity-40"
            >
              <Home size={11} />
              主目录
            </button>
            <span className="text-text-tertiary text-[11px]">|</span>
            {(browse?.drives || []).map((d) => (
              <button
                key={d}
                onClick={() => handleQuick(d)}
                disabled={switching}
                className="flex items-center gap-1 px-2 py-1 rounded-md bg-bg-tertiary border border-border text-[11px] font-mono text-text-secondary hover:text-text-primary hover:border-accent transition-colors disabled:opacity-40"
              >
                <HardDrive size={11} />
                {d}
              </button>
            ))}
          </div>

          {/* 目录列表 */}
          <div className="border border-border rounded-lg bg-bg-tertiary/40">
            <div className="flex items-center justify-between px-3 py-1.5 border-b border-border bg-bg-tertiary rounded-t-lg">
              <div className="flex items-center gap-1.5 text-[11px] text-text-secondary min-w-0">
                <FolderOpen size={12} className="text-accent flex-shrink-0" />
                <span className="font-mono truncate">{browsePath || "加载中..."}</span>
              </div>
              <div className="flex items-center gap-1 flex-shrink-0">
                <button
                  onClick={handleGoUp}
                  disabled={switching || loadingBrowse || !browse?.parent}
                  className="p-1 rounded text-text-tertiary hover:text-text-secondary hover:bg-bg-secondary transition-colors disabled:opacity-30"
                  title="上级目录"
                >
                  <ChevronUp size={13} />
                </button>
                <button
                  onClick={() => load(browsePath)}
                  disabled={switching || loadingBrowse}
                  className="p-1 rounded text-text-tertiary hover:text-text-secondary hover:bg-bg-secondary transition-colors disabled:opacity-30"
                  title="刷新"
                >
                  <RefreshCw size={13} className={loadingBrowse ? "animate-spin" : ""} />
                </button>
              </div>
            </div>

            <div ref={listRef} className="max-h-56 overflow-y-auto p-1.5 space-y-0.5">
              {loadingBrowse && (
                <div className="flex items-center justify-center gap-2 py-8 text-text-tertiary text-xs">
                  <Loader2 size={14} className="animate-spin" />
                  加载中...
                </div>
              )}
              {!loadingBrowse && browseError && (
                <div className="flex items-center gap-2 py-6 px-2 text-xs text-red-400">
                  <AlertCircle size={13} className="flex-shrink-0" />
                  {browseError}
                </div>
              )}
              {!loadingBrowse && !browseError && browse && browse.dirs.length === 0 && (
                <div className="py-8 text-center text-text-tertiary text-xs">（此目录下没有子目录）</div>
              )}
              {!loadingBrowse &&
                browse &&
                browse.dirs.map((d) => (
                  <button
                    key={d.path}
                    onClick={() => handleEnterDir(d.path)}
                    disabled={switching}
                    className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left hover:bg-bg-secondary transition-colors disabled:opacity-50 group"
                    title={`进入 ${d.path}`}
                  >
                    <Folder size={13} className="text-amber-400 flex-shrink-0" />
                    <span className="text-xs text-text-primary truncate flex-1">{d.name}</span>
                    <ArrowRight size={11} className="opacity-0 group-hover:opacity-100 text-text-tertiary flex-shrink-0" />
                  </button>
                ))}
            </div>
          </div>

          {/* 选中状态提示 */}
          <div className="text-[11px] text-text-tertiary bg-bg-tertiary rounded-lg p-2.5 leading-relaxed">
            {selected
              ? "当前已在该目录。浏览到其他目录后点击「切换到此目录」。"
              : browsePath
                ? <>将切换到：<code className="font-mono text-text-secondary break-all">{browsePath}</code></>
                : "请选择一个目录。"}
            <br />
            切换会取消运行中的任务并重建引擎（数秒），会话按目录隔离。
          </div>

          {/* 后端切换错误提示 */}
          {errorMessage && !switching && (
            <div className="flex items-start gap-2 text-[11px] text-red-400 bg-red-400/10 border border-red-400/20 rounded-lg p-2.5">
              <AlertCircle size={13} className="mt-0.5 flex-shrink-0" />
              <span className="break-all">{errorMessage}</span>
            </div>
          )}
        </div>

        {/* 底部按钮 */}
        <div className="flex items-center justify-between gap-2 px-5 py-3 border-t border-border flex-shrink-0">
          <div className="text-[11px] text-text-tertiary font-mono truncate">
            {browsePath ? (
              <span className="flex items-center gap-1">
                <Folder size={11} />
                {browsePath}
              </span>
            ) : (
              "未选择目录"
            )}
          </div>
          <div className="flex items-center gap-2 flex-shrink-0">
            <button
              onClick={onClose}
              disabled={switching}
              className="px-3 py-1.5 rounded-lg text-xs text-text-secondary hover:text-text-primary hover:bg-bg-tertiary transition-colors disabled:opacity-30"
            >
              取消
            </button>
            <button
              onClick={handleConfirm}
              disabled={!canConfirm}
              className="flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-accent text-white text-xs hover:opacity-90 transition-opacity disabled:opacity-40 disabled:cursor-not-allowed"
              title={canConfirm ? `切换到 ${browsePath}` : selected ? "当前已在该目录" : "请先选择目录"}
            >
              {switching ? (
                <>
                  <Loader2 size={13} className="animate-spin" />
                  切换中...
                </>
              ) : (
                "切换到此目录"
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
