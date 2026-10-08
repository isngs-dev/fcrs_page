/**
 * AI voice agent ("Call Us") -- initial render of <VoiceAgentConfigCard>,
 * same `renderToStaticMarkup` pattern as missed-call-config.test.tsx.
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { VoiceAgentConfigCard } from "@/app/(protected)/settings/voice-agent-config";
import type { VoiceAgentConfig, VoiceAgentConfigResult } from "@/lib/voice-agent";

const CONFIG: VoiceAgentConfig = {
  enabled: true,
  greeting: "Hi, you've reached ISN Roofing.",
  transferNumber: "+17868233553",
  timezone: "America/New_York",
  openTime: "09:00",
  closeTime: "18:00",
  openDays: [0, 1, 2, 3, 4],
  maxMinutes: 10,
  plivoReady: true,
};

describe("VoiceAgentConfigCard", () => {
  it("renders the saved transfer number, hours and greeting", () => {
    const result: VoiceAgentConfigResult = { status: "ok", config: CONFIG };
    const html = renderToStaticMarkup(<VoiceAgentConfigCard result={result} />);

    expect(html).toMatch(/AI voice agent \(Call Us\)/);
    expect(html).toContain('value="+17868233553"');
    expect(html).toContain('value="09:00"');
    expect(html).toContain('value="18:00"');
    expect(html).toContain("Hi, you&#x27;ve reached ISN Roofing.");
    expect(html).not.toMatch(/isn&#x27;t set up on the server/);
  });

  it("warns that Call Us stays hidden until Plivo is set up", () => {
    const result: VoiceAgentConfigResult = { status: "ok", config: { ...CONFIG, plivoReady: false } };
    const html = renderToStaticMarkup(<VoiceAgentConfigCard result={result} />);

    expect(html).toMatch(/isn&#x27;t set up on the server yet/);
  });

  it("renders an honest error and still shows the form on a fetch failure", () => {
    const result: VoiceAgentConfigResult = { status: "error", message: "Nope.", correlationId: "c-1" };
    const html = renderToStaticMarkup(<VoiceAgentConfigCard result={result} />);

    expect(html).toMatch(/Unable to load this setting/);
    expect(html).toContain('value="+17868233553"'); // default transfer number
  });
});
