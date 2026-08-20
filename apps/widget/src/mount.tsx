/**
 * Shadow DOM mount (S14.1 decision 2, scope item 4).
 *
 * Creates (or reuses an integrator-provided) host element, attaches an
 * *open* shadow root, injects the compiled widget CSS as a single
 * `<style>`, and `createRoot()`s React inside the shadow root. Idempotent:
 * calling `mountWidget` a second time is a no-op, not a double mount.
 */
import { createRoot, type Root } from "react-dom/client";
// A plain TS string export, not a `.css` file imported via `?raw`/
// `?inline` — see widgetCss.ts for why (Vite CSS-query imports proved
// unreliable specifically under this repo's Vitest transform pipeline).
// The whole bundle still stays one widget.js (decision 1's single-file
// embed contract) since this is just a normal module import.
import { widgetCss } from "./ui/widgetCss";

const HOST_ELEMENT_ID = "chatbot-widget-root";

export interface MountResult {
  host: HTMLElement;
  shadowRoot: ShadowRoot;
  reactRoot: Root;
}

// Module-scoped so a second boot() call (e.g. the script tag somehow
// evaluated twice) is detected and short-circuited rather than creating a
// second host/root.
let activeMount: MountResult | null = null;

function resolveHostElement(mountSelector: string | null, doc: Document): HTMLElement {
  if (mountSelector) {
    const existing = doc.querySelector<HTMLElement>(mountSelector);
    if (existing) return existing;
    console.error(
      `[chatbot-widget] data-mount selector "${mountSelector}" matched no element; falling back to an auto-created host.`,
    );
  }

  const existingAuto = doc.getElementById(HOST_ELEMENT_ID);
  if (existingAuto instanceof HTMLElement) return existingAuto;

  const created = doc.createElement("div");
  created.id = HOST_ELEMENT_ID;
  doc.body.appendChild(created);
  return created;
}

/**
 * Create (or reuse) the shadow host, attach an open shadow root, inject
 * CSS, and set up the React root. Safe to call more than once — returns
 * the existing mount on subsequent calls instead of mounting again.
 *
 * `position` (default "right", matching every pre-existing embed's
 * unconfigurable behavior) is stamped as a `data-position` attribute on the
 * light-DOM host element -- `widgetCss.ts`'s `:host([data-position="left"])`
 * rules read it from there to flip the launcher/panel/teaser to the left
 * edge. Stamped on the HOST (light DOM), not an element inside the shadow
 * root, because `:host()` selectors inside a shadow-scoped stylesheet only
 * ever match attributes on the shadow root's own host element.
 */
export function mountWidget(
  mountSelector: string | null,
  position: "left" | "right" = "right",
  doc: Document = document,
): MountResult {
  if (activeMount) {
    return activeMount;
  }

  const host = resolveHostElement(mountSelector, doc);
  host.setAttribute("data-position", position);

  // A shadow root can only be attached once per element — if one already
  // exists (e.g. re-entrant call on a reused host), reuse it rather than
  // throwing.
  const shadowRoot = host.shadowRoot ?? host.attachShadow({ mode: "open" });

  let style = shadowRoot.querySelector<HTMLStyleElement>("style[data-chatbot-widget]");
  if (!style) {
    style = doc.createElement("style");
    style.setAttribute("data-chatbot-widget", "true");
    style.textContent = widgetCss;
    shadowRoot.appendChild(style);
  }

  let reactMountNode = shadowRoot.querySelector<HTMLDivElement>("div[data-chatbot-widget-app]");
  if (!reactMountNode) {
    reactMountNode = doc.createElement("div");
    reactMountNode.setAttribute("data-chatbot-widget-app", "true");
    shadowRoot.appendChild(reactMountNode);
  }

  const reactRoot = createRoot(reactMountNode);

  activeMount = { host, shadowRoot, reactRoot };
  return activeMount;
}

/** Test-only reset so each test gets a fresh module-scoped mount state. */
export function __resetMountForTests(): void {
  activeMount = null;
}
