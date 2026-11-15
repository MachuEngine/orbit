/**
 * 앱 창(서버 출처)에 `window.orbitDesktop`만 노출한다. `ipcRenderer`·Node API는 노출하지 않는다.
 * sandbox preload는 로컬 파일을 require할 수 없어 esbuild가 한 파일로 묶는다.
 */
import { contextBridge, ipcRenderer } from "electron";
import { CHANNELS, VERSION_ARG } from "../shared/channels";

function readVersion(): string {
  const arg = process.argv.find((item) => item.startsWith(VERSION_ARG));
  const value = arg?.slice(VERSION_ARG.length) ?? "";
  return /^[0-9A-Za-z.+-]{1,32}$/.test(value) ? value : "unknown";
}

const orbitDesktop = Object.freeze({
  notify(payload: { id: string; title: string; body?: string }): void {
    // 값을 새 객체로 옮겨 담아 보낸다. 검증은 main이 한다.
    ipcRenderer.send(CHANNELS.notify, {
      id: payload.id,
      title: payload.title,
      body: payload.body ?? "",
    });
  },
  setBadge(count: number): void {
    ipcRenderer.send(CHANNELS.setBadge, count);
  },
  onToggleVoice(callback: () => void): () => void {
    if (typeof callback !== "function") return () => undefined;
    const listener = () => {
      callback();
    };
    ipcRenderer.on(CHANNELS.toggleVoice, listener);
    return () => {
      ipcRenderer.removeListener(CHANNELS.toggleVoice, listener);
    };
  },
  info: Object.freeze({ platform: process.platform, version: readVersion() }),
});

contextBridge.exposeInMainWorld("orbitDesktop", orbitDesktop);
