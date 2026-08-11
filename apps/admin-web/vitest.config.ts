import path from "node:path";
import { defineConfig } from "vitest/config";
import tsconfigPaths from "vite-tsconfig-paths";

export default defineConfig({
  plugins: [tsconfigPaths()],
  resolve: {
    alias: {
      // See vitest.server-only-stub.ts for why this alias exists.
      "server-only": path.resolve(__dirname, "./vitest.server-only-stub.ts"),
    },
  },
  test: {
    environment: "node",
    // SR-17: widened from `**/*.test.ts` to also match `.test.tsx` --
    // needed for the RBAC-aware rendering tests, which invoke server/client
    // components directly and assert on `react-dom/server`'s static markup
    // (no jsdom/@testing-library added -- neither is a repo dependency, and
    // CLAUDE.md §4 forbids adding one for this frontend-only sprint).
    include: ["**/*.test.ts", "**/*.test.tsx"],
    exclude: ["node_modules", ".next"],
    setupFiles: ["./vitest.setup.ts"],
  },
});
