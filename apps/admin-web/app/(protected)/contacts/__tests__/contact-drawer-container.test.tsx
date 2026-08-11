import { afterEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();
vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

const { ContactDrawerContainer } = await import("@/app/(protected)/contacts/contact-drawer-container");

function contactDetailResponse() {
  return new Response(
    JSON.stringify({
      contact_id: "contact-1",
      account_id: null,
      lead_id: null,
      name: "Ada",
      email: "ada@example.com",
      phone: null,
      owner_agent_id: null,
      created_at: "2026-07-15T00:00:00Z",
    }),
    { status: 200 }
  );
}

function timelineResponse() {
  return new Response(
    JSON.stringify({
      subject: { kind: "contact", id: "contact-1", converted_to_contact_id: null },
      degraded: false,
      sources: {},
      items: [],
      next_before: null,
    }),
    { status: 200 }
  );
}

describe("ContactDrawerContainer -- server composition", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("fetches exactly two things (detail + timeline), no fan-out", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(contactDetailResponse())
      .mockResolvedValueOnce(timelineResponse());

    await ContactDrawerContainer({ contactId: "contact-1", before: undefined, basePath: "/contacts" });

    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it("forwards `before` to the timeline fetch when present in the URL", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(contactDetailResponse())
      .mockResolvedValueOnce(timelineResponse());

    await ContactDrawerContainer({
      contactId: "contact-1",
      before: "2026-06-01T00:00:00Z",
      basePath: "/contacts",
    });

    const timelineUrl = fetchSpy.mock.calls[1][0] as string;
    expect(timelineUrl).toMatch(/before=2026-06-01T00%3A00%3A00Z/);
  });

  it("never issues a request to the /admin/tenants/{id}/... family (D7)", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(contactDetailResponse())
      .mockResolvedValueOnce(timelineResponse());

    await ContactDrawerContainer({ contactId: "contact-1", before: undefined, basePath: "/contacts" });

    for (const call of fetchSpy.mock.calls) {
      expect(call[0] as string).not.toMatch(/\/admin\/tenants\//);
    }
  });
});
