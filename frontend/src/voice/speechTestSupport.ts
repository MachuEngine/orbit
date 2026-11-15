import { vi } from "vitest";

export class FakeUtterance {
  static instances: FakeUtterance[] = [];
  text: string;
  lang = "";
  voice: SpeechSynthesisVoice | null = null;
  onend: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(text: string) {
    this.text = text;
    FakeUtterance.instances.push(this);
  }
}

export function voice(lang: string, name = lang): SpeechSynthesisVoice {
  return { lang, name } as SpeechSynthesisVoice;
}

/** `speechSynthesis`를 가짜로 바꾼다. 실제 소리는 나지 않는다. */
export function installFakeSpeech(voices: SpeechSynthesisVoice[] = []) {
  FakeUtterance.instances = [];
  const synth = {
    speak: vi.fn(),
    cancel: vi.fn(),
    getVoices: vi.fn(() => voices),
  };
  vi.stubGlobal("speechSynthesis", synth);
  vi.stubGlobal("SpeechSynthesisUtterance", FakeUtterance);
  return synth;
}
