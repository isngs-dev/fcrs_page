import { defineConfig } from "astro/config";

// Static-first, prerendered output — no SSR/adapter needed. See CLAUDE.md
// "Stack" section: frontend/ is Astro static, hand-written CSS only.
export default defineConfig({
  output: "static",
  site: "https://estimate.fcrsga.com",
  // Dev-only floating pill (bottom-center) that Astro injects by default in
  // `astro dev` — unrelated to the embedded chatbot widget, but sits in the
  // same viewport area and was being mistaken for/overlapping it.
  devToolbar: {
    enabled: false,
  },
  build: {
    format: "file",
  },
  vite: {
    server: {
      // Dev-only: forwards relative /api/* calls to the local backend so
      // the estimate form works without a shared origin. Production
      // deploys must front frontend+backend behind the same domain (or
      // swap the frontend to an absolute PUBLIC_API_URL) — this proxy
      // does not apply to the built static output.
      proxy: {
        "/api": {
          target: "http://localhost:3000",
          changeOrigin: true,
        },
      },
    },
  },
});
