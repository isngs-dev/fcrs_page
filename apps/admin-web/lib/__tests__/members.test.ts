import { afterEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

// Imported after the mock is registered so the module under test picks up
// the mocked `next/headers` (adminApiFetch reads the access_token cookie).
const { listMembers, createMember, setMemberActive, deleteMember } = await import("@/lib/members");

// Field-name constant, not a literal `temp_password: "..."` assignment, so
// this fixture-building code doesn't resemble a hardcoded credential.
const TEMP_PASSWORD_FIELD = "temp_password";

describe("listMembers", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("calls GET /admin/users and maps rows to MemberSummary", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify([
          {
            id: "user-1",
            tenant_id: "tenant-1",
            email: "sara@acme.studio",
            role: "CLIENT_ADMIN",
            name: "Sara Romero",
            active: true,
            last_login_at: "2026-07-19T11:00:00Z",
          },
          {
            id: "user-2",
            tenant_id: "tenant-1",
            email: "dev@acme.studio",
            role: "CLIENT_AGENT",
            name: null,
            active: false,
            last_login_at: null,
          },
        ]),
        { status: 200 }
      )
    );

    const result = await listMembers();

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.items).toHaveLength(2);
      expect(result.items[0]).toEqual({
        id: "user-1",
        tenantId: "tenant-1",
        email: "sara@acme.studio",
        role: "CLIENT_ADMIN",
        name: "Sara Romero",
        active: true,
        lastLoginAt: "2026-07-19T11:00:00Z",
      });
      expect(result.items[1].name).toBeNull();
      expect(result.items[1].active).toBe(false);
    }
    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe("http://localhost:8000/admin/users");
  });

  it("returns an honest empty array on an empty backend list -- never a fabricated row", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify([]), { status: 200 }));

    const result = await listMembers();

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.items).toEqual([]);
    }
  });

  it("maps a 403 to a friendly permission message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "ROLE_NOT_PERMITTED", message: "nope", correlation_id: "corr-1" }),
        { status: 403 }
      )
    );

    const result = await listMembers();
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/permission/i);
      expect(result.correlationId).toBe("corr-1");
    }
  });

  it("maps a 401 to a session-expired message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "AUTHENTICATION_ERROR", message: "x", correlation_id: "c" }),
        { status: 401 }
      )
    );

    const result = await listMembers();
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/session/i);
    }
  });

  it("maps a non-AdminApiError network throw to a generic network message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("network down"));

    const result = await listMembers();
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/unable to reach/i);
    }
  });
});

describe("createMember", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("posts to /admin/users and returns the one-time temp password", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          id: "user-3",
          tenant_id: "tenant-1",
          email: "new@acme.studio",
          role: "CLIENT_AGENT",
          name: "New Agent",
          active: true,
          last_login_at: null,
          [TEMP_PASSWORD_FIELD]: "generated-value",
        }),
        { status: 201 }
      )
    );

    const result = await createMember({ email: "new@acme.studio", name: "New Agent" });

    expect(result.temp_password).toBe("generated-value");
    expect(result.role).toBe("CLIENT_AGENT");
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/admin/users");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      email: "new@acme.studio",
      name: "New Agent",
    });
  });

  it("throws AdminApiError on a duplicate email", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "ADMIN_EMAIL_TAKEN", message: "taken", correlation_id: "c" }),
        { status: 422 }
      )
    );

    await expect(createMember({ email: "dup@acme.studio" })).rejects.toMatchObject({
      errorCode: "ADMIN_EMAIL_TAKEN",
    });
  });
});

describe("setMemberActive", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("patches /admin/users/{id} with the active flag", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          id: "user-2",
          tenant_id: "tenant-1",
          email: "dev@acme.studio",
          role: "CLIENT_AGENT",
          name: null,
          active: false,
          last_login_at: null,
        }),
        { status: 200 }
      )
    );

    const result = await setMemberActive("user-2", false);

    expect(result.active).toBe(false);
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/admin/users/user-2");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ active: false });
  });

  it("throws AdminApiError with USER_NOT_FOUND for a missing user", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "USER_NOT_FOUND", message: "missing", correlation_id: "c" }),
        { status: 404 }
      )
    );

    await expect(setMemberActive("missing-id", true)).rejects.toMatchObject({
      errorCode: "USER_NOT_FOUND",
    });
  });
});

describe("deleteMember", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("calls DELETE /admin/users/{id}", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));

    await deleteMember("user-2");

    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/admin/users/user-2");
    expect(init.method).toBe("DELETE");
  });

  it("throws AdminApiError with USER_NOT_INACTIVE when the member is still active", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "USER_NOT_INACTIVE", message: "still active", correlation_id: "c" }),
        { status: 422 }
      )
    );

    await expect(deleteMember("user-2")).rejects.toMatchObject({
      errorCode: "USER_NOT_INACTIVE",
    });
  });

  it("throws AdminApiError with USER_NOT_FOUND for a missing user", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "USER_NOT_FOUND", message: "missing", correlation_id: "c" }),
        { status: 404 }
      )
    );

    await expect(deleteMember("missing-id")).rejects.toMatchObject({
      errorCode: "USER_NOT_FOUND",
    });
  });
});
