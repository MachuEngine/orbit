import { vi } from "vitest";

/** 테스트용 MediaRecorder. `stop()`하면 모인 조각과 함께 `onstop`을 부른다. 실제 마이크는 쓰지 않는다. */
export class FakeRecorder {
  static instances: FakeRecorder[] = [];
  static isTypeSupported = vi.fn((type: string) => type === "audio/webm;codecs=opus");
  state: "inactive" | "recording" = "inactive";
  mimeType: string;
  ondataavailable: ((event: BlobEvent) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readonly options: MediaRecorderOptions | undefined;

  constructor(_stream: MediaStream, options?: MediaRecorderOptions) {
    this.options = options;
    this.mimeType = options?.mimeType ?? "audio/webm";
    FakeRecorder.instances.push(this);
  }

  start() {
    this.state = "recording";
  }

  stop() {
    if (this.state === "inactive") return;
    this.state = "inactive";
    this.ondataavailable?.({
      data: new Blob(["synthetic-audio"], { type: this.mimeType }),
    } as BlobEvent);
    this.onstop?.();
  }
}

export interface FakeMic {
  getUserMedia: ReturnType<typeof vi.fn>;
  track: { stop: ReturnType<typeof vi.fn> };
}

/** `navigator.mediaDevices`와 `MediaRecorder`를 가짜로 바꾼다. 반환한 함수로 되돌린다. */
export function installFakeMic(
  options: { error?: DOMException } = {},
): FakeMic & { restore: () => void } {
  const track = { stop: vi.fn() };
  const stream = { getTracks: () => [track] } as unknown as MediaStream;
  const getUserMedia = vi.fn(() =>
    options.error === undefined ? Promise.resolve(stream) : Promise.reject(options.error),
  );
  FakeRecorder.instances = [];
  FakeRecorder.isTypeSupported.mockClear();
  Object.defineProperty(navigator, "mediaDevices", {
    value: { getUserMedia },
    configurable: true,
  });
  vi.stubGlobal("MediaRecorder", FakeRecorder);
  return {
    getUserMedia,
    track,
    restore: () => {
      Reflect.deleteProperty(navigator, "mediaDevices");
      vi.unstubAllGlobals();
    },
  };
}
