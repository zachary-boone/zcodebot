// 主题状态：dark / light，跟随系统或用户手动切换，localStorage 持久化
import { create } from "zustand";

type Theme = "dark" | "light";

interface ThemeStore {
  theme: Theme;
  toggle: () => void;
  setTheme: (t: Theme) => void;
  init: () => void;
}

function applyTheme(theme: Theme) {
  const html = document.documentElement;
  html.classList.remove("dark", "light");
  html.classList.add(theme);
  localStorage.setItem("codebot-theme", theme);
}

function detectInitial(): Theme {
  const saved = localStorage.getItem("codebot-theme") as Theme | null;
  if (saved === "dark" || saved === "light") return saved;
  // 跟随系统
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export const useThemeStore = create<ThemeStore>((set, get) => ({
  theme: "dark",
  init: () => {
    const t = detectInitial();
    applyTheme(t);
    set({ theme: t });
  },
  toggle: () => {
    const next = get().theme === "dark" ? "light" : "dark";
    applyTheme(next);
    set({ theme: next });
  },
  setTheme: (t) => {
    applyTheme(t);
    set({ theme: t });
  },
}));
