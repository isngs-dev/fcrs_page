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
import { TTS_GREETING_TEXT, cancel, speak, speakGreeting } from "./tts";

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
    it("calls speechSynthesis.speak exactly once with the baked-in greeting text (simulating an open-gesture call site)", async () => {
      const speak = vi.fn();
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak, cancel: vi.fn() },
        configurable: true,
        writable: true,
      });
      class FakeUtterance {
        text: string;
        constructor(text: string) {
          this.text = text;
        }
      }
      Object.defineProperty(window, "SpeechSynthesisUtterance", {
        value: FakeUtterance,
        configurable: true,
        writable: true,
      });

      // Caller-side gating: only invoked from the simulated open gesture,
      // never on its own — this test just proves speakGreeting's own
      // behavior once called.
      await speakGreeting(baseConfig);

      expect(speak).toHaveBeenCalledTimes(1);
      const uttered = speak.mock.calls[0]?.[0] as FakeUtterance & { rate?: number };
      expect(uttered.text).toBe(TTS_GREETING_TEXT);
      // Slightly slower than the reply-speaking default (0.95) -- warmer,
      // easier to catch on first listen.
      expect(uttered.rate).toBe(0.85);
    });

    it("does not call speak when the caller does not invoke speakGreeting (muted path is the caller's responsibility)", () => {
      const speak = vi.fn();
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak, cancel: vi.fn() },
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

      // Simulating a muted visitor: the widget's mute check short-circuits
      // before ever calling speakGreeting — tts.ts has no muted concept of
      // its own, so the contract under test is "not calling it means no
      // speech", proven trivially but explicitly for the record.
      expect(speak).not.toHaveBeenCalled();
    });

    it("no-ops without throwing when window.speechSynthesis is absent", async () => {
      Object.defineProperty(window, "speechSynthesis", {
        value: undefined,
        configurable: true,
        writable: true,
      });

      await expect(speakGreeting(baseConfig)).resolves.not.toThrow();
    });

    it("no-ops without throwing when SpeechSynthesisUtterance is absent", async () => {
      const speak = vi.fn();
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak, cancel: vi.fn() },
        configurable: true,
        writable: true,
      });
      Object.defineProperty(window, "SpeechSynthesisUtterance", {
        value: undefined,
        configurable: true,
        writable: true,
      });

      await expect(speakGreeting(baseConfig)).resolves.not.toThrow();
      expect(speak).not.toHaveBeenCalled();
    });

    it("swallows a throwing speak() and never lets it escape (chat must be unaffected)", async () => {
      const speak = vi.fn(() => {
        throw new Error("blocked by browser policy");
      });
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak, cancel: vi.fn() },
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

      await expect(speakGreeting(baseConfig)).resolves.not.toThrow();
      expect(speak).toHaveBeenCalledTimes(1);
    });

    it("swallows a throwing Utterance constructor and never lets it escape", async () => {
      const speak = vi.fn();
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak, cancel: vi.fn() },
        configurable: true,
        writable: true,
      });
      Object.defineProperty(window, "SpeechSynthesisUtterance", {
        value: class {
          constructor() {
            throw new Error("construction failed");
          }
        },
        configurable: true,
        writable: true,
      });

      await expect(speakGreeting(baseConfig)).resolves.not.toThrow();
      expect(speak).not.toHaveBeenCalled();
    });

    it("calls onBlocked when the capability check fails (no speechSynthesis)", async () => {
      Object.defineProperty(window, "speechSynthesis", {
        value: undefined,
        configurable: true,
        writable: true,
      });
      const onBlocked = vi.fn();

      await speakGreeting(baseConfig, onBlocked);

      expect(onBlocked).toHaveBeenCalledTimes(1);
    });

    it("calls onBlocked when SpeechSynthesisUtterance is absent", async () => {
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak: vi.fn(), cancel: vi.fn() },
        configurable: true,
        writable: true,
      });
      Object.defineProperty(window, "SpeechSynthesisUtterance", {
        value: undefined,
        configurable: true,
        writable: true,
      });
      const onBlocked = vi.fn();

      await speakGreeting(baseConfig, onBlocked);

      expect(onBlocked).toHaveBeenCalledTimes(1);
    });

    it("calls onBlocked when speak() throws synchronously", async () => {
      Object.defineProperty(window, "speechSynthesis", {
        value: {
          speak: vi.fn(() => {
            throw new Error("blocked by browser policy");
          }),
          cancel: vi.fn(),
        },
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
      const onBlocked = vi.fn();

      await speakGreeting(baseConfig, onBlocked);

      expect(onBlocked).toHaveBeenCalledTimes(1);
    });

    it("wires onBlocked to the utterance's error event for a genuine block reason (Chrome's not-allowed)", async () => {
      const speak = vi.fn();
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak, cancel: vi.fn() },
        configurable: true,
        writable: true,
      });
      class FakeUtterance {
        text: string;
        onerror: ((event: { error: string }) => void) | null = null;
        constructor(text: string) {
          this.text = text;
        }
      }
      Object.defineProperty(window, "SpeechSynthesisUtterance", {
        value: FakeUtterance,
        configurable: true,
        writable: true,
      });
      const onBlocked = vi.fn();

      await speakGreeting(baseConfig, onBlocked);

      expect(onBlocked).not.toHaveBeenCalled();
      const uttered = speak.mock.calls[0]?.[0] as FakeUtterance;
      uttered.onerror?.({ error: "not-allowed" });
      expect(onBlocked).toHaveBeenCalledTimes(1);
    });

    it.each(["canceled", "interrupted"])(
      "does NOT call onBlocked when the error is '%s' -- that means our own cancel() (or a newer speak()) stopped an utterance that genuinely played/queued, not a browser-policy block",
      async (errorCode) => {
        const speak = vi.fn();
        Object.defineProperty(window, "speechSynthesis", {
          value: { speak, cancel: vi.fn() },
          configurable: true,
          writable: true,
        });
        class FakeUtterance {
          text: string;
          onerror: ((event: { error: string }) => void) | null = null;
          constructor(text: string) {
            this.text = text;
          }
        }
        Object.defineProperty(window, "SpeechSynthesisUtterance", {
          value: FakeUtterance,
          configurable: true,
          writable: true,
        });
        const onBlocked = vi.fn();

        await speakGreeting(baseConfig, onBlocked);

        const uttered = speak.mock.calls[0]?.[0] as FakeUtterance;
        uttered.onerror?.({ error: errorCode });
        expect(onBlocked).not.toHaveBeenCalled();
      },
    );

    it("never calls onBlocked when speak() succeeds and no error event fires", async () => {
      const speak = vi.fn();
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak, cancel: vi.fn() },
        configurable: true,
        writable: true,
      });
      Object.defineProperty(window, "SpeechSynthesisUtterance", {
        value: class {
          onerror: (() => void) | null = null;
          constructor(public text: string) {}
        },
        configurable: true,
        writable: true,
      });
      const onBlocked = vi.fn();

      await speakGreeting(baseConfig, onBlocked);

      expect(onBlocked).not.toHaveBeenCalled();
    });

    it("greets with 'Rebecca' by name, not the old generic 'your assistant' text", () => {
      expect(TTS_GREETING_TEXT).toBe("Hi, I'm Rebecca, how can I help?");
    });

    function fakeVoice(name: string): SpeechSynthesisVoice {
      return { name, lang: "en-US", default: false, localService: true, voiceURI: name };
    }

    class FakeUtteranceWithVoice {
      text: string;
      voice: SpeechSynthesisVoice | null = null;
      rate = 1;
      onerror: ((event: { error: string }) => void) | null = null;
      constructor(text: string) {
        this.text = text;
      }
    }

    it("selects a known female-named voice when one is available", async () => {
      const speak = vi.fn();
      const getVoices = vi.fn(() => [
        fakeVoice("Microsoft David - English (United States)"),
        fakeVoice("Microsoft Zira - English (United States)"),
      ]);
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak, cancel: vi.fn(), getVoices },
        configurable: true,
        writable: true,
      });
      Object.defineProperty(window, "SpeechSynthesisUtterance", {
        value: FakeUtteranceWithVoice,
        configurable: true,
        writable: true,
      });

      await speakGreeting(baseConfig);

      const uttered = speak.mock.calls[0]?.[0] as FakeUtteranceWithVoice;
      expect(uttered.voice?.name).toBe("Microsoft Zira - English (United States)");
    });

    it("matches a voice whose name explicitly says 'Female' even if not on the known-name list", async () => {
      const speak = vi.fn();
      const getVoices = vi.fn(() => [
        fakeVoice("Google UK English Male"),
        fakeVoice("Google UK English Female"),
      ]);
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak, cancel: vi.fn(), getVoices },
        configurable: true,
        writable: true,
      });
      Object.defineProperty(window, "SpeechSynthesisUtterance", {
        value: FakeUtteranceWithVoice,
        configurable: true,
        writable: true,
      });

      await speakGreeting(baseConfig);

      const uttered = speak.mock.calls[0]?.[0] as FakeUtteranceWithVoice;
      expect(uttered.voice?.name).toBe("Google UK English Female");
    });

    it("leaves voice unset (browser default) when no female-sounding voice is available -- never throws", async () => {
      const speak = vi.fn();
      const getVoices = vi.fn(() => [fakeVoice("Microsoft David - English (United States)")]);
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak, cancel: vi.fn(), getVoices },
        configurable: true,
        writable: true,
      });
      Object.defineProperty(window, "SpeechSynthesisUtterance", {
        value: FakeUtteranceWithVoice,
        configurable: true,
        writable: true,
      });

      await expect(speakGreeting(baseConfig)).resolves.not.toThrow();
      const uttered = speak.mock.calls[0]?.[0] as FakeUtteranceWithVoice;
      expect(uttered.voice).toBeNull();
    });

    it("never throws when speechSynthesis has no getVoices at all (older/partial implementations)", async () => {
      const speak = vi.fn();
      Object.defineProperty(window, "speechSynthesis", {
        // Deliberately no getVoices — matches the plain-object mocks used
        // throughout the rest of this file, and real older browsers.
        value: { speak, cancel: vi.fn() },
        configurable: true,
        writable: true,
      });
      Object.defineProperty(window, "SpeechSynthesisUtterance", {
        value: FakeUtteranceWithVoice,
        configurable: true,
        writable: true,
      });

      await expect(speakGreeting(baseConfig)).resolves.not.toThrow();
      expect(speak).toHaveBeenCalledTimes(1);
    });

    it("sets a slightly slower rate for clarity, regardless of whether a voice match was found", async () => {
      const speak = vi.fn();
      Object.defineProperty(window, "speechSynthesis", {
        value: { speak, cancel: vi.fn(), getVoices: vi.fn(() => []) },
        configurable: true,
        writable: true,
      });
      Object.defineProperty(window, "SpeechSynthesisUtterance", {
        value: FakeUtteranceWithVoice,
        configurable: true,
        writable: true,
      });

      await speakGreeting(baseConfig);

      const uttered = speak.mock.calls[0]?.[0] as FakeUtteranceWithVoice;
      expect(uttered.rate).toBe(0.85);
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

    it("passes the greeting's slightly-slower rate through to cloud TTS too", async () => {
      isVoiceTtsEnabledMock.mockReturnValue(true);
      synthesizeSpeechMock.mockResolvedValue({ ok: true, audio: new Blob(["fake-mp3"]) });

      await speakGreeting(baseConfig);

      expect(synthesizeSpeechMock).toHaveBeenCalledWith(baseConfig, TTS_GREETING_TEXT, 0.85);
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
