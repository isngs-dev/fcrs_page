import { afterEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();
vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

const { LeadDrawerContainer } = await import("@/app/(protected)/leads/lead-drawer-container");

function leadDetailResponse() {
  return new Response(
    JSON.stringify({
      lead_id: "lead-1",
      name: "Ada",
      email: "ada@example.com",
      phone: null,
      status: "open",
      stage: "qualified",
      qualification_score: 80,
      assigned_agent_id: null,
      source: "widget",
    }),
    { status: 200 }
  );
}

function timelineResponse() {
  return new Response(
    JSON.stringify({
      subject: { kind: "lead", id: "lead-1", converted_to_contact_id: null },
      degraded: false,
      sources: {},
      items: [],
      next_before: null,
    }),
    { status: 200 }
  );
}

describe("LeadDrawerContainer -- timeline tab gating (SR-17)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("SR-24: fetches BOTH detail and timeline when tab is omitted -- Timeline is now the default tab", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(leadDetailResponse())
      .mockResolvedValueOnce(timelineResponse());

    await LeadDrawerContainer({ leadId: "lead-1", rawTab: undefined, basePath: "/leads" });

    expect(fetchSpy).toHaveBeenCalledTimes(2);
    const timelineUrl = fetchSpy.mock.calls[1][0] as string;
    expect(timelineUrl).toMatch(/\/admin\/leads\/lead-1\/timeline/);
  });

  it("does NOT fetch the timeline when tab is 'details' -- only detail is fetched", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(leadDetailResponse());

    await LeadDrawerContainer({ leadId: "lead-1", rawTab: "details", basePath: "/leads" });

    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("fetches the timeline when tab is 'timeline'", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(leadDetailResponse())
      .mockResolvedValueOnce(timelineResponse());

    await LeadDrawerContainer({ leadId: "lead-1", rawTab: "timeline", basePath: "/leads" });

    expect(fetchSpy).toHaveBeenCalledTimes(2);
    const timelineUrl = fetchSpy.mock.calls[1][0] as string;
    expect(timelineUrl).toMatch(/\/admin\/leads\/lead-1\/timeline/);
  });

  it("forwards `before` to the timeline fetch", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(leadDetailResponse())
      .mockResolvedValueOnce(timelineResponse());

    await LeadDrawerContainer({
      leadId: "lead-1",
      rawTab: "timeline",
      basePath: "/leads",
      before: "2026-06-01T00:00:00Z",
    });

    const timelineUrl = fetchSpy.mock.calls[1][0] as string;
    expect(timelineUrl).toMatch(/before=2026-06-01T00%3A00%3A00Z/);
  });
});
