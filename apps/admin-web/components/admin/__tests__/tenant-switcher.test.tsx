/**
 * Structural test for the sidebar "active chatbot" switcher (multi-chatbot
 * accounts). Renders via `renderToStaticMarkup` -- this repo's vitest config
 * runs `environment: "node"` (no DOM/RTL, see `admin-shell.test.ts`'s own
 * convention), so this only asserts markup shape, not the `onChange`
 * interaction (covered by `switch-tenant/__tests__/actions.test.ts` for the
 * switch action itself; the "+ Add a chatbot" navigation is a plain
 * `router.push`, verified manually in the browser per this session's
 * verification workflow).
 */
import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

// renderToStaticMarkup renders outside Next's real App Router tree, so
// `useRouter()` (used for the "+ Add a chatbot" navigation) has no provider
// to attach to -- stub it, matching this repo's existing `next/navigation`
// mocking convention (e.g. `switch-tenant/__tests__/actions.test.ts`).
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

const { TenantSwitcher } = await import("@/components/admin/tenant-switcher");

describe("TenantSwitcher", () => {
  it("renders nothing when the caller has no chatbots and can't create one either (CLIENT_AGENT, empty account)", () => {
    const html = renderToStaticMarkup(
      <TenantSwitcher chatbots={[]} activeTenantId={null} collapsed={false} canCreate={false} />
    );
    expect(html).toBe("");
  });

  it("still renders the dropdown with just '+ Add a chatbot' when the account has zero chatbots but the caller can create one", () => {
    const html = renderToStaticMarkup(
      <TenantSwitcher chatbots={[]} activeTenantId={null} collapsed={false} canCreate />
    );
    expect(html).toContain("Add a chatbot");
    expect(html).toMatch(/<option[^>]*value="__add_chatbot__"/);
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
        canCreate={false}
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
        canCreate={false}
      />
    );

    expect(html).toMatch(/<option[^>]*disabled/);
    expect(html).toContain("Bot One (disabled)");
  });

  it("appends '+ Add a chatbot' after the real options for CLIENT_ADMIN (canCreate)", () => {
    const html = renderToStaticMarkup(
      <TenantSwitcher
        chatbots={[{ id: "tenant-1", name: "Bot One", enabled: true }]}
        activeTenantId="tenant-1"
        collapsed={false}
        canCreate
      />
    );

    expect(html).toContain("Bot One");
    expect(html).toContain("+ Add a chatbot");
    expect(html.indexOf("Bot One")).toBeLessThan(html.indexOf("Add a chatbot"));
  });

  it("never shows '+ Add a chatbot' for a caller who can't create one (CLIENT_AGENT)", () => {
    const html = renderToStaticMarkup(
      <TenantSwitcher
        chatbots={[{ id: "tenant-1", name: "Bot One", enabled: true }]}
        activeTenantId="tenant-1"
        collapsed={false}
        canCreate={false}
      />
    );

    expect(html).not.toContain("Add a chatbot");
    expect(html).not.toMatch(/__add_chatbot__/);
  });

  it("is visually hidden when the sidebar is collapsed", () => {
    const html = renderToStaticMarkup(
      <TenantSwitcher
        chatbots={[{ id: "tenant-1", name: "Bot One", enabled: true }]}
        activeTenantId="tenant-1"
        collapsed
        canCreate={false}
      />
    );

    expect(html).toMatch(/class="hidden"/);
  });
});
