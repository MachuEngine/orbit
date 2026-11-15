import { useCallback, useEffect, useRef, useState } from "react";
import { transcribeAudio } from "../api/endpoints";
import { isApiError } from "../api/errors";
import { isDesktop } from "../desktop/bridge";

/** 서버 상한(ADR-0025 §2)과 같다. 넘으면 자동으로 멈추고 전사한다. */
export const MAX_RECORD_SECONDS = 60;
/** 서버의 압축 형식 크기 상한(`MAX_COMPRESSED_AUDIO_BYTES`)과 짝이다. 60초 ≈ 240KB. */
export const RECORD_BITS_PER_SECOND = 32_000;

export type VoicePhase = "idle" | "recording" | "transcribing";

/** 브라우저가 지원하는 형식을 이 순서로 고른다(webm/opus 우선). 없으면 브라우저 기본 형식이다. */
const MIME_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/mp4",
];

export function isVoiceInputSupported(): boolean {
  return (
    typeof navigator !== "undefined" &&
    typeof (navigator as Partial<Navigator>).mediaDevices?.getUserMedia === "function" &&
    typeof MediaRecorder !== "undefined"
  );
}

function pickMimeType(): string | undefined {
  if (typeof MediaRecorder.isTypeSupported !== "function") return undefined;
  return MIME_CANDIDATES.find((type) => MediaRecorder.isTypeSupported(type));
}

export const VOICE_TEXT = {
  permission: "마이크 권한이 없습니다. 브라우저 설정에서 이 사이트의 마이크를 허용해 주세요.",
  permissionDesktop:
    "마이크 권한이 없습니다. 시스템 설정의 개인정보 보호 및 보안 > 마이크에서 이 앱을 허용한 뒤 다시 시도해 주세요.",
  noDevice: "사용할 수 있는 마이크를 찾지 못했습니다.",
  startFailed: "마이크를 시작하지 못했습니다. 다른 앱이 마이크를 쓰고 있는지 확인해 주세요.",
  recordFailed: "녹음 중 문제가 생겼습니다. 다시 시도해 주세요.",
  empty: "인식된 말이 없습니다. 다시 말씀해 주세요.",
  tooLong: "녹음이 너무 깁니다. 60초 이내로 다시 녹음해 주세요.",
  format: "이 브라우저의 녹음 형식을 서버가 지원하지 않습니다.",
  limit: "음성 입력 사용 한도에 도달했습니다. 잠시 뒤 다시 시도하거나 직접 입력해 주세요.",
  provider: "음성을 글자로 바꾸지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
  unavailable: "음성 입력이 이 서버에 설정되어 있지 않습니다.",
  network: "서버에 연결하지 못했습니다. 연결을 확인한 뒤 다시 시도해 주세요.",
  signedOut: "로그인이 끝났습니다. 다시 로그인해 주세요.",
} as const;

function startErrorText(error: unknown): string {
  const name = error instanceof DOMException ? error.name : "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return isDesktop() ? VOICE_TEXT.permissionDesktop : VOICE_TEXT.permission;
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") return VOICE_TEXT.noDevice;
  return VOICE_TEXT.startFailed;
}

function transcribeErrorText(error: unknown): string {
  if (!isApiError(error)) return VOICE_TEXT.provider;
  if (error.kind === "network") return VOICE_TEXT.network;
  if (error.kind === "unauthorized") return VOICE_TEXT.signedOut;
  switch (error.detail) {
    case "too_large":
    case "too_long":
      return VOICE_TEXT.tooLong;
    case "unsupported_format":
      return VOICE_TEXT.format;
    case "usage_limit_exceeded":
      return VOICE_TEXT.limit;
    case "stt_unavailable":
      return VOICE_TEXT.unavailable;
    default:
      return VOICE_TEXT.provider;
  }
}

export interface UseVoiceInputOptions {
  /** 전사가 끝나면 문장을 넘긴다. 자동으로 전송하지 않는다. */
  onTranscript: (text: string) => void;
  /** 마이크를 열기 직전에 부른다(읽어 주던 음성을 멈추는 데 쓴다). */
  onStart?: () => void;
  /** 오디오를 글자로 바꾸는 함수. 테스트에서 바꿔 끼운다. */
  transcribe?: (audio: Blob) => Promise<string>;
  /**
   * Esc로 녹음을 취소해도 되는 상황인가. 다른 대화 상자(승인 카드·알림 패널 등)가 Esc를 쓰는 중이면
   * false를 돌려 가로채지 않게 한다. 없으면 `defaultPrevented`가 아닌 Esc는 모두 처리한다.
   */
  shouldHandleEscape?: () => boolean;
}

/**
 * 마이크 토글 녹음 로직(ADR-0025 §2). 화면 스타일은 갖지 않는다.
 * - `toggle`/`start`/`stop`/`cancel`을 노출한다. 데스크톱 전역 단축키(13b)가 같은 함수로 켜고 끈다.
 * - 한 번 시작하면 60초에 자동으로 멈추고 전사한다. `cancel`과 Esc는 전사하지 않고 버린다.
 * - 컴포넌트가 사라지면 마이크 트랙을 멈추고 결과를 버린다. 오디오는 메모리에만 두고 저장하지 않는다.
 */
