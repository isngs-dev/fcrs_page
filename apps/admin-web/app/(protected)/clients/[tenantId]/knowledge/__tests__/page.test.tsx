/**
 * Regression test for the platform-admin knowledge redesign: "Upload
 * knowledge" (and `<UploadForm>`) must never render on this screen again --
 * uploading is exclusively a CLIENT_ADMIN capability now. The list is
 * rendered with `tenantId` passed through, turning on View/Export.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

const adminApiFetchMock = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    adminApiFetch: (path: string, init?: RequestInit) => adminApiFetchMock(path, init),
  };
});

const ClientKnowledgePage = (
  await import("@/app/(protected)/clients/[tenantId]/knowledge/page")
).default;

describe("ClientKnowledgePage (/clients/[tenantId]/knowledge)", () => {
  afterEach(() => {
    adminApiFetchMock.mockReset();
  });

  it("never renders 'Upload knowledge' or an upload form, and shows View/Export on the list", async () => {
    adminApiFetchMock.mockImplementation((path: string) => {
      if (path.includes("/ingestion/docs")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              docs: [
                {
                  doc_id: "doc-1",
                  title: null,
                  description: null,
                  filename: "pricing.txt",
                  content_type: "text/plain",
                  status: "parsed",
                  uploaded_by: null,
                  uploaded_by_name: null,
                  created_at: "2026-01-01T00:00:00Z",
                },
              ],
            }),
            { status: 200 }
          )
        );
      }
      return Promise.reject(new Error("unexpected path: " + path));
    });

    const element = await ClientKnowledgePage({
      params: Promise.resolve({ tenantId: "tenant-42" }),
    });
    const html = renderToStaticMarkup(element);

    expect(html).not.toMatch(/Upload knowledge/i);
    expect(html).not.toContain('name="file"');
    expect(html).toContain("Uploaded knowledge");
    expect(html).toMatch(/>View</);
    expect(html).toMatch(/>Export</);
    expect(html).toContain("/knowledge/download/doc-1?tenant_id=tenant-42");
  });
});
