"use client";

/**
 * "AI provider" settings section -- the self-service fix for
 * EMBEDDING_NOT_CONFIGURED: lets a CLIENT_ADMIN set their chatbot's LLM
 * chat provider AND embedding provider themselves (previously only settable
 * via a temporary debug endpoint, with no admin-web UI at all). Mirrors
 * `calendly-section.tsx`'s `SoftCard`/`SetRow` shape, but -- unlike
 * Calendly, which has no GET and starts blank every load -- this is
 * pre-filled from `currentConfig` (`GET /admin/llm/config`), the first
 * provider-picker UI in this app (native `<select>`, one flat field set for
 * all 3 providers rather than a dynamic per-provider switcher -- see the
 * plan's design decision 5).
 *
 * Credential fields (API key / embedding API key) are never pre-filled --
 * only a "Currently configured" / "Not set" indicator driven by
 * `hasApiKey`/`hasEmbeddingApiKey`. Leaving one blank on save KEEPS the
 * existing key (the backend preserves it); this is proportionate, not
 * `RotateKeyControl`-style show-once reveal, since these are provider
 * credentials the admin already holds elsewhere, not a secret this app
 * generates.
 */
import { useActionState, useId, useState } from "react";
import { useFormStatus } from "react-dom";
import { Button } from "@/components/ui/button";
import { SoftCard } from "@/components/admin/soft-card";
import { SetRow, SET_ROW_FIELD_CLASS } from "@/components/admin/set-row";
import { saveLlmConfig, type SaveLlmConfigState } from "@/app/(protected)/workspace/ai-provider-actions";
import type { LlmConfig } from "@/lib/llm-config";

const initialState: SaveLlmConfigState = { status: "idle" };

const PROVIDER_OPTIONS = [
  { value: "anthropic", label: "Anthropic" },
  { value: "openai", label: "OpenAI" },
  { value: "azure", label: "Azure OpenAI" },
] as const;

function SaveButton() {
  const { pending } = useFormStatus();
  return (
    <Button type="submit" disabled={pending}>
      {pending ? "Saving…" : "Save AI provider settings"}
    </Button>
  );
}

function CredentialStatus({ configured }: { configured: boolean }) {
  return (
    <span className={configured ? "text-[var(--success-fg)]" : "text-muted-foreground"}>
      {configured ? "Currently configured." : "Not set."}
    </span>
  );
}

