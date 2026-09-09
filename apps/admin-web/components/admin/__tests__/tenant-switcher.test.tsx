/**
 * Structural test for the sidebar "active chatbot" switcher (multi-chatbot
 * accounts). Renders via `renderToStaticMarkup` -- this repo's vitest config
 * runs `environment: "node"` (no DOM/RTL, see `admin-shell.test.ts`'s own
 * convention), so this only asserts markup shape, not the `onChange`
 * interaction (covered by `switch-tenant/__tests__/actions.test.ts` for the
 * action it calls, and manually in the browser per this session's
 * verification workflow).
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { TenantSwitcher } from "@/components/admin/tenant-switcher";

describe("TenantSwitcher", () => {
  it("renders nothing when the caller has no chatbots at all", () => {
    const html = renderToStaticMarkup(
      <TenantSwitcher chatbots={[]} activeTenantId={null} collapsed={false} />
    );
    expect(html).toBe("");
  });

  it("renders one option per chatbot, with the active one pre-selected", () => {
    const html = renderToStaticMarkup(
      <TenantSwitcher
        chatbots={[
          { id: "tenant-1", name: "Bot One", enabled: true },
          { id: "tenant-2", name: "Bot Two", enabled: true },
        ]}
        activeTenantId="tenant-2"
        collapsed={false}
      />
    );

    expect(html).toContain("Bot One");
    expect(html).toContain("Bot Two");
    expect(html).toMatch(/<option[^>]*value="tenant-2"[^>]*selected/);
  });

  it("marks a disabled chatbot's option as disabled, never switchable to", () => {
    const html = renderToStaticMarkup(
      <TenantSwitcher
        chatbots={[{ id: "tenant-1", name: "Bot One", enabled: false }]}
        activeTenantId={null}
        collapsed={false}
      />
    );

    expect(html).toMatch(/<option[^>]*disabled/);
    expect(html).toContain("Bot One (disabled)");
  });

  it("is visually hidden when the sidebar is collapsed", () => {
    const html = renderToStaticMarkup(
      <TenantSwitcher
        chatbots={[{ id: "tenant-1", name: "Bot One", enabled: true }]}
        activeTenantId="tenant-1"
        collapsed
      />
    );

    expect(html).toMatch(/class="hidden"/);
  });
});
