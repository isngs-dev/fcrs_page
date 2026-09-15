import { afterEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

const { getLlmConfig } = await import("@/lib/llm-config");

describe("getLlmConfig", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("calls GET /admin/llm/config and maps the response to LlmConfig", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          provider: "openai",
          model: "gpt-4o",
          base_url: null,
          api_version: null,
          embedding_model: "text-embedding-3-small",
          embedding_base_url: null,
          embedding_dimensions: 768,
          has_api_key: true,
          has_embedding_api_key: false,
        }),
        { status: 200 }
      )
    );

    const result = await getLlmConfig();

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.config).toEqual({
        provider: "openai",
        model: "gpt-4o",
        baseUrl: null,
        apiVersion: null,
        embeddingModel: "text-embedding-3-small",
        embeddingBaseUrl: null,
        embeddingDimensions: 768,
        hasApiKey: true,
        hasEmbeddingApiKey: false,
      });
    }
    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/admin/llm/config");
  });

  it("returns an honest unconfigured state -- never a fabricated provider", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          provider: null,
          model: null,
          base_url: null,
          api_version: null,
          embedding_model: null,
          embedding_base_url: null,
          embedding_dimensions: null,
          has_api_key: false,
          has_embedding_api_key: false,
        }),
        { status: 200 }
      )
    );

    const result = await getLlmConfig();

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.config.provider).toBeNull();
      expect(result.config.hasApiKey).toBe(false);
    }
  });

  it("maps a 403 to a friendly permission message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "ROLE_NOT_PERMITTED", message: "nope", correlation_id: "corr-1" }),
        { status: 403 }
      )
    );

    const result = await getLlmConfig();

    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/permission/i);
      expect(result.correlationId).toBe("corr-1");
    }
  });

  it("maps a 401 to a session-expired message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "AUTHENTICATION_ERROR", message: "x", correlation_id: "c" }),
        { status: 401 }
      )
    );

    const result = await getLlmConfig();

    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/session/i);
    }
  });

  it("maps a non-AdminApiError network throw to a generic network message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("network down"));

    const result = await getLlmConfig();

    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/unable to reach/i);
    }
  });
});
