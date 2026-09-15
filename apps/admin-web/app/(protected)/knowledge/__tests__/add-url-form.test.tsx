/**
 * Structural test for `AddUrlForm` (add-a-website knowledge source
 * feature). Renders via `renderToStaticMarkup` -- this repo's vitest config
 * runs `environment: "node"` (no DOM/RTL, see `admin-shell.test.ts`'s own
 * convention), so this only asserts the idle-state markup shape, not the
 * submit/polling interaction (covered by `actions.test.ts` for
 * `addKnowledgeUrl` itself, and by `upload-form.tsx`'s exported
 * `StatusPanel`, which this component reuses unchanged).
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { AddUrlForm } from "@/app/(protected)/knowledge/add-url-form";

describe("AddUrlForm", () => {
  it("renders a URL input and an 'Add website' submit button", () => {
    const html = renderToStaticMarkup(<AddUrlForm />);

    expect(html).toMatch(/type="url"/);
    expect(html).toMatch(/name="url"/);
    expect(html).toContain("Add website");
  });

  it("renders optional title/description fields, matching UploadForm's shape", () => {
    const html = renderToStaticMarkup(<AddUrlForm />);

    expect(html).toMatch(/name="title"/);
    expect(html).toMatch(/name="description"/);
  });

  it("does not render a status panel before any submission", () => {
    const html = renderToStaticMarkup(<AddUrlForm />);

    expect(html).not.toMatch(/Ingested successfully/);
    expect(html).not.toMatch(/Queued for ingestion/);
  });
});
