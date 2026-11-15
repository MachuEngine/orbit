import { useCallback, useEffect, useRef, useState } from "react";

/** `speechSynthesis`가 있는 브라우저인가(ADR-0025 §4: OS 내장 음성). */
export function isSpeechSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    "speechSynthesis" in window &&
    typeof SpeechSynthesisUtterance !== "undefined"
  );
}

/** 한국어 음성이 있으면 고른다. 없으면 undefined로 두어 브라우저 기본 음성(`lang`만 지정)을 쓴다. */
export function pickKoreanVoice(
  voices: readonly SpeechSynthesisVoice[],
): SpeechSynthesisVoice | undefined {
  const korean = voices.filter((voice) =>
    voice.lang.toLowerCase().replace("_", "-").startsWith("ko"),
  );
  return (
    korean.find((voice) => voice.lang.toLowerCase().replace("_", "-") === "ko-kr") ?? korean[0]
  );
}

/**
 * 답 읽어 주기. 같은 `key`로 다시 부르면 멈춘다. 다른 `key`를 읽는 중이면 그것을 멈추고 새로 읽는다.
 * 컴포넌트가 사라지면 읽기를 멈춘다.
 */
export function useSpeech() {
  const supported = isSpeechSupported();
  const [speakingKey, setSpeakingKey] = useState<string | null>(null);
  const ticket = useRef(0);

  const stop = useCallback(() => {
    ticket.current += 1; // 취소로 오는 이전 발화의 종료 이벤트를 무시한다
    if (isSpeechSupported()) window.speechSynthesis.cancel();
    setSpeakingKey(null);
  }, []);

  const speak = useCallback((key: string, text: string) => {
    if (!isSpeechSupported() || text === "") return;
    const synth = window.speechSynthesis;
    ticket.current += 1;
    const mine = ticket.current;
    synth.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = "ko-KR";
    const voice = pickKoreanVoice(synth.getVoices());
    if (voice !== undefined) utterance.voice = voice;
    const done = () => {
      if (ticket.current === mine) setSpeakingKey(null);
    };
    utterance.onend = done;
    utterance.onerror = done;
    setSpeakingKey(key);
    synth.speak(utterance);
  }, []);

  const toggle = useCallback(
    (key: string, text: string) => {
      if (speakingKey === key) stop();
      else speak(key, text);
    },
    [speakingKey, speak, stop],
  );

  useEffect(
    () => () => {
      ticket.current += 1;
      if (isSpeechSupported()) window.speechSynthesis.cancel();
    },
    [],
  );

  return { supported, speakingKey, speak, stop, toggle };
}

export type Speech = ReturnType<typeof useSpeech>;
