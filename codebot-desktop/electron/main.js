// Electron 主进程入口
// 职责：1) spawn Python FastAPI sidecar  2) 创建窗口  3) 退出时清理 sidecar
const { app, BrowserWindow } = require("electron");
const path = require("node:path");
const { spawn } = require("node:child_process");

// 项目根（codebot-desktop 的上一级）
const PROJECT_ROOT = path.resolve(__dirname, "..", "..");
// Python venv 解释器
const PYTHON_EXE = path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe");
// FastAPI server 端口
const SERVER_PORT = 7800;
const SERVER_URL = `http://127.0.0.1:${SERVER_PORT}`;

let sidecar = null;
let mainWindow = null;

function spawnSidecar() {
  console.log(`[codebot-desktop] spawning sidecar: ${PYTHON_EXE} -m codebot.server --port ${SERVER_PORT}`);
  sidecar = spawn(PYTHON_EXE, ["-m", "codebot.server", "--port", String(SERVER_PORT)], {
    cwd: PROJECT_ROOT,
    env: { ...process.env },
    windowsHide: true,
  });
  sidecar.stdout.on("data", (d) => console.log(`[sidecar] ${d.toString().trim()}`));
  sidecar.stderr.on("data", (d) => console.error(`[sidecar] ${d.toString().trim()}`));
  sidecar.on("exit", (code) => console.log(`[sidecar] exited with code ${code}`));
}

async function waitForServer(timeoutMs = 15000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(`${SERVER_URL}/api/health`);
      if (res.ok) return;
    } catch {
      // server 还没起来，继续等
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error(`sidecar 在 ${timeoutMs}ms 内未就绪`);
}

async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    backgroundColor: "#1a1b26",
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  // 开发模式加载 Vite dev server，生产模式加载打包后的 index.html
  const isDev = !app.isPackaged;
  if (isDev) {
    await mainWindow.loadURL("http://localhost:5173");
    mainWindow.webContents.openDevTools();
  } else {
    await mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }
}

app.whenReady().then(async () => {
  spawnSidecar();
  try {
    await waitForServer();
    console.log("[codebot-desktop] sidecar ready");
  } catch (e) {
    console.error("[codebot-desktop] sidecar 启动失败:", e.message);
  }
  await createWindow();
});

app.on("window-all-closed", () => {
  if (sidecar) {
    sidecar.kill();
    sidecar = null;
  }
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  if (sidecar) {
    sidecar.kill();
    sidecar = null;
  }
});
