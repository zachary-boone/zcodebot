// Preload exposes safe APIs to the renderer via contextBridge.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("codebot", {
  // 后续可加：菜单等原生能力
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },
  // 原生目录选择器：返回选中目录绝对路径，用户取消返回空串。
  // 用于"切换工作目录"功能——浏览器环境没有目录选择 API，必须走主进程 dialog。
  // 注意：仅在 Electron 打包/运行时可用；纯浏览器（vite dev 无 electron）下为 undefined，
  // 前端调用前需做存在性判断并降级为手动输入。
  selectDirectory: async (opts) => {
    return ipcRenderer.invoke("dialog:openDirectory", opts);
  },
  hasNativeDialog: true,
});
