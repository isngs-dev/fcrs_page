/**
 * Live preview -- sample bubbles reflect the bot/visitor text colors,
 * same `renderToStaticMarkup` pattern as voice-agent-config.test.tsx.
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { WidgetPreview } from "@/app/(protected)/settings/widget-preview";

function bubbles(html: string): { bot: string; visitor: string } {
  const pick = (testId: string) => html.match(new RegExp(`<[^>]*data-testid="${testId}"[^>]*>`))?.[0] ?? "";
  return { bot: pick("widget-preview-bot-bubble"), visitor: pick("widget-preview-visitor-bubble") };
}

describe("WidgetPreview text colors", () => {
  it("colors the sample bot and visitor bubbles with the chosen text colors", () => {
    const html = renderToStaticMarkup(
      <WidgetPreview
        greeting=""
        tone=""
        launcherLabel=""
        accentColor="#16a34a"
        botTextColor="#7c2d12"
        visitorTextColor="#fefce8"
      />,
    );
    const { bot, visitor } = bubbles(html);

    expect(bot).toContain("color:#7c2d12");
    expect(visitor).toContain("color:#fefce8");
    expect(visitor).toContain("background-color:#16a34a");
  });

  it("leaves the bubbles on their default colors when the text colors are blank or not yet valid", () => {
    const html = renderToStaticMarkup(
      <WidgetPreview greeting="" tone="" launcherLabel="" botTextColor="" visitorTextColor="#fef" />,
    );
    const { bot, visitor } = bubbles(html);

    expect(bot).not.toBe("");
    expect(visitor).not.toBe("");
    expect(bot).not.toContain("color:");
    expect(visitor).not.toMatch(/(^|[;"])color:/);
  });
});
