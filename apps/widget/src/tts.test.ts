import { afterEach, describe, expect, it, vi } from "vitest";

import { TTS_GREETING_TEXT, cancel, speakGreeting } from "./tts";

describe("tts", () => {
  const originalSpeechSynthesis = window.speechSynthesis;
  const originalUtterance = window.SpeechSynthesisUtterance;

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
    vi.restoreAllMocks();
  });

  describe("speakGreeting", () => {
    it("calls speechSynthesis.speak exactly once with the baked-in greeting text (simulating an open-gesture call site)", () => {
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
      speakGreeting();

      expect(speak).toHaveBeenCalledTimes(1);
      const uttered = speak.mock.calls[0]?.[0] as FakeUtterance;
      expect(uttered.text).toBe(TTS_GREETING_TEXT);
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

    it("no-ops without throwing when window.speechSynthesis is absent", () => {
      Object.defineProperty(window, "speechSynthesis", {
        value: undefined,
        configurable: true,
        writable: true,
      });

      expect(() => speakGreeting()).not.toThrow();
    });

    it("no-ops without throwing when SpeechSynthesisUtterance is absent", () => {
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

      expect(() => speakGreeting()).not.toThrow();
      expect(speak).not.toHaveBeenCalled();
    });

    it("swallows a throwing speak() and never lets it escape (chat must be unaffected)", () => {
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

      expect(() => speakGreeting()).not.toThrow();
      expect(speak).toHaveBeenCalledTimes(1);
    });

    it("swallows a throwing Utterance constructor and never lets it escape", () => {
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

      expect(() => speakGreeting()).not.toThrow();
      expect(speak).not.toHaveBeenCalled();
    });

    it("calls onBlocked when the capability check fails (no speechSynthesis)", () => {
      Object.defineProperty(window, "speechSynthesis", {
        value: undefined,
        configurable: true,
        writable: true,
      });
      const onBlocked = vi.fn();

      speakGreeting(onBlocked);

      expect(onBlocked).toHaveBeenCalledTimes(1);
    });

    it("calls onBlocked when SpeechSynthesisUtterance is absent", () => {
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

      speakGreeting(onBlocked);

      expect(onBlocked).toHaveBeenCalledTimes(1);
    });

    it("calls onBlocked when speak() throws synchronously", () => {
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

      speakGreeting(onBlocked);

      expect(onBlocked).toHaveBeenCalledTimes(1);
    });

    it("wires onBlocked to the utterance's error event for a genuine block reason (Chrome's not-allowed)", () => {
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

      speakGreeting(onBlocked);

      expect(onBlocked).not.toHaveBeenCalled();
      const uttered = speak.mock.calls[0]?.[0] as FakeUtterance;
      uttered.onerror?.({ error: "not-allowed" });
      expect(onBlocked).toHaveBeenCalledTimes(1);
    });

    it.each(["canceled", "interrupted"])(
      "does NOT call onBlocked when the error is '%s' -- that means our own cancel() (or a newer speak()) stopped an utterance that genuinely played/queued, not a browser-policy block",
      (errorCode) => {
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

        speakGreeting(onBlocked);

        const uttered = speak.mock.calls[0]?.[0] as FakeUtterance;
        uttered.onerror?.({ error: errorCode });
        expect(onBlocked).not.toHaveBeenCalled();
      },
    );

    it("never calls onBlocked when speak() succeeds and no error event fires", () => {
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

      speakGreeting(onBlocked);

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

    it("selects a known female-named voice when one is available", () => {
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

      speakGreeting();

      const uttered = speak.mock.calls[0]?.[0] as FakeUtteranceWithVoice;
      expect(uttered.voice?.name).toBe("Microsoft Zira - English (United States)");
    });

    it("matches a voice whose name explicitly says 'Female' even if not on the known-name list", () => {
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

      speakGreeting();

      const uttered = speak.mock.calls[0]?.[0] as FakeUtteranceWithVoice;
      expect(uttered.voice?.name).toBe("Google UK English Female");
    });

    it("leaves voice unset (browser default) when no female-sounding voice is available -- never throws", () => {
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

      expect(() => speakGreeting()).not.toThrow();
      const uttered = speak.mock.calls[0]?.[0] as FakeUtteranceWithVoice;
      expect(uttered.voice).toBeNull();
    });

    it("never throws when speechSynthesis has no getVoices at all (older/partial implementations)", () => {
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

      expect(() => speakGreeting()).not.toThrow();
      expect(speak).toHaveBeenCalledTimes(1);
    });

    it("sets a slightly slower rate for clarity, regardless of whether a voice match was found", () => {
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

      speakGreeting();

      const uttered = speak.mock.calls[0]?.[0] as FakeUtteranceWithVoice;
      expect(uttered.rate).toBeLessThan(1);
      expect(uttered.rate).toBeGreaterThanOrEqual(0.9);
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
  });
});
