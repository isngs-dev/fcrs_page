import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import jwt from "jsonwebtoken";

const getMock = vi.fn();
const redirectMock = vi.fn((url: string) => {
  throw new Error(`REDIRECT:${url}`);
});

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

vi.mock("next/navigation", () => ({
  redirect: redirectMock,
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

const LeadsPage = (await import("@/app/(protected)/leads/page")).default;

const SECRET = process.env.JWT_SECRET as string;

function signToken(role: string): string {
  return jwt.sign({ sub: "user-1", role, tenant_id: "tenant-1", project_ids: [] }, SECRET, {
    algorithm: "HS256",
    expiresIn: "1h",
  });
}

function leadListResponse(items: unknown[] = []) {
  return new Response(JSON.stringify({ items, total: items.length, limit: 200, offset: 0 }), { status: 200 });
}

function sampleLead(overrides: Record<string, unknown> = {}) {
  return {
    lead_id: "lead-1",
    name: "Ada Lovelace",
    email: "ada@example.com",
    phone: null,
    status: "new",
    stage: "captured",
    qualification_score: 30,
    assigned_agent_id: null,
    source: "widget",
    created_at: "2026-07-15T00:00:00Z",
    ...overrides,
  };
}

describe("LeadsPage board view (SR-18 M6 -- replaces the SR-15 placeholder)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
    redirectMock.mockClear();
  });

  it("?view=board no longer renders the SR-15 'coming soon' placeholder text", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(leadListResponse([sampleLead()]));

    const element = await LeadsPage({ searchParams: Promise.resolve({ view: "board" }) });
    const html = renderToStaticMarkup(element);

    expect(html).not.toMatch(/coming soon/i);
    expect(html).not.toMatch(/needs its own careful pass/i);
  });

  it("?view=board renders the funnel's columns (Captured, Qualified, Contacted, Converted, Disqualified)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(leadListResponse([sampleLead()]));

    const element = await LeadsPage({ searchParams: Promise.resolve({ view: "board" }) });
    const html = renderToStaticMarkup(element);

    for (const label of ["Captured", "Qualified", "Contacted", "Converted", "Disqualified"]) {
      expect(html).toMatch(new RegExp(label));
    }
  });

  it("?view=board fetches unfiltered at limit=200, ignoring any stray ?stage=/?page=", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(leadListResponse([]));

    await LeadsPage({ searchParams: Promise.resolve({ view: "board", stage: "qualified", page: "3" }) });

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toMatch(/limit=200&offset=0/);
    expect(url).not.toMatch(/stage=/);
  });

  it("both CLIENT_ADMIN and CLIENT_AGENT reach the board (M1/M10 -- agents can transition lead stages too)", async () => {
    for (const role of ["CLIENT_ADMIN", "CLIENT_AGENT"]) {
      getMock.mockReturnValue({ value: signToken(role) });
      vi.spyOn(globalThis, "fetch").mockResolvedValue(leadListResponse([sampleLead()]));

      const element = await LeadsPage({ searchParams: Promise.resolve({ view: "board" }) });
      expect(redirectMock).not.toHaveBeenCalled();
      expect(element).toBeTruthy();
      vi.restoreAllMocks();
    }
  });

  it("an empty leads set on the board renders an explicit empty state", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(leadListResponse([]));

    const element = await LeadsPage({ searchParams: Promise.resolve({ view: "board" }) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/No leads yet/i);
  });

  it("a failing board fetch renders the error treatment with its correlation ID", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error_code: "SOMETHING", message: "boom", correlation_id: "corr-42" }), {
        status: 500,
      })
    );

    const element = await LeadsPage({ searchParams: Promise.resolve({ view: "board" }) });
    const html = renderToStaticMarkup(element);

    expect(html).toMatch(/corr-42/);
  });

  it("a converted lead card renders aria-disabled (terminal, not draggable)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      leadListResponse([sampleLead({ lead_id: "lead-converted", stage: "converted" })])
    );

    const element = await LeadsPage({ searchParams: Promise.resolve({ view: "board" }) });
    const html = renderToStaticMarkup(element);

    const idx = html.indexOf('data-testid="pipeline-card-lead-converted"');
    const tagStart = html.lastIndexOf("<button", idx);
    const tagEnd = html.indexOf(">", idx);
    const tag = html.slice(tagStart, tagEnd + 1);
    expect(tag).toMatch(/aria-disabled="true"/);
  });

  it("never sends a tenant_id anywhere in the outgoing fetch URL (SR-17 D7)", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(leadListResponse([]));

    await LeadsPage({ searchParams: Promise.resolve({ view: "board" }) });

    for (const call of fetchSpy.mock.calls) {
      const url = call[0] as string;
      expect(url).not.toMatch(/\/admin\/tenants\//);
      expect(url).not.toMatch(/tenant_id/);
    }
  });

  it("Table view is untouched: still renders the leads table, not the board, when ?view is absent", async () => {
    getMock.mockReturnValue({ value: signToken("CLIENT_ADMIN") });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(leadListResponse([sampleLead()]));

    const element = await LeadsPage({ searchParams: Promise.resolve({}) });
    const html = renderToStaticMarkup(element);

    // The table renders column headers the board does not.
    expect(html).toMatch(/>Assigned</);
  });
});
