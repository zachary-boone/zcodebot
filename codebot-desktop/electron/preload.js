// preload：通过 contextBridge 暴露安全 API 给渲染进程
const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("codebot", {
  // 后续可加：打开文件选择对话框、菜单等原生能力
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },
});