export function AiProviderSection({ currentConfig }: { currentConfig: LlmConfig }) {
  const [state, formAction] = useActionState(saveLlmConfig, initialState);

  const [provider, setProvider] = useState(currentConfig.provider ?? "openai");
  const [model, setModel] = useState(currentConfig.model ?? "");
  const [baseUrl, setBaseUrl] = useState(currentConfig.baseUrl ?? "");
  const [apiVersion, setApiVersion] = useState(currentConfig.apiVersion ?? "");
  const [embeddingModel, setEmbeddingModel] = useState(currentConfig.embeddingModel ?? "");
  const [embeddingBaseUrl, setEmbeddingBaseUrl] = useState(currentConfig.embeddingBaseUrl ?? "");
  const [embeddingDimensions, setEmbeddingDimensions] = useState(
    currentConfig.embeddingDimensions?.toString() ?? ""
  );

  const hasApiKey = state.status === "saved" ? state.config.hasApiKey : currentConfig.hasApiKey;
  const hasEmbeddingApiKey =
    state.status === "saved" ? state.config.hasEmbeddingApiKey : currentConfig.hasEmbeddingApiKey;

  const fieldErrors = state.status === "error" ? state.fieldErrors : {};
  const formError = state.status === "error" ? state.formError : null;

  const providerSelectId = useId();
  const modelId = useId();
  const apiKeyId = useId();
  const baseUrlId = useId();
  const apiVersionId = useId();
  const embeddingModelId = useId();
  const embeddingApiKeyId = useId();
  const embeddingBaseUrlId = useId();
  const embeddingDimensionsId = useId();

  return (
    <SoftCard className="flex scroll-mt-16 flex-col px-[22px] pb-4 pt-1" id="settings-ai-provider">
      <h2 className="pb-0.5 pt-4 text-[15px] font-semibold text-foreground">AI provider</h2>
      <p className="pb-2 text-xs text-muted-foreground">
        Required before this chatbot can answer questions or ingest knowledge-base documents.
        Applies to this active chatbot only.
      </p>

      {state.status === "saved" ? (
        <p role="status" className="mb-2 rounded-md border border-border bg-secondary p-3 text-sm text-foreground">
          Saved. {state.config.provider} / {state.config.model} is now configured.
        </p>
      ) : null}
      {formError ? (
        <p role="alert" className="mb-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
          {formError}
        </p>
      ) : null}

      <form action={formAction}>
        <SetRow label="Provider" description="The service that generates chat replies." htmlFor={providerSelectId}>
          <select
            id={providerSelectId}
            name="provider"
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            className={SET_ROW_FIELD_CLASS}
          >
            {PROVIDER_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </SetRow>

        <SetRow label="Model" description="e.g. claude-opus-4-8, gpt-4o, or your Azure deployment name." htmlFor={modelId}>
          <input
            id={modelId}
            name="model"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="gpt-4o"
            className={SET_ROW_FIELD_CLASS}
          />
          {fieldErrors.model ? (
            <p role="alert" className="mt-1.5 text-sm text-destructive">
              {fieldErrors.model}
            </p>
          ) : null}
        </SetRow>

        <SetRow label="API key" description="Your provider dashboard's secret key." htmlFor={apiKeyId}>
          <input
            id={apiKeyId}
            name="apiKey"
            type="password"
            placeholder={hasApiKey ? "Leave blank to keep the current key" : "Required"}
            className={SET_ROW_FIELD_CLASS}
          />
          <p className="mt-1.5 text-xs">
            <CredentialStatus configured={hasApiKey} />
          </p>
          {fieldErrors.credential ? (
            <p role="alert" className="mt-1.5 text-sm text-destructive">
              {fieldErrors.credential}
            </p>
          ) : null}
        </SetRow>

        <SetRow
          label="Base URL (optional)"
          description="Only needed for Azure or a custom/self-hosted endpoint."
          htmlFor={baseUrlId}
        >
          <input
            id={baseUrlId}
            name="baseUrl"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="https://my-resource.openai.azure.com"
            className={SET_ROW_FIELD_CLASS}
          />
        </SetRow>

        <SetRow label="API version (optional)" description="Azure only." htmlFor={apiVersionId}>
          <input
            id={apiVersionId}
            name="apiVersion"
            value={apiVersion}
            onChange={(e) => setApiVersion(e.target.value)}
            placeholder="2024-02-01"
            className={SET_ROW_FIELD_CLASS}
          />
        </SetRow>

        <h3 className="mt-4 pb-0.5 text-[13px] font-semibold text-foreground">Embeddings</h3>
        <p className="pb-2 text-xs text-muted-foreground">
          Used to turn uploaded/scraped knowledge into searchable vectors. A chatbot cannot
          ingest a document or a website until an embedding model is set here.
        </p>

        <SetRow label="Embedding model" description="e.g. text-embedding-3-small." htmlFor={embeddingModelId}>
          <input
            id={embeddingModelId}
            name="embeddingModel"
            value={embeddingModel}
            onChange={(e) => setEmbeddingModel(e.target.value)}
            placeholder="text-embedding-3-small"
            className={SET_ROW_FIELD_CLASS}
          />
        </SetRow>

        <SetRow
          label="Embedding API key"
          description="Only needed if it's different from the chat API key above."
          htmlFor={embeddingApiKeyId}
        >
          <input
            id={embeddingApiKeyId}
            name="embeddingApiKey"
            type="password"
            placeholder={
              hasEmbeddingApiKey
                ? "Leave blank to keep the current key"
                : "Leave blank to reuse the API key above"
            }
            className={SET_ROW_FIELD_CLASS}
          />
          <p className="mt-1.5 text-xs">
            <CredentialStatus configured={hasEmbeddingApiKey} />
          </p>
        </SetRow>

        <SetRow
          label="Embedding base URL (optional)"
          description="Only needed if embeddings use a different endpoint than chat."
          htmlFor={embeddingBaseUrlId}
        >
          <input
            id={embeddingBaseUrlId}
            name="embeddingBaseUrl"
            value={embeddingBaseUrl}
            onChange={(e) => setEmbeddingBaseUrl(e.target.value)}
            placeholder="https://api.openai.com/v1"
            className={SET_ROW_FIELD_CLASS}
          />
        </SetRow>

        <SetRow
          label="Embedding dimensions (optional)"
          description="Must match what's already embedded if you change this later."
          htmlFor={embeddingDimensionsId}
          isLast
        >
          <input
            id={embeddingDimensionsId}
            name="embeddingDimensions"
            type="number"
            min={1}
            max={8192}
            value={embeddingDimensions}
            onChange={(e) => setEmbeddingDimensions(e.target.value)}
            placeholder="768"
            className={SET_ROW_FIELD_CLASS}
          />
          {fieldErrors.embeddingDimensions ? (
            <p role="alert" className="mt-1.5 text-sm text-destructive">
              {fieldErrors.embeddingDimensions}
            </p>
          ) : null}
        </SetRow>

        <div className="py-3">
          <SaveButton />
        </div>
      </form>
    </SoftCard>
  );
}
