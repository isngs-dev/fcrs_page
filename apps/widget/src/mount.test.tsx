import { act } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { __resetMountForTests, mountWidget } from "./mount";

describe("mountWidget", () => {
  beforeEach(() => {
    __resetMountForTests();
    document.body.innerHTML = "";
  });

  afterEach(() => {
    __resetMountForTests();
    document.body.innerHTML = "";
  });

  it("attaches an open shadow root to the host element and injects a <style>", () => {
    const { host, shadowRoot } = mountWidget(null);

    expect(host.id).toBe("chatbot-widget-root");
    expect(document.body.contains(host)).toBe(true);
    expect(host.shadowRoot).toBe(shadowRoot);
    expect(shadowRoot.mode).toBe("open");

    const style = shadowRoot.querySelector("style[data-chatbot-widget]");
    expect(style).not.toBeNull();
    expect(style?.textContent?.length ?? 0).toBeGreaterThan(0);
  });

  it("renders content inside the shadow root, not the light DOM", () => {
    const { shadowRoot, reactRoot } = mountWidget(null);

    act(() => {
      reactRoot.render(<button type="button">Chat</button>);
    });

    const inShadow = shadowRoot.querySelector("button");
    expect(inShadow).not.toBeNull();
    expect(inShadow?.textContent).toBe("Chat");

    // Must not have leaked into the light DOM.
    const inLightDom = document.body.querySelector("button");
    expect(inLightDom).toBeNull();
  });

  it("is idempotent: calling mountWidget twice does not create a second host or root", () => {
    const first = mountWidget(null);
    const second = mountWidget(null);

    expect(second).toBe(first);
    expect(document.querySelectorAll("#chatbot-widget-root").length).toBe(1);
  });

  it("reuses an integrator-provided element when data-mount selector matches", () => {
    const preplaced = document.createElement("div");
    preplaced.id = "my-mount-point";
    document.body.appendChild(preplaced);

    const { host } = mountWidget("#my-mount-point");

    expect(host).toBe(preplaced);
    expect(host.shadowRoot).not.toBeNull();
  });

  it("falls back to an auto-created host when the data-mount selector matches nothing", () => {
    const { host } = mountWidget("#does-not-exist");

    expect(host.id).toBe("chatbot-widget-root");
    expect(document.body.contains(host)).toBe(true);
  });

  it("stamps data-position=\"right\" on the host by default", () => {
    const { host } = mountWidget(null);

    expect(host.getAttribute("data-position")).toBe("right");
  });

  it("stamps data-position=\"left\" on the host when passed explicitly", () => {
    const { host } = mountWidget(null, "left");

    expect(host.getAttribute("data-position")).toBe("left");
  });
});
