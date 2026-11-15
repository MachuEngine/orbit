import { useCallback, useState } from "react";

const KEY = "orbit.voice.autoRead";

/** 저장소를 못 쓰는 환경(사생활 보호 모드, 차단)에서는 기본값(꺼짐)으로 둔다. */
export function loadAutoRead(): boolean {
  try {
    return window.localStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

export function saveAutoRead(value: boolean): void {
  try {
    window.localStorage.setItem(KEY, value ? "1" : "0");
  } catch {
    // 저장하지 못해도 이번 화면에서는 선택이 유지된다.
  }
}

/** "음성으로 질문하면 답을 자동으로 읽기" 설정. 기본은 꺼짐이다. */
export function useAutoRead(): [boolean, (value: boolean) => void] {
  const [value, setValue] = useState(loadAutoRead);
  const update = useCallback((next: boolean) => {
    setValue(next);
    saveAutoRead(next);
  }, []);
  return [value, update];
}
