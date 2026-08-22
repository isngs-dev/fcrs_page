import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WidgetConfig } from "./config";

const authHeaderMock = vi.fn<() => { Authorization: string } | null>();

vi.mock("./session", () => ({
  authHeader: () => authHeaderMock(),
}));

const baseConfig: WidgetConfig = {
  clientKey: "pk_test_123",
  apiBase: "http://localhost:8000",
  mountSelector: null,
  debug: false,
  position: "right",
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("voice", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.resetModules();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    authHeaderMock.mockReset();
    authHeaderMock.mockReturnValue({ Authorization: "Bearer jwt.abc.def" });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  describe("transcribeAudio", () => {
    it("posts the audio blob as the raw body with its own type as Content-Type", async () => {
      fetchMock.mockResolvedValueOnce(jsonResponse(200, { text: "how much does an inspection cost" }));
      const { transcribeAudio } = await import("./voice");
      const audio = new Blob(["fake-audio"], { type: "audio/webm" });

      const result = await transcribeAudio(baseConfig, audio);

      expect(result).toEqual({ ok: true, text: "how much does an inspection cost" });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/public/chat/transcribe");
      expect(init.method).toBe("POST");
      expect(init.credentials).toBe("omit");
      expect(init.body).toBe(audio);
      expect(init.headers).toMatchObject({
        "Content-Type": "audio/webm",
        Authorization: "Bearer jwt.abc.def",
      });
    });

    it("returns NO_SESSION and issues no fetch when authHeader() is null", async () => {
      authHeaderMock.mockReturnValue(null);
      const { transcribeAudio } = await import("./voice");

      const result = await transcribeAudio(baseConfig, new Blob(["x"]));

      expect(result.ok).toBe(false);
      if (result.ok) throw new Error("expected error result");
      expect(result.error.errorCode).toBe("NO_SESSION");
      expect(fetchMock).not.toHaveBeenCalled();
    });

    it("returns NETWORK_ERROR (no throw) when fetch rejects", async () => {
      fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
      const { transcribeAudio } = await import("./voice");

      const result = await transcribeAudio(baseConfig, new Blob(["x"]));

      expect(result.ok).toBe(false);
      if (result.ok) throw new Error("expected error result");
      expect(result.error.errorCode).toBe("NETWORK_ERROR");
    });

    it("returns the backend's error_code/message on a non-2xx response", async () => {
      fetchMock.mockResolvedValueOnce(
        jsonResponse(422, { error_code: "VOICE_PROVIDER_NOT_CONFIGURED", message: "not configured" }),
      );
      const { transcribeAudio } = await import("./voice");

      const result = await transcribeAudio(baseConfig, new Blob(["x"]));

      expect(result.ok).toBe(false);
      if (result.ok) throw new Error("expected error result");
      expect(result.error.errorCode).toBe("VOICE_PROVIDER_NOT_CONFIGURED");
      expect(result.error.message).toBe("not configured");
    });

    it("returns INVALID_RESPONSE_SHAPE when the 200 body has no text field", async () => {
      fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));
      const { transcribeAudio } = await import("./voice");

      const result = await transcribeAudio(baseConfig, new Blob(["x"]));

      expect(result.ok).toBe(false);
      if (result.ok) throw new Error("expected error result");
      expect(result.error.errorCode).toBe("INVALID_RESPONSE_SHAPE");
    });
  });

  describe("synthesizeSpeech", () => {
    it("posts { text } as JSON and returns the raw audio blob on success", async () => {
      const audioBytes = new Blob(["fake-mp3-bytes"], { type: "audio/mpeg" });
      fetchMock.mockResolvedValueOnce(
        new Response(audioBytes, { status: 200, headers: { "Content-Type": "audio/mpeg" } }),
      );
      const { synthesizeSpeech } = await import("./voice");

      const result = await synthesizeSpeech(baseConfig, "We're open Monday through Friday.");

      expect(result.ok).toBe(true);
      if (!result.ok) throw new Error("expected ok result");
      expect(result.audio.type).toBe("audio/mpeg");

      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/public/chat/speak");
      expect(init.method).toBe("POST");
      expect(JSON.parse(init.body as string)).toEqual({ text: "We're open Monday through Friday." });
    });

    it("returns NO_SESSION and issues no fetch when authHeader() is null", async () => {
      authHeaderMock.mockReturnValue(null);
      const { synthesizeSpeech } = await import("./voice");

      const result = await synthesizeSpeech(baseConfig, "hello");

      expect(result.ok).toBe(false);
      if (result.ok) throw new Error("expected error result");
      expect(result.error.errorCode).toBe("NO_SESSION");
      expect(fetchMock).not.toHaveBeenCalled();
    });

    it("returns NETWORK_ERROR (no throw) when fetch rejects", async () => {
      fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
      const { synthesizeSpeech } = await import("./voice");

      const result = await synthesizeSpeech(baseConfig, "hello");

      expect(result.ok).toBe(false);
      if (result.ok) throw new Error("expected error result");
      expect(result.error.errorCode).toBe("NETWORK_ERROR");
    });

    it("returns the backend's error_code/message on a non-2xx response", async () => {
      fetchMock.mockResolvedValueOnce(
        jsonResponse(422, { error_code: "VOICE_PROVIDER_NOT_CONFIGURED", message: "not configured" }),
      );
      const { synthesizeSpeech } = await import("./voice");

      const result = await synthesizeSpeech(baseConfig, "hello");

      expect(result.ok).toBe(false);
      if (result.ok) throw new Error("expected error result");
      expect(result.error.errorCode).toBe("VOICE_PROVIDER_NOT_CONFIGURED");
    });
  });
});
