import { describe, expect, it } from "vitest";

import { findWidgetScript, parseConfig } from "./config";

function makeScript(attrs: Record<string, string>): HTMLScriptElement {
  const script = document.createElement("script");
  for (const [key, value] of Object.entries(attrs)) {
    script.setAttribute(key, value);
  }
  return script;
}

describe("parseConfig", () => {
  it("parses a valid client key into a config with the build-time default api base", () => {
    const script = makeScript({ "data-client-key": "pk_test_123" });
    const result = parseConfig(script);

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.config.clientKey).toBe("pk_test_123");
    expect(result.config.apiBase).toBe(__WIDGET_API_BASE__);
    expect(result.config.mountSelector).toBeNull();
    expect(result.config.debug).toBe(false);
  });

  it("returns a typed MISSING_CLIENT_KEY error (never throws) when data-client-key is absent", () => {
    const script = makeScript({});
    const result = parseConfig(script);

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.type).toBe("MISSING_CLIENT_KEY");
  });

  it("returns a typed MISSING_CLIENT_KEY error when data-client-key is blank/whitespace", () => {
    const script = makeScript({ "data-client-key": "   " });
    const result = parseConfig(script);

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.type).toBe("MISSING_CLIENT_KEY");
  });

  it("returns a typed MISSING_CLIENT_KEY error (never throws) when the script element itself is null", () => {
    const result = parseConfig(null);

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected error result");
    expect(result.error.type).toBe("MISSING_CLIENT_KEY");
  });

  it("lets data-api-base override the build-time default", () => {
    const script = makeScript({
      "data-client-key": "pk_test_123",
      "data-api-base": "https://gateway.example.com",
    });
    const result = parseConfig(script);

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.config.apiBase).toBe("https://gateway.example.com");
  });

  it("reads data-mount and data-debug", () => {
    const script = makeScript({
      "data-client-key": "pk_test_123",
      "data-mount": "#my-mount-point",
      "data-debug": "true",
    });
    const result = parseConfig(script);

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.config.mountSelector).toBe("#my-mount-point");
    expect(result.config.debug).toBe(true);
  });

  it("treats any non-'true' data-debug value as false", () => {
    const script = makeScript({ "data-client-key": "pk_test_123", "data-debug": "yes" });
    const result = parseConfig(script);

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.config.debug).toBe(false);
  });

  it("defaults position to 'right' when data-position is absent (every pre-existing embed)", () => {
    const script = makeScript({ "data-client-key": "pk_test_123" });
    const result = parseConfig(script);

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.config.position).toBe("right");
  });

  it("reads data-position='left' (case-insensitive, trimmed)", () => {
    const script = makeScript({ "data-client-key": "pk_test_123", "data-position": " LEFT " });
    const result = parseConfig(script);

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.config.position).toBe("left");
  });

  it("treats any non-'left' data-position value as 'right' rather than throwing", () => {
    const script = makeScript({ "data-client-key": "pk_test_123", "data-position": "top" });
    const result = parseConfig(script);

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok result");
    expect(result.config.position).toBe("right");
  });
});

describe("findWidgetScript", () => {
  it("falls back to a selector lookup when document.currentScript is unavailable", () => {
    const script = makeScript({ "data-client-key": "pk_test_123" });
    document.body.appendChild(script);

    try {
      // jsdom's document.currentScript is null outside of an executing
      // <script>, so this exercises the fallback path directly.
      const found = findWidgetScript(document);
      expect(found).toBe(script);
    } finally {
      script.remove();
    }
  });

  it("returns null when no script tag carries data-client-key", () => {
    const found = findWidgetScript(document);
    expect(found).toBeNull();
  });
});
