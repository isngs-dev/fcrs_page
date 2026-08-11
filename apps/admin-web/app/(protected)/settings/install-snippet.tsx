"use client";

/**
 * Install section for 6a: the embed script tag on an ink background with a
 * Copy button (HANDOFF-SPEC.md §3 "6a ... Install (script snippet on ink w/
 * Copy)"). Attribute shape (`data-client-key`, `data-api-base`) matches the
 * REAL production embed documented in `apps/widget/dev/host.html` /
 * `apps/widget/dev/README.md` step 3 -- not invented.
 *
 * The tenant's real `client_key` is a one-time-revealed secret (see
 * `app/(protected)/clients/[tenantId]/rotate-key-control.tsx`'s file header)
 * -- it is never present in `BotSettings` / `GET /admin/settings`'s response,
 * so this screen cannot honestly show the real key. `clientKey` is therefore
 * optional; when absent we render a clearly-labeled placeholder instead of
 * fabricating one, matching the CLAUDE.md §3 "no silent fallbacks" standard.
 */
import { useState } from "react";
import { Button } from "@/components/ui/button";

const PLACEHOLDER_CLIENT_KEY = "pk_YOUR_CLIENT_KEY";
const WIDGET_SCRIPT_SRC = "https://cdn.chatleads.io/widget.js";
const DEFAULT_API_BASE = "https://api.chatleads.io";

function buildSnippet(clientKey: string | undefined, apiBase: string | undefined): string {
  const key = clientKey ?? PLACEHOLDER_CLIENT_KEY;
  const base = apiBase ?? DEFAULT_API_BASE;
  return `<script src="${WIDGET_SCRIPT_SRC}" data-client-key="${key}" data-api-base="${base}"></script>`;
}

export function InstallSnippet({
  clientKey,
  apiBase,
}: {
  clientKey?: string;
  apiBase?: string;
}) {
  const [copyStatus, setCopyStatus] = useState<"idle" | "copied" | "unavailable">("idle");
  const snippet = buildSnippet(clientKey, apiBase);
  const isPlaceholder = clientKey === undefined;

  async function handleCopy() {
    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(snippet);
        setCopyStatus("copied");
        setTimeout(() => setCopyStatus("idle"), 2000);
        return;
      } catch {
        // fall through
      }
    }
    setCopyStatus("unavailable");
  }

  return (
    <section className="flex flex-col gap-3 rounded-[14px] border border-border p-5">
      <h2 className="text-sm font-bold text-foreground">Install</h2>
      {/* SR-15 D1: the Copy button's citron fill is deleted and re-decided
          to --secondary/--ink-2 -- it sits on the dark snippet block, so it
          needs to read as a control, not carry the system's one chromatic
          accent. */}
      <div className="flex items-center justify-between gap-2.5 rounded-[10px] bg-primary px-3.5 py-3">
        <code className="min-w-0 flex-1 overflow-x-auto font-mono text-xs whitespace-pre text-primary-foreground/80">
          {snippet}
        </code>
        <Button
          type="button"
          size="sm"
          onClick={handleCopy}
          className="shrink-0 bg-primary-foreground text-primary hover:bg-primary-foreground/90"
        >
          {copyStatus === "copied" ? "Copied!" : "Copy"}
        </Button>
      </div>
      {copyStatus === "unavailable" ? (
        <p role="alert" className="text-xs text-destructive">
          Couldn&apos;t copy automatically — select the snippet text above and copy manually.
        </p>
      ) : isPlaceholder ? (
        <p className="text-xs text-muted-foreground">
          Replace <code className="font-mono">{PLACEHOLDER_CLIENT_KEY}</code> with your real client
          key from the client&apos;s key rotation screen before installing — the real key is only
          shown once when generated and isn&apos;t exposed on this page.
        </p>
      ) : (
        <p className="text-xs text-muted-foreground">
          Paste this one script tag before <code className="font-mono">&lt;/body&gt;</code> on any
          page you want the widget to appear on.
        </p>
      )}
    </section>
  );
}
