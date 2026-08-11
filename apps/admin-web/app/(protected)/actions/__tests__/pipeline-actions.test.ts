import { afterEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();
const revalidatePathMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

vi.mock("next/cache", () => ({
  revalidatePath: revalidatePathMock,
}));

const { transitionLeadStage, transitionDealStage, convertLeadToContact } = await import(
  "@/app/(protected)/actions/pipeline-actions"
);

describe("transitionLeadStage -- M1: never sends client-computed status/qualification_score", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
    revalidatePathMock.mockReset();
  });

  it("sends PATCH /admin/leads/{id} with ONLY {stage} in the body", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ lead_id: "lead-1", stage: "qualified" }), { status: 200 })
    );

    await transitionLeadStage("lead-1", "qualified", "/leads");

    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/admin/leads/lead-1");
    expect(init.method).toBe("PATCH");
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({ stage: "qualified" });
    expect(body).not.toHaveProperty("status");
    expect(body).not.toHaveProperty("qualification_score");
    expect(body).not.toHaveProperty("qualificationScore");
  });

  it("returns {status:'ok', stage} from the server's real response on success", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ lead_id: "lead-1", stage: "qualified" }), { status: 200 })
    );

    const result = await transitionLeadStage("lead-1", "qualified", "/leads");
    expect(result).toEqual({ status: "ok", stage: "qualified" });
  });

  it("revalidates the given path on success", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ lead_id: "lead-1", stage: "qualified" }), { status: 200 })
    );

    await transitionLeadStage("lead-1", "qualified", "/leads");
    expect(revalidatePathMock).toHaveBeenCalledWith("/leads");
  });

  it("maps a 422 INVALID_STAGE_TRANSITION to an honest error result carrying the server's message + correlation ID -- no silent swallow", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          error_code: "INVALID_STAGE_TRANSITION",
          message: "Illegal stage transition from 'captured' to 'contacted'.",
          correlation_id: "corr-abc",
        }),
        { status: 422 }
      )
    );

    const result = await transitionLeadStage("lead-1", "contacted", "/leads");
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.errorCode).toBe("INVALID_STAGE_TRANSITION");
      expect(result.correlationId).toBe("corr-abc");
      expect(result.message).toMatch(/captured.*contacted/);
    }
  });

  it("does NOT call revalidatePath on a rejected transition", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "INVALID_STAGE_TRANSITION", message: "no", correlation_id: "c" }),
        { status: 422 }
      )
    );

    await transitionLeadStage("lead-1", "contacted", "/leads");
    expect(revalidatePathMock).not.toHaveBeenCalled();
  });

  it("maps a network throw to a generic network error result (never throws out of the action)", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("down"));

    const result = await transitionLeadStage("lead-1", "qualified", "/leads");
    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.errorCode).toBe("NETWORK_ERROR");
  });
});

describe("transitionDealStage -- M3/D4/D9: close_reason enforcement, honest response mapping", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
    revalidatePathMock.mockReset();
  });

  it("sends POST /admin/opportunities/{id}/stage with {stage} for a non-off-ramp transition", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ opportunity_id: "opp-1", stage: "qualification" }), { status: 200 })
    );

    await transitionDealStage("opp-1", "qualification", undefined, "/deals");

    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/admin/opportunities/opp-1/stage");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body as string);
    expect(body.stage).toBe("qualification");
    expect(body.close_reason).toBeNull();
  });

  it("client-side belt: closed_lost with NO reason is rejected WITHOUT a network call (D4)", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch");

    const result = await transitionDealStage("opp-1", "closed_lost", undefined, "/deals");

    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.errorCode).toBe("CLOSE_REASON_REQUIRED");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("client-side belt: closed_lost with a WHITESPACE-ONLY reason is rejected without a network call", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch");

    const result = await transitionDealStage("opp-1", "closed_lost", "   ", "/deals");

    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.errorCode).toBe("CLOSE_REASON_REQUIRED");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("closed_lost with a real reason sends close_reason trimmed", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ opportunity_id: "opp-1", stage: "closed_lost" }), { status: 200 })
    );

    await transitionDealStage("opp-1", "closed_lost", "  Budget cut  ", "/deals");

    const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.stage).toBe("closed_lost");
    expect(body.close_reason).toBe("Budget cut");
  });

  it("suspenders: a server-side 422 CLOSE_REASON_REQUIRED (bypassed client check) is still surfaced honestly", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          error_code: "CLOSE_REASON_REQUIRED",
          message: "close_reason is required when transitioning to closed_lost.",
          correlation_id: "corr-xyz",
        }),
        { status: 422 }
      )
    );

    // Simulated bypass: this test forges past the client belt by mutating
    // the exported schema indirectly is not possible, so instead we assert
    // the server-error MAPPING path directly by making the server reject a
    // transition the client believed was fine (a legal-looking non-empty
    // reason the server still rejects for its own reasons) -- the important
    // assertion is that a CLOSE_REASON_REQUIRED from the SERVER is mapped
    // to the same honest, non-swallowed message as any other 422.
    const result = await transitionDealStage("opp-1", "closed_lost", "reason", "/deals");
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.errorCode).toBe("CLOSE_REASON_REQUIRED");
      expect(result.correlationId).toBe("corr-xyz");
      expect(result.message).toMatch(/reason is required/i);
    }
  });

  it("maps a 422 INVALID_OPPORTUNITY_STAGE_TRANSITION to an honest error result", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          error_code: "INVALID_OPPORTUNITY_STAGE_TRANSITION",
          message: "Illegal opportunity stage transition from 'prospecting' to 'proposal'.",
          correlation_id: "corr-def",
        }),
        { status: 422 }
      )
    );

    const result = await transitionDealStage("opp-1", "proposal", undefined, "/deals");
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.errorCode).toBe("INVALID_OPPORTUNITY_STAGE_TRANSITION");
      expect(result.correlationId).toBe("corr-def");
    }
  });

  it("revalidates the given path on success", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ opportunity_id: "opp-1", stage: "qualification" }), { status: 200 })
    );

    await transitionDealStage("opp-1", "qualification", undefined, "/deals");
    expect(revalidatePathMock).toHaveBeenCalledWith("/deals");
  });

  it("never calls PUT /admin/opportunities/config (M10 -- agents get 403 there, this action never touches it)", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ opportunity_id: "opp-1", stage: "qualification" }), { status: 200 })
    );

    await transitionDealStage("opp-1", "qualification", undefined, "/deals");

    for (const call of fetchSpy.mock.calls) {
      const url = call[0] as string;
      expect(url).not.toMatch(/\/config/);
    }
  });
});

