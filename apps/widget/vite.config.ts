import { resolve } from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

/**
 * Vite library-mode config for the embeddable widget bundle.
 *
 * Decision 1 (S14.1): single IIFE entry, React bundled IN (not external) so
 * the widget depends on zero host-page globals and no module system. Output
 * is a single fixed-name `widget.js` — that filename is the production
 * embed contract (`<script src=".../widget.js" ...>`).
 *
 * `__WIDGET_API_BASE__` is a build-time constant (decision 3): the default
 * gateway URL baked in when the integrator doesn't supply `data-api-base`.
 * It is a public URL, never a secret, sourced from `VITE_WIDGET_API_BASE`
 * (see `.env.example`).
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiBase = env.VITE_WIDGET_API_BASE ?? "http://localhost:8000";

  return {
    plugins: [react()],
    define: {
      __WIDGET_API_BASE__: JSON.stringify(apiBase),
    },
    build: {
      lib: {
        entry: resolve(__dirname, "src/entry.tsx"),
        name: "ChatbotWidget",
        formats: ["iife"],
        fileName: () => "widget.js",
      },
      // React/ReactDOM are deliberately NOT external — they must be
      // compiled into the single IIFE (decision 1 rejects the peer-dep
      // "library default"). Widget CSS is a plain TS string export
      // (src/ui/widgetCss.ts, not a `.css` file) injected into the shadow
      // root at runtime — no separate .css asset is emitted, keeping the
      // embed contract a single widget.js file.
      // Keep production minification on (Vite's default esbuild minifier).
    },
    server: {
      port: 5173,
    },
  };
});
