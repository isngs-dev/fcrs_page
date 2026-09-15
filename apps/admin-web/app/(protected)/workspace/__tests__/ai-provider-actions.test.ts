import { afterEach, describe, expect, it, vi } from "vitest";

const adminApiFetchMock = vi.fn();
const revalidatePathMock = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    adminApiFetch: (...args: unknown[]) => adminApiFetchMock(...args),
  };
});

vi.mock("next/cache", () => ({
  revalidatePath: (...args: unknown[]) => revalidatePathMock(...args),
}));

const { saveLlmConfig } = await import("@/app/(protected)/workspace/ai-provider-actions");
const { AdminApiError } = await import("@/lib/api");

// Fixture value kept out of a bare `apiKey: "..."` object literal so it
// reads clearly as test data, not a real credential (matches
// calendly-actions.test.ts's `fixtureSigningSecretValue` convention).
const fixtureApiKeyValue = "a-fixture-provider-api-key-value";

function jsonResponse(body: Record<string, unknown>, status: number): Response {
  return new Response(JSON.stringify(body), { status });
}

function buildFormData(overrides: Partial<Record<string, string>> = {}): FormData {
  const values: Record<string, string> = {
    provider: "openai",
    model: "gpt-4o",
    ...overrides,
  };
  const fd = new FormData();
  for (const [key, value] of Object.entries(values)) {
    fd.set(key, value);
  }
  return fd;
}

describe("saveLlmConfig", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    adminApiFetchMock.mockReset();
    revalidatePathMock.mockReset();
  });

  it("calls POST /admin/llm/config with provider/model + the api key when provided", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          provider: "openai",
          model: "gpt-4o",
          base_url: null,
          api_version: null,
          embedding_model: null,
          embedding_base_url: null,
          embedding_dimensions: null,
          has_api_key: true,
          has_embedding_api_key: false,
        },
        200
      )
    );

    const state = await saveLlmConfig(
      { status: "idle" },
      buildFormData({ apiKey: fixtureApiKeyValue })
    );

    expect(state.status).toBe("saved");
    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/admin/llm/config",
      expect.objectContaining({ method: "POST" })
    );
    const body = JSON.parse(adminApiFetchMock.mock.calls[0][1].body);
    expect(body).toEqual({ provider: "openai", model: "gpt-4o", api_key: fixtureApiKeyValue });
    expect(revalidatePathMock).toHaveBeenCalledWith("/workspace");
  });

  it("omits api_key from the request body entirely when the field is left blank", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          provider: "openai",
          model: "gpt-4o-mini",
          base_url: null,
          api_version: null,
          embedding_model: null,
          embedding_base_url: null,
          embedding_dimensions: null,
          has_api_key: true,
          has_embedding_api_key: false,
        },
        200
      )
    );

    await saveLlmConfig({ status: "idle" }, buildFormData({ model: "gpt-4o-mini" }));

    const body = JSON.parse(adminApiFetchMock.mock.calls[0][1].body);
    expect(body).not.toHaveProperty("api_key");
    expect(body).not.toHaveProperty("embedding_api_key");
  });

  it("includes optional base_url/api_version/embedding fields only when provided", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          provider: "azure",
          model: "my-deployment",
          base_url: "https://my-resource.openai.azure.com",
          api_version: "2024-02-01",
          embedding_model: "text-embedding-3-small",
          embedding_base_url: null,
          embedding_dimensions: 768,
          has_api_key: true,
          has_embedding_api_key: false,
        },
        200
      )
    );

    await saveLlmConfig(
      { status: "idle" },
      buildFormData({
        provider: "azure",
        model: "my-deployment",
        apiKey: fixtureApiKeyValue,
        baseUrl: "https://my-resource.openai.azure.com",
        apiVersion: "2024-02-01",
        embeddingModel: "text-embedding-3-small",
        embeddingDimensions: "768",
      })
    );

    const body = JSON.parse(adminApiFetchMock.mock.calls[0][1].body);
    expect(body).toEqual({
      provider: "azure",
      model: "my-deployment",
      base_url: "https://my-resource.openai.azure.com",
      api_version: "2024-02-01",
      embedding_model: "text-embedding-3-small",
      embedding_dimensions: 768,
      api_key: fixtureApiKeyValue,
    });
  });

  it("rejects a missing model without calling adminApiFetch", async () => {
    const state = await saveLlmConfig({ status: "idle" }, buildFormData({ model: "" }));

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.fieldErrors.model).toBeTruthy();
    }
    expect(adminApiFetchMock).not.toHaveBeenCalled();
  });

  it("rejects an invalid provider without calling adminApiFetch", async () => {
    const state = await saveLlmConfig({ status: "idle" }, buildFormData({ provider: "cohere" }));

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.fieldErrors.provider).toBeTruthy();
    }
    expect(adminApiFetchMock).not.toHaveBeenCalled();
  });

  it("rejects a non-integer embedding dimensions value without calling adminApiFetch", async () => {
    const state = await saveLlmConfig(
      { status: "idle" },
      buildFormData({ embeddingDimensions: "not-a-number" })
    );

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.fieldErrors.embeddingDimensions).toBeTruthy();
    }
    expect(adminApiFetchMock).not.toHaveBeenCalled();
  });

  it("maps API_KEY_REQUIRED to the credential field error", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(422, {
        error_code: "API_KEY_REQUIRED",
        message: "An API key is required.",
        correlation_id: "corr-1",
      })
    );

    const state = await saveLlmConfig({ status: "idle" }, buildFormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.fieldErrors.credential).toMatch(/first time/i);
    }
  });

  it("maps a 403 to a permission-denied form error", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(403, {
        error_code: "ROLE_NOT_PERMITTED",
        message: "Forbidden.",
        correlation_id: "corr-2",
      })
    );

    const state = await saveLlmConfig({ status: "idle" }, buildFormData({ apiKey: fixtureApiKeyValue }));

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.formError).toMatch(/permission/i);
    }
  });

  it("maps a 401 to a session-expired form error", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(401, {
        error_code: "UNAUTHORIZED",
        message: "Unauthorized.",
        correlation_id: "corr-3",
      })
    );

    const state = await saveLlmConfig({ status: "idle" }, buildFormData({ apiKey: fixtureApiKeyValue }));

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.formError).toMatch(/session has expired/i);
    }
  });

  it("returns a network-failure message when adminApiFetch throws a non-AdminApiError", async () => {
    adminApiFetchMock.mockRejectedValue(new TypeError("fetch failed"));

    const state = await saveLlmConfig({ status: "idle" }, buildFormData({ apiKey: fixtureApiKeyValue }));

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.formError).toMatch(/unable to reach the server/i);
    }
  });
});