describe("convertLeadToContact -- SR-18 follow-up: real convert endpoint, not the bare stage PATCH", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
    revalidatePathMock.mockReset();
  });

  it("sends POST /admin/leads/{id}/convert, NOT PATCH /admin/leads/{id}", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ contact_id: "contact-1", lead_id: "lead-1" }), { status: 201 })
    );

    await convertLeadToContact("lead-1", "/leads");

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/admin/leads/lead-1/convert");
    expect(init.method).toBe("POST");
  });

  it("returns {status:'ok', stage:'converted'} on success -- the board only needs the new stage, never the contact_id", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ contact_id: "contact-1", lead_id: "lead-1" }), { status: 201 })
    );

    const result = await convertLeadToContact("lead-1", "/leads");
    expect(result).toEqual({ status: "ok", stage: "converted" });
  });

  it("revalidates the given path on success", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ contact_id: "contact-1", lead_id: "lead-1" }), { status: 201 })
    );

    await convertLeadToContact("lead-1", "/leads");
    expect(revalidatePathMock).toHaveBeenCalledWith("/leads");
  });

  it("maps a 409 LEAD_ALREADY_CONVERTED to an honest error result -- no silent swallow", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          error_code: "LEAD_ALREADY_CONVERTED",
          message: "This lead has already been converted to a contact.",
          contact_id: "contact-1",
        }),
        { status: 409 }
      )
    );

    const result = await convertLeadToContact("lead-1", "/leads");
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.errorCode).toBe("LEAD_ALREADY_CONVERTED");
      expect(result.message).toMatch(/already been converted/);
    }
  });

  it("maps a 403 (CLIENT_AGENT session somehow reaching the endpoint) to a permission error, never a silent success", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          error_code: "ROLE_NOT_PERMITTED",
          message: "This role cannot perform this action.",
          correlation_id: "corr-role",
        }),
        { status: 403 }
      )
    );

    const result = await convertLeadToContact("lead-1", "/leads");
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.errorCode).toBe("ROLE_NOT_PERMITTED");
      expect(result.message).toMatch(/permission/i);
    }
  });

  it("does NOT call revalidatePath on a rejected conversion", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "NOT_FOUND", message: "not found", correlation_id: "c" }),
        { status: 404 }
      )
    );

    await convertLeadToContact("lead-1", "/leads");
    expect(revalidatePathMock).not.toHaveBeenCalled();
  });

  it("maps a network throw to a generic network error result (never throws out of the action)", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("down"));

    const result = await convertLeadToContact("lead-1", "/leads");
    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.errorCode).toBe("NETWORK_ERROR");
  });
});

describe("RBAC (SR-18 M10): both transition actions carry no client-side role gate", () => {
  // Both `PATCH /admin/leads/{id}` and `POST /admin/opportunities/{id}/stage`
  // are open to CLIENT_ADMIN + CLIENT_AGENT server-side (verified live,
  // leads/admin_routes.py:535, opportunities/admin_routes.py:509). This
  // deliberately differs from SR-17's read-only contacts pattern
  // (`addContact` server action has no such symmetry -- CLIENT_ADMIN only).
  // These actions therefore must NOT hardcode a role check of their own --
  // the backend is the sole enforcement boundary (admin-web skill: "the API
  // is the real gate"), and the cookie forwarded by `adminApiFetch` is
  // whichever role's session made the request. This test asserts the
  // request succeeds identically regardless of which role's cookie is
  // present -- i.e. this file performs no client-side role branching that
  // could accidentally lock CLIENT_AGENT out to "be consistent" with
  // SR-17's stricter pattern.
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
    revalidatePathMock.mockReset();
  });

  it("transitionLeadStage succeeds identically for a CLIENT_ADMIN and a CLIENT_AGENT session cookie", async () => {
    for (const cookieValue of ["admin-jwt", "agent-jwt"]) {
      getMock.mockReturnValue({ value: cookieValue });
      vi.spyOn(globalThis, "fetch").mockResolvedValue(
        new Response(JSON.stringify({ lead_id: "lead-1", stage: "qualified" }), { status: 200 })
      );

      const result = await transitionLeadStage("lead-1", "qualified", "/leads");
      expect(result.status).toBe("ok");
      vi.restoreAllMocks();
    }
  });

  it("transitionDealStage succeeds identically for a CLIENT_ADMIN and a CLIENT_AGENT session cookie", async () => {
    for (const cookieValue of ["admin-jwt", "agent-jwt"]) {
      getMock.mockReturnValue({ value: cookieValue });
      vi.spyOn(globalThis, "fetch").mockResolvedValue(
        new Response(JSON.stringify({ opportunity_id: "opp-1", stage: "qualification" }), { status: 200 })
      );

      const result = await transitionDealStage("opp-1", "qualification", undefined, "/deals");
      expect(result.status).toBe("ok");
      vi.restoreAllMocks();
    }
  });
});
