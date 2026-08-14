/**
 * SR-27 slice 0 -- geometry-isolation tests for the shared `set-row` and
 * `settings-rail` primitives, verified BEFORE the Settings/Bot-settings
 * shells (slices 7-8) are built against them. Moved here from
 * `app/(protected)/settings/__tests__/` when the primitives themselves moved
 * to `components/admin/` (they are now shared across the `/settings` and
 * `/workspace` routes, not just within one route).
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { SetRow } from "@/components/admin/set-row";
import { SettingsRail } from "@/components/admin/settings-rail";

describe("SetRow (Console.dc.html:880-883 .set-row recipe)", () => {
  it("renders a 210px label column against a flexible field column", () => {
    const html = renderToStaticMarkup(
      <SetRow label="Workspace name" description="Shown in the console">
        <input />
      </SetRow>
    );
    expect(html).toContain("grid-cols-[210px_1fr]");
  });

  it("renders the label at 13px/600 and the description at 12px muted", () => {
    const html = renderToStaticMarkup(
      <SetRow label="Workspace name" description="Shown in the console">
        <input />
      </SetRow>
    );
    expect(html).toContain("text-[13px]");
    expect(html).toContain("font-semibold");
    expect(html).toContain("text-xs");
  });

  it("suppresses the bottom border when isLast is set (:last-child)", () => {
    const html = renderToStaticMarkup(
      <SetRow label="Time zone" isLast>
        <input />
      </SetRow>
    );
    expect(html).not.toContain("border-b");
  });

  it("slots arbitrary children as the field (text, textarea, disabled note, etc.)", () => {
    const html = renderToStaticMarkup(
      <SetRow label="Language">
        <p aria-disabled="true">Not available yet</p>
      </SetRow>
    );
    expect(html).toContain("Not available yet");
  });
});

describe("SettingsRail (Console.dc.html:869-876 .setnav recipe, 184px rail)", () => {
  it("renders at a fixed 184px width", () => {
    const html = renderToStaticMarkup(
      <SettingsRail
        eyebrow="Settings"
        rows={[{ kind: "link", key: "general", label: "General", href: "/settings", active: true }]}
      />
    );
    expect(html).toContain("w-[184px]");
  });

  it("renders an uppercase eyebrow label", () => {
    const html = renderToStaticMarkup(
      <SettingsRail
        eyebrow="Settings"
        rows={[{ kind: "link", key: "general", label: "General", href: "/settings", active: true }]}
      />
    );
    expect(html).toContain("uppercase");
    expect(html).toContain("Settings");
  });

  it("renders a disabled row with aria-disabled and no href (e.g. Billing)", () => {
    const html = renderToStaticMarkup(
      <SettingsRail eyebrow="Settings" rows={[{ kind: "disabled", key: "billing", label: "Billing" }]} />
    );
    expect(html).toMatch(/aria-disabled="true"/);
    expect(html).not.toMatch(/<a[^>]+Billing/);
  });

  it("renders link rows as real navigable <a> elements", () => {
    const html = renderToStaticMarkup(
      <SettingsRail
        eyebrow="Settings"
        rows={[{ kind: "link", key: "members", label: "Members", href: "/members" }]}
      />
    );
    expect(html).toMatch(/<a[^>]+href="\/members"/);
  });
});
