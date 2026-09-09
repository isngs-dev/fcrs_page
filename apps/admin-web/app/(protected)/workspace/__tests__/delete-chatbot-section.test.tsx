/**
 * Structural test for `DeleteChatbotSection` (delete-the-active-chatbot
 * feature). Renders via `renderToStaticMarkup` -- this repo's vitest config
 * runs `environment: "node"` (no DOM/RTL, see `admin-shell.test.ts`'s own
 * convention) -- so this only asserts the closed-dialog markup shape, not
 * the type-to-confirm gating or the click/submit interaction (covered by
 * `delete-chatbot-actions.test.ts` for the server action itself; the
 * confirm-dialog's own interaction is verified manually in the browser per
 * this session's verification workflow, matching `tenant-switcher.test.tsx`'s
 * own documented convention for the same limitation).
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

const { DeleteChatbotSection } = await import(
  "@/app/(protected)/workspace/delete-chatbot-section"
);

describe("DeleteChatbotSection", () => {
  it("names the current chatbot in the danger-zone copy", () => {
    const html = renderToStaticMarkup(<DeleteChatbotSection currentName="Acme Support Bot" />);

    expect(html).toContain("Danger zone");
    expect(html).toContain("Delete this chatbot");
    expect(html).toContain("Acme Support Bot");
  });

  it("renders the delete button as live/clickable, never disabled (unlike the old dead placeholder)", () => {
    const html = renderToStaticMarkup(<DeleteChatbotSection currentName="Acme Support Bot" />);

    expect(html).not.toMatch(/disabled=""[^>]*>\s*Delete this chatbot/);
  });

  it("does not render the confirm dialog until opened (closed by default)", () => {
    const html = renderToStaticMarkup(<DeleteChatbotSection currentName="Acme Support Bot" />);

    expect(html).not.toContain("alertdialog");
    expect(html).not.toMatch(/Type.*to confirm/);
  });
});
