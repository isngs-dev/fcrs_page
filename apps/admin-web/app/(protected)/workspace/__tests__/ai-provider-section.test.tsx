/**
 * Structural test for `AiProviderSection` (self-service AI provider
 * settings, the fix for EMBEDDING_NOT_CONFIGURED). Renders via
 * `renderToStaticMarkup` -- this repo's vitest config runs
 * `environment: "node"` (no DOM/RTL) -- so this only asserts the prefilled
 * markup shape, not the submit interaction (covered by
 * `ai-provider-actions.test.ts` for the server action itself).
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { AiProviderSection } from "@/app/(protected)/workspace/ai-provider-section";
import type { LlmConfig } from "@/lib/llm-config";

function makeConfig(overrides: Partial<LlmConfig> = {}): LlmConfig {
  return {
    provider: null,
    model: null,
    baseUrl: null,
    apiVersion: null,
    embeddingModel: null,
    embeddingBaseUrl: null,
    embeddingDimensions: null,
    hasApiKey: false,
    hasEmbeddingApiKey: false,
    ...overrides,
  };
}

describe("AiProviderSection", () => {
  it("renders all 3 providers in the select", () => {
    const html = renderToStaticMarkup(<AiProviderSection currentConfig={makeConfig()} />);

    expect(html).toMatch(/<option[^>]*value="anthropic"/);
    expect(html).toMatch(/<option[^>]*value="openai"/);
    expect(html).toMatch(/<option[^>]*value="azure"/);
  });

  it("shows 'Not set' for the API key when unconfigured", () => {
    const html = renderToStaticMarkup(
      <AiProviderSection currentConfig={makeConfig({ hasApiKey: false })} />
    );

    expect(html).toContain("Not set.");
    expect(html).not.toContain("Currently configured.");
  });

  it("shows 'Currently configured' for the API key when set, and never renders the key itself", () => {
    const html = renderToStaticMarkup(
      <AiProviderSection currentConfig={makeConfig({ hasApiKey: true, provider: "openai" })} />
    );

    expect(html).toContain("Currently configured.");
    expect(html).not.toMatch(/value="sk-/);
  });

  it("prefills model/provider from currentConfig", () => {
    const html = renderToStaticMarkup(
      <AiProviderSection
        currentConfig={makeConfig({ provider: "anthropic", model: "claude-opus-4-8" })}
      />
    );

    expect(html).toMatch(/<option[^>]*value="anthropic"[^>]*selected/);
    expect(html).toMatch(/value="claude-opus-4-8"/);
  });

  it("mentions ingestion in the embeddings section (why this field matters)", () => {
    const html = renderToStaticMarkup(<AiProviderSection currentConfig={makeConfig()} />);

    expect(html).toMatch(/ingest/i);
  });

  it("credential inputs are always type=password, never plain text", () => {
    const html = renderToStaticMarkup(
      <AiProviderSection currentConfig={makeConfig({ hasApiKey: true, hasEmbeddingApiKey: true })} />
    );

    const passwordInputs = html.match(/type="password"/g) ?? [];
    expect(passwordInputs.length).toBe(2); // api key + embedding api key
  });
});