export function useVoiceInput({
  onTranscript,
  onStart,
  transcribe,
  shouldHandleEscape,
}: UseVoiceInputOptions) {
  const [phase, setPhase] = useState<VoicePhase>("idle");
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const supported = isVoiceInputSupported();

  const phaseRef = useRef<VoicePhase | "starting">("idle");
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const discardRef = useRef(false);
  const mountedRef = useRef(true);
  // 비동기 구간마다 다시 읽는다(타입 좁히기가 `await` 뒤의 값 변화를 놓치지 않게 함수로 읽는다).
  const isMounted = useCallback(() => mountedRef.current, []);
  const timersRef = useRef<{ tick?: number; limit?: number }>({});
  const latest = useRef({ onTranscript, onStart, transcribe, shouldHandleEscape });
  useEffect(() => {
    latest.current = { onTranscript, onStart, transcribe, shouldHandleEscape };
  });

  const setPhaseBoth = useCallback((next: VoicePhase) => {
    phaseRef.current = next;
    if (mountedRef.current) setPhase(next);
  }, []);

  const clearTimers = useCallback(() => {
    window.clearInterval(timersRef.current.tick);
    window.clearTimeout(timersRef.current.limit);
    timersRef.current = {};
  }, []);

  const releaseStream = useCallback(() => {
    for (const track of streamRef.current?.getTracks() ?? []) track.stop();
    streamRef.current = null;
  }, []);

  const finish = useCallback(
    async (blob: Blob) => {
      if (discardRef.current || !isMounted()) {
        setPhaseBoth("idle"); // 취소(Esc)나 화면 이탈: 전사하지 않고 버린다
        return;
      }
      if (blob.size === 0) {
        setError(VOICE_TEXT.empty);
        setPhaseBoth("idle");
        return;
      }
      setPhaseBoth("transcribing");
      try {
        const text = (await (latest.current.transcribe ?? transcribeAudio)(blob)).trim();
        if (!isMounted()) return;
        if (text === "") setError(VOICE_TEXT.empty);
        else latest.current.onTranscript(text);
      } catch (cause) {
        if (isMounted()) setError(transcribeErrorText(cause));
      } finally {
        setPhaseBoth("idle");
      }
    },
    [isMounted, setPhaseBoth],
  );

  const stop = useCallback(() => {
    const recorder = recorderRef.current;
    if (phaseRef.current !== "recording" || recorder?.state !== "recording") return;
    recorder.stop();
  }, []);

  const cancel = useCallback(() => {
    const recorder = recorderRef.current;
    if (phaseRef.current !== "recording" || recorder?.state !== "recording") return;
    discardRef.current = true;
    recorder.stop();
  }, []);

  const start = useCallback(async () => {
    if (!isVoiceInputSupported() || phaseRef.current !== "idle") return;
    phaseRef.current = "starting";
    setError(null);
    latest.current.onStart?.();
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (cause) {
      phaseRef.current = "idle";
      if (isMounted()) setError(startErrorText(cause));
      return;
    }
    if (!isMounted()) {
      for (const track of stream.getTracks()) track.stop(); // 기다리는 사이 화면이 사라졌다
      phaseRef.current = "idle";
      return;
    }
    streamRef.current = stream;
    const chunks: Blob[] = [];
    let recorder: MediaRecorder;
    try {
      const mimeType = pickMimeType();
      recorder = new MediaRecorder(stream, {
        ...(mimeType === undefined ? {} : { mimeType }),
        // 서버는 길이를 알 수 없는 형식을 크기 상한(512KB)으로 막는다. 60초가 이 안에 들어오게 낮춘다.
        audioBitsPerSecond: RECORD_BITS_PER_SECOND,
      });
    } catch {
      releaseStream();
      phaseRef.current = "idle";
      setError(VOICE_TEXT.startFailed);
      return;
    }
    recorderRef.current = recorder;
    discardRef.current = false;
    recorder.ondataavailable = (event: BlobEvent) => {
      if (event.data.size > 0) chunks.push(event.data);
    };
    recorder.onerror = () => {
      discardRef.current = true;
      if (isMounted()) setError(VOICE_TEXT.recordFailed);
      if (recorder.state === "recording") recorder.stop();
    };
    recorder.onstop = () => {
      clearTimers();
      releaseStream();
      recorderRef.current = null;
      const type = recorder.mimeType !== "" ? recorder.mimeType : (chunks[0]?.type ?? "audio/webm");
      void finish(new Blob(chunks, { type }));
    };

    const startedAt = Date.now();
    setElapsed(0);
    recorder.start();
    setPhaseBoth("recording");
    timersRef.current.tick = window.setInterval(() => {
      setElapsed(Math.min(MAX_RECORD_SECONDS, Math.floor((Date.now() - startedAt) / 1000)));
    }, 250);
    timersRef.current.limit = window.setTimeout(() => {
      stop();
    }, MAX_RECORD_SECONDS * 1000);
  }, [clearTimers, finish, isMounted, releaseStream, setPhaseBoth, stop]);

  const toggle = useCallback(() => {
    if (phaseRef.current === "recording") stop();
    else if (phaseRef.current === "idle") void start();
  }, [start, stop]);

  // Esc는 녹음 중일 때만 가로채어 전사 없이 취소한다.
  useEffect(() => {
    if (phase !== "recording") return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return; // 다른 곳이 이미 처리했다
      if (latest.current.shouldHandleEscape?.() === false) return;
      event.preventDefault();
      cancel();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [phase, cancel]);

  // 화면이 사라지면 마이크를 반드시 놓고 진행 중인 결과는 버린다.
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      discardRef.current = true;
      clearTimers();
      const recorder = recorderRef.current;
      if (recorder !== null) {
        recorder.ondataavailable = null;
        recorder.onstop = null;
        recorder.onerror = null;
        if (recorder.state !== "inactive") recorder.stop();
        recorderRef.current = null;
      }
      releaseStream();
    };
  }, [clearTimers, releaseStream]);

  return { supported, phase, elapsed, error, start, stop, cancel, toggle };
}

export type VoiceInput = ReturnType<typeof useVoiceInput>;
