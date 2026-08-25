import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WidgetConfig } from "./config";
import type { SpeakResult } from "./voice";

const isVoiceTtsEnabledMock = vi.fn<() => boolean>(() => false);
const synthesizeSpeechMock =
  vi.fn<(config: WidgetConfig, text: string, speed?: number) => Promise<SpeakResult>>();

vi.mock("./session", () => ({
  isVoiceTtsEnabled: () => isVoiceTtsEnabledMock(),
}));

vi.mock("./voice", () => ({
  synthesizeSpeech: (config: WidgetConfig, text: string, speed?: number) =>
    synthesizeSpeechMock(config, text, speed),
}));

// Imported AFTER the mocks above so tts.ts picks them up.
import { cancel, speak, speakGreeting } from "./tts";

const baseConfig: WidgetConfig = {
  clientKey: "pk_test_123",
  apiBase: "http://localhost:8000",
  mountSelector: null,
  debug: false,
  position: "right",
};

describe("tts", () => {
  describe("speak", () => {
    it("speaks arbitrary text (a bot reply), not just the baked-in greeting", async () => {
      const speakFn = vi.fn();
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak: speakFn, cancel: vi.fn() },
        configurable: true,
        writable: true,
      });
      class FakeUtterance {
        constructor(public text: string) {}
      }
      Object.defineProperty(window, "SpeechSynthesisUtterance", {
        value: FakeUtterance,
        configurable: true,
        writable: true,
      });

      await speak(baseConfig, "We're open Monday through Friday, 9 to 6.");

      const uttered = speakFn.mock.calls[0]?.[0] as FakeUtterance;
      expect(uttered.text).toBe("We're open Monday through Friday, 9 to 6.");
    });
  });

  const originalSpeechSynthesis = window.speechSynthesis;
  const originalUtterance = window.SpeechSynthesisUtterance;
  const originalAudio = window.Audio;
  const originalCreateObjectURL = URL.createObjectURL.bind(URL);
  const originalRevokeObjectURL = URL.revokeObjectURL.bind(URL);

  beforeEach(() => {
    isVoiceTtsEnabledMock.mockReset();
    isVoiceTtsEnabledMock.mockReturnValue(false);
    synthesizeSpeechMock.mockReset();
  });

  afterEach(() => {
    Object.defineProperty(window, "speechSynthesis", {
      value: originalSpeechSynthesis,
      configurable: true,
      writable: true,
    });
    Object.defineProperty(window, "SpeechSynthesisUtterance", {
      value: originalUtterance,
      configurable: true,
      writable: true,
    });
    Object.defineProperty(window, "Audio", {
      value: originalAudio,
      configurable: true,
      writable: true,
    });
    URL.createObjectURL = originalCreateObjectURL;
    URL.revokeObjectURL = originalRevokeObjectURL;
    vi.restoreAllMocks();
  });

  describe("speakGreeting", () => {
    // The greeting is pre-baked audio played directly via `new Audio(...)`
    // -- no speechSynthesis, no synthesizeSpeech/network call at all (see
    // greetingAudio.ts). These tests mock window.Audio to verify that
    // mechanism and its onBlocked semantics; the "cloud TTS" describe block
    // below covers the SEPARATE mechanism ordinary spoken replies use.
    class FakeGreetingAudio {
      static instances: FakeGreetingAudio[] = [];
      src: string;
      onended: (() => void) | null = null;
      play: () => Promise<void>;
      pause = vi.fn();
      constructor(src: string) {
        this.src = src;
        this.play = vi.fn(() => Promise.resolve());
        FakeGreetingAudio.instances.push(this);
      }
    }

    beforeEach(() => {
      FakeGreetingAudio.instances = [];
      Object.defineProperty(window, "Audio", {
        value: FakeGreetingAudio,
        configurable: true,
        writable: true,
      });
    });

    it("plays the pre-baked greeting audio directly, with no network/synthesis call", async () => {
      await speakGreeting(baseConfig);

      expect(FakeGreetingAudio.instances).toHaveLength(1);
      expect(FakeGreetingAudio.instances[0]?.src).toContain("data:audio/mpeg;base64,");
      expect(FakeGreetingAudio.instances[0]?.play).toHaveBeenCalledTimes(1);
      expect(synthesizeSpeechMock).not.toHaveBeenCalled();
    });

    it("does not call onBlocked when playback succeeds", async () => {
      const onBlocked = vi.fn();

      await speakGreeting(baseConfig, onBlocked);

      expect(onBlocked).not.toHaveBeenCalled();
    });

    it("calls onBlocked when play() is rejected (autoplay policy)", async () => {
      Object.defineProperty(window, "Audio", {
        value: class {
          play = vi.fn(() => Promise.reject(new Error("blocked by autoplay policy")));
          pause = vi.fn();
          onended: (() => void) | null = null;
        },
        configurable: true,
        writable: true,
      });
      const onBlocked = vi.fn();

      await speakGreeting(baseConfig, onBlocked);

      expect(onBlocked).toHaveBeenCalledTimes(1);
    });

    it("swallows a throwing Audio constructor and never lets it escape (chat must be unaffected)", async () => {
      Object.defineProperty(window, "Audio", {
        value: class {
          constructor() {
            throw new Error("construction failed");
          }
        },
        configurable: true,
        writable: true,
      });
      const onBlocked = vi.fn();

      await expect(speakGreeting(baseConfig, onBlocked)).resolves.not.toThrow();
      expect(onBlocked).toHaveBeenCalledTimes(1);
    });

    it("cancel() pauses the greeting's audio -- it shares the same currentAudio tracking as spoken replies", async () => {
      await speakGreeting(baseConfig);
      const instance = FakeGreetingAudio.instances[0];
      expect(instance).toBeDefined();

      cancel();

      expect(instance?.pause).toHaveBeenCalledTimes(1);
    });
  });


  describe("cloud TTS (OpenAI, via the backend)", () => {
    class FakeAudio {
      static instances: FakeAudio[] = [];
      onended: (() => void) | null = null;
      play: () => Promise<void>;
      pause = vi.fn();
      src: string;
      constructor(url: string) {
        this.src = url;
        this.play = vi.fn(() => Promise.resolve());
        FakeAudio.instances.push(this);
      }
    }

    beforeEach(() => {
      FakeAudio.instances = [];
      Object.defineProperty(window, "Audio", {
        value: FakeAudio,
        configurable: true,
        writable: true,
      });
      URL.createObjectURL = vi.fn(() => "blob:fake-url");
      URL.revokeObjectURL = vi.fn();
    });

    it("does not call synthesizeSpeech at all when cloud TTS is disabled -- goes straight to the browser-native path", async () => {
      isVoiceTtsEnabledMock.mockReturnValue(false);
      const browserSpeak = vi.fn();
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak: browserSpeak, cancel: vi.fn() },
        configurable: true,
        writable: true,
      });
      Object.defineProperty(window, "SpeechSynthesisUtterance", {
        value: class {
          constructor(public text: string) {}
        },
        configurable: true,
        writable: true,
      });

      await speak(baseConfig, "Hello there");

      expect(synthesizeSpeechMock).not.toHaveBeenCalled();
      expect(browserSpeak).toHaveBeenCalledTimes(1);
    });

    it("plays cloud audio and never touches speechSynthesis when synthesizeSpeech succeeds", async () => {
      isVoiceTtsEnabledMock.mockReturnValue(true);
      synthesizeSpeechMock.mockResolvedValue({ ok: true, audio: new Blob(["fake-mp3"]) });
      const browserSpeak = vi.fn();
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak: browserSpeak, cancel: vi.fn() },
        configurable: true,
        writable: true,
      });

      await speak(baseConfig, "Hello there");

      expect(synthesizeSpeechMock).toHaveBeenCalledWith(baseConfig, "Hello there", undefined);
      expect(FakeAudio.instances).toHaveLength(1);
      expect(FakeAudio.instances[0]?.play).toHaveBeenCalledTimes(1);
      expect(browserSpeak).not.toHaveBeenCalled();
    });

    it("falls back to the browser-native path when synthesizeSpeech itself fails (not configured / network / upstream)", async () => {
      isVoiceTtsEnabledMock.mockReturnValue(true);
      synthesizeSpeechMock.mockResolvedValue({
        ok: false,
        error: { type: "VOICE_CALL_ERROR", errorCode: "VOICE_PROVIDER_NOT_CONFIGURED", message: "not configured" },
      });
      const browserSpeak = vi.fn();
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak: browserSpeak, cancel: vi.fn() },
        configurable: true,
        writable: true,
      });
      Object.defineProperty(window, "SpeechSynthesisUtterance", {
        value: class {
          constructor(public text: string) {}
        },
        configurable: true,
        writable: true,
      });

      await speak(baseConfig, "Hello there");

      expect(FakeAudio.instances).toHaveLength(0);
      expect(browserSpeak).toHaveBeenCalledTimes(1);
    });

    it("calls onBlocked (not a browser-native fallback) when cloud audio.play() rejects -- same autoplay-policy contract as speechSynthesis", async () => {
      isVoiceTtsEnabledMock.mockReturnValue(true);
      synthesizeSpeechMock.mockResolvedValue({ ok: true, audio: new Blob(["fake-mp3"]) });
      class RejectingAudio extends FakeAudio {
        constructor(url: string) {
          super(url);
          this.play = vi.fn(() => Promise.reject(new Error("no user activation")));
        }
      }
      Object.defineProperty(window, "Audio", {
        value: RejectingAudio,
        configurable: true,
        writable: true,
      });
      const browserSpeak = vi.fn();
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak: browserSpeak, cancel: vi.fn() },
        configurable: true,
        writable: true,
      });
      const onBlocked = vi.fn();

      await speak(baseConfig, "Hello there", onBlocked);

      expect(onBlocked).toHaveBeenCalledTimes(1);
      expect(browserSpeak).not.toHaveBeenCalled();
    });
  });

  describe("cancel", () => {
    it("calls speechSynthesis.cancel when available", () => {
      const cancelFn = vi.fn();
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak: vi.fn(), cancel: cancelFn },
        configurable: true,
        writable: true,
      });

      cancel();

      expect(cancelFn).toHaveBeenCalledTimes(1);
    });

    it("no-ops without throwing when window.speechSynthesis is absent", () => {
      Object.defineProperty(window, "speechSynthesis", {
        value: undefined,
        configurable: true,
        writable: true,
      });

      expect(() => cancel()).not.toThrow();
    });

    it("swallows a throwing cancel() and never lets it escape", () => {
      const cancelFn = vi.fn(() => {
        throw new Error("blocked");
      });
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak: vi.fn(), cancel: cancelFn },
        configurable: true,
        writable: true,
      });

      expect(() => cancel()).not.toThrow();
    });

    it("pauses any in-progress cloud audio and revokes its object URL", async () => {
      isVoiceTtsEnabledMock.mockReturnValue(true);
      synthesizeSpeechMock.mockResolvedValue({ ok: true, audio: new Blob(["fake-mp3"]) });
      class FakeAudioForCancel {
        onended: (() => void) | null = null;
        pause = vi.fn();
        src = "";
        play = vi.fn(() => Promise.resolve());
      }
      Object.defineProperty(window, "Audio", {
        value: FakeAudioForCancel,
        configurable: true,
        writable: true,
      });
      URL.createObjectURL = vi.fn(() => "blob:fake-url");
      const revoke = vi.fn();
      URL.revokeObjectURL = revoke;
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak: vi.fn(), cancel: vi.fn() },
        configurable: true,
        writable: true,
      });

      await speak(baseConfig, "Hello there");
      cancel();

      expect(revoke).toHaveBeenCalledWith("blob:fake-url");
    });
  });
});
