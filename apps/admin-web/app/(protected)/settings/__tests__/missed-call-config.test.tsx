/**
 * Missed-call text-back -- tests for <MissedCallConfig>, using this repo's
 * established `environment: "node"` `renderToStaticMarkup` pattern (see
 * coverage-gaps.test.tsx's header comment for the full rationale). These
 * prove the INITIAL rendered structure (unset state, populated state, error
 * state, webhook URL) -- submitting the save form requires a real browser
 * and is covered by live verification instead.
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { MissedCallConfig } from "@/app/(protected)/settings/missed-call-config";
import type { CallConfigResult } from "@/lib/calls";

describe("MissedCallConfig", () => {
  it("renders an empty form with the unset state before first save", () => {
    const result: CallConfigResult = {
      status: "ok",
      config: { monitoredPhoneNumber: null, enabled: false, textBackMessage: null },
    };
    const html = renderToStaticMarkup(
      <MissedCallConfig result={result} ownTenantId="tenant-1" />
    );

    expect(html).toMatch(/Missed-call text-back/i);
    expect(html).not.toContain('value="+1');
  });

  it("renders the saved phone number, message, and enabled checkbox state", () => {
    const result: CallConfigResult = {
      status: "ok",
      config: {
        monitoredPhoneNumber: "+15005550006",
        enabled: true,
        textBackMessage: "Sorry we missed your call!",
      },
    };
    const html = renderToStaticMarkup(
      <MissedCallConfig result={result} ownTenantId="tenant-1" />
    );

    expect(html).toContain("+15005550006");
    expect(html).toContain("Sorry we missed your call!");
    expect(html).toMatch(/checked=""/);
  });

  it("renders the webhook URL scoped to the given tenant", () => {
    const result: CallConfigResult = {
      status: "ok",
      config: { monitoredPhoneNumber: null, enabled: false, textBackMessage: null },
    };
    const html = renderToStaticMarkup(
      <MissedCallConfig result={result} ownTenantId="tenant-abc" />
    );

    expect(html).toContain("/public/calls/twilio/tenant-abc");
  });

  it("renders an honest inline error, never a blank/fabricated form, on a fetch failure", () => {
    const result: CallConfigResult = {
      status: "error",
      message: "Something went wrong.",
      correlationId: "corr-1",
    };
    const html = renderToStaticMarkup(
      <MissedCallConfig result={result} ownTenantId="tenant-1" />
    );

    expect(html).toMatch(/Unable to load this setting/i);
    // The form (and webhook URL) still render below the error so the admin
    // can still configure it fresh.
    expect(html).toContain("/public/calls/twilio/tenant-1");
  });
});
