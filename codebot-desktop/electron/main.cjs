// Electron 主进程入口
// 职责：1) spawn Python FastAPI sidecar  2) 创建窗口  3) 退出时清理 sidecar
//       4) 提供 IPC：原生目录选择器（供前端"切换工作目录"使用）
const { app, BrowserWindow, ipcMain, dialog } = require("electron");
const path = require("node:path");
const { spawn } = require("node:child_process");

// 无 GPU / 虚拟机 / 远程环境下 GPU 进程会反复崩溃导致 electron 无法启动，
// 禁用硬件加速 + no-sandbox 绕过（必须在 app ready 之前调用）。
// 这些开关只影响渲染层的图形加速，不影响 CodeBot 的核心功能。
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("no-sandbox");

// 项目根（codebot-desktop 的上一级）。打包后用 process.resourcesPath
const isPackaged = app.isPackaged;
const PROJECT_ROOT = isPackaged
  ? process.resourcesPath
  : path.resolve(__dirname, "..", "..");

// Python venv 解释器（开发模式用项目 .venv，打包模式用 extraResources 里的 .venv）
const PYTHON_EXE = isPackaged
  ? path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")
  : path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe");

// codebot 包路径（打包后在 resources/codebot）
const CODEBOT_DIR = isPackaged
  ? path.join(PROJECT_ROOT, "codebot")
  : path.join(PROJECT_ROOT, "codebot");

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

async function waitForServer(timeoutMs = 40000) {
  // sidecar 首次启动需要加载 config / 连 MCP / 初始化 runtime，可能较慢，
  // 给 40s 余量避免误报超时（即便超时 sidecar 仍可能在后台继续启动，
  // 前端 WS 有重连机制能后续接上）。
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
      preload: path.join(__dirname, "preload.cjs"),
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
  // 原生目录选择器：渲染进程通过 ipcRenderer.invoke('dialog:openDirectory') 调用
  // 返回选中目录的绝对路径字符串；用户取消返回空串。切工作目录时复用此能力。
  ipcMain.handle("dialog:openDirectory", async (_event, opts) => {
    const win = BrowserWindow.getFocusedWindow() || mainWindow;
    const result = await dialog.showOpenDialog(win, {
      title: (opts && opts.title) || "选择工作目录",
      message: (opts && opts.message) || "选择一个新的工作目录，引擎将在此目录下重建",
      properties: ["openDirectory", "createDirectory"],
    });
    if (result.canceled || result.filePaths.length === 0) return "";
    return result.filePaths[0];
  });

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
