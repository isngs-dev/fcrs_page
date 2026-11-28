import { defineConfig } from "astro/config";
import { loadEnv } from "vite";

// The estimate form posts to the chatbot platform's form intake. A build
// without that URL would ship a form that silently goes nowhere, so refuse.
const { PUBLIC_INTAKE_URL } = loadEnv("production", process.cwd(), "");
if (process.argv.includes("build") && !PUBLIC_INTAKE_URL) {
  throw new Error(
    "PUBLIC_INTAKE_URL is not set. Copy the Form URL from the admin console " +
      "(Outreach -> Lead intake) into the build environment."
  );
}

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
