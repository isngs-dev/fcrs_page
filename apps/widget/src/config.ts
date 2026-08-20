/**
 * Script-tag config parsing (S14.1 decision 3, scope item 2).
 *
 * Locates the widget's own `<script>` element and reads its `data-*`
 * attributes into a typed, validated config. Never throws — a
 * misconfigured embed (missing client key) must degrade to "mount
 * nothing", not an unhandled exception on the host page.
 */

export interface WidgetConfig {
  /** The tenant's public client key (`pk_...`). Not a secret. */
  clientKey: string;
  /** Gateway base URL — from `data-api-base`, else the build-time default. */
  apiBase: string;
  /** Optional CSS selector for a host-provided mount point (`data-mount`). */
  mountSelector: string | null;
  /** `data-debug="true"` opt-in — renders the diagnostic strip on failure. */
  debug: boolean;
  /** `data-position="left"|"right"` — which screen corner the launcher/panel
   * anchor to. Defaults to "right" (the pre-existing, unconfigurable
   * behavior every already-deployed embed keeps by not setting this
   * attribute at all). Any value other than exactly "left" (case-
   * insensitive, trimmed) falls back to "right" rather than throwing or
   * rendering nothing -- a typo'd data-position must never break the embed. */
  position: "left" | "right";
}

export interface MissingClientKeyError {
  readonly type: "MISSING_CLIENT_KEY";
  readonly message: string;
}

export type ConfigResult =
  | { ok: true; config: WidgetConfig }
  | { ok: false; error: MissingClientKeyError };

/**
 * Find the `<script>` tag that booted this bundle.
 *
 * Prefers `document.currentScript` (correct during synchronous top-level
 * eval — the only time entry.tsx calls this). Falls back to a selector
 * lookup for environments where `currentScript` is unavailable (e.g. some
 * test harnesses, or a script loaded via unusual means), per decision 3.
 */
export function findWidgetScript(doc: Document = document): HTMLScriptElement | null {
  const current = doc.currentScript;
  if (current instanceof HTMLScriptElement) {
    return current;
  }
  return doc.querySelector<HTMLScriptElement>("script[data-client-key]");
}

/**
 * Parse + validate a widget config from the given script element's
 * `data-*` attributes. Returns a typed result — never throws.
 */
export function parseConfig(script: HTMLScriptElement | null): ConfigResult {
  const clientKey = script?.dataset.clientKey?.trim();
  if (!clientKey) {
    return {
      ok: false,
      error: {
        type: "MISSING_CLIENT_KEY",
        message:
          "[chatbot-widget] MISSING_CLIENT_KEY: the <script> tag is missing a non-empty data-client-key attribute.",
      },
    };
  }

  const apiBase = script?.dataset.apiBase?.trim() || __WIDGET_API_BASE__;
  const mountSelector = script?.dataset.mount?.trim() || null;
  const debug = script?.dataset.debug?.trim().toLowerCase() === "true";
  const position: "left" | "right" =
    script?.dataset.position?.trim().toLowerCase() === "left" ? "left" : "right";

  return {
    ok: true,
    config: { clientKey, apiBase, mountSelector, debug, position },
  };
}

/** Convenience: locate the script + parse config in one call. */
export function loadConfig(doc: Document = document): ConfigResult {
  return parseConfig(findWidgetScript(doc));
}
