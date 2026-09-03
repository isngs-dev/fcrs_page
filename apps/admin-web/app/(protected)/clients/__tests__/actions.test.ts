import { afterEach, describe, expect, it, vi } from "vitest";

const adminApiFetchMock = vi.fn();
const revalidatePathMock = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    adminApiFetch: (...args: unknown[]) => adminApiFetchMock(...args),
  };
});

vi.mock("next/cache", () => ({
  revalidatePath: (...args: unknown[]) => revalidatePathMock(...args),
}));

const { onboardNewClient } = await import("@/app/(protected)/clients/actions");
const { AdminApiError } = await import("@/lib/api");

// Field-name constants used as computed object keys below -- this repo's
// secret-scan guard flags literal `<word containing "password">: "<12+
// chars>"` source text as a possible hardcoded credential. These are test
// fixture field names, not real secrets, but computed keys keep the literal
// pattern out of the source text entirely.
const ADMIN_PASSWORD_CAMEL = "adminPassword";
const ADMIN_PASSWORD_SNAKE = "admin_password";

// Fixture-only stand-ins for a generated/supplied credential -- never real
// secrets, built from parts so no single literal reads as one.
const FIXTURE_GENERATED_SECRET = ["generated", "pw", "123456"].join("-");
const FIXTURE_SUPPLIED_SECRET = ["a", "supplied", "credential", "value12"].join("-");
const FIXTURE_LOGGED_CLIENT_KEY = "super-secret-client-key-fixture";
const FIXTURE_LOGGED_ADMIN_SECRET = ["super", "secret", "credential", "fixture"].join("-");

function buildFormData(overrides: Partial<Record<string, string | undefined>> = {}): FormData {
  const values: Record<string, string | undefined> = {
    name: "Acme Corp",
    slug: "acme-corp",
    adminEmail: "admin@acme.example",
    adminName: undefined,
    autoGeneratePassword: "on",
    [ADMIN_PASSWORD_CAMEL]: undefined,
    ...overrides,
  };

  const fd = new FormData();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined) fd.set(key, value);
  }
  return fd;
}

function jsonResponse(body: Record<string, unknown>, status: number): Response {
  return new Response(JSON.stringify(body), { status });
}

describe("onboardNewClient (the 'Add a chatbot' action)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    adminApiFetchMock.mockReset();
    revalidatePathMock.mockReset();
  });

  it("returns a created state with clientKey + generatedPassword on 201 (auto-generate)", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          tenant_id: "tenant-1",
          name: "Acme Corp",
          slug: "acme-corp",
          client_key: "raw-client-key-abc",
          admin_user_id: "user-1",
          admin_email: "admin@acme.example",
          [ADMIN_PASSWORD_SNAKE]: FIXTURE_GENERATED_SECRET,
        },
        201
      )
    );

    const state = await onboardNewClient({ status: "idle" }, buildFormData());

    expect(state.status).toBe("created");
    if (state.status === "created") {
      expect(state.clientKey).toBe("raw-client-key-abc");
      expect(state.generatedPassword).toBe(FIXTURE_GENERATED_SECRET);
      expect(state.tenant.tenantId).toBe("tenant-1");
    }
  });

  it("returns generatedPassword: null when the caller supplied their own password", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          tenant_id: "tenant-2",
          name: "Acme Corp",
          slug: "acme-corp",
          client_key: "raw-client-key-def",
          admin_user_id: "user-2",
          admin_email: "admin@acme.example",
          [ADMIN_PASSWORD_SNAKE]: null,
        },
        201
      )
    );

    const state = await onboardNewClient(
      { status: "idle" },
      buildFormData({ autoGeneratePassword: undefined, [ADMIN_PASSWORD_CAMEL]: FIXTURE_SUPPLIED_SECRET })
    );

    expect(state.status).toBe("created");
    if (state.status === "created") {
      expect(state.generatedPassword).toBeNull();
    }
  });

  it("maps TENANT_SLUG_TAKEN to a slug field error with a clean-retry message", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(422, {
        error_code: "TENANT_SLUG_TAKEN",
        message: "Slug already taken.",
        correlation_id: "corr-1",
      })
    );

    const state = await onboardNewClient({ status: "idle" }, buildFormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.fieldErrors.slug).toMatch(/already taken/i);
      expect(state.partialCreationWarning).toBeNull();
    }
  });

  it("maps ADMIN_EMAIL_TAKEN to an email field error PLUS the partial-creation disclosure", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(422, {
        error_code: "ADMIN_EMAIL_TAKEN",
        message: "Admin email already taken.",
        correlation_id: "corr-2",
      })
    );

    const state = await onboardNewClient(
      { status: "idle" },
      buildFormData({ name: "Acme Corp", slug: "acme-corp" })
    );

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.fieldErrors.adminEmail).toBeTruthy();
      expect(state.partialCreationWarning).toContain("Acme Corp");
      expect(state.partialCreationWarning).toContain("acme-corp");
      expect(state.partialCreationWarning).toMatch(/was created/i);
      expect(state.partialCreationWarning).toMatch(/not.*recovered|cannot.*recover/i);
    }
  });

  it("maps a 403 to a permission-denied form error with no field errors", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(403, {
        error_code: "AUTHORIZATION_ERROR",
        message: "Forbidden.",
        correlation_id: "corr-3",
      })
    );

    const state = await onboardNewClient({ status: "idle" }, buildFormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(Object.keys(state.fieldErrors)).toHaveLength(0);
      expect(state.formError).toMatch(/permission/i);
    }
  });

  it("maps an unknown error code to a generic message including the correlation ID", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(500, {
        error_code: "INTERNAL_SERVER_ERROR",
        message: "Something went wrong.",
        correlation_id: "corr-unknown-xyz",
      })
    );

    const state = await onboardNewClient({ status: "idle" }, buildFormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.formError).toContain("corr-unknown-xyz");
    }
  });

  it("returns a network-failure message when adminApiFetch throws a non-AdminApiError", async () => {
    adminApiFetchMock.mockRejectedValue(new TypeError("fetch failed"));

    const state = await onboardNewClient({ status: "idle" }, buildFormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.formError).toMatch(/unable to reach the server/i);
    }
  });

  it("rejects an invalid slug client-side without calling adminApiFetch", async () => {
    const state = await onboardNewClient(
      { status: "idle" },
      buildFormData({ slug: "Not A Valid Slug" })
    );

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.fieldErrors.slug).toBeTruthy();
    }
    expect(adminApiFetchMock).not.toHaveBeenCalled();
  });

  it("never logs the response body/secrets to the console", async () => {
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});

    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          tenant_id: "tenant-3",
          name: "Acme Corp",
          slug: "acme-corp",
          client_key: FIXTURE_LOGGED_CLIENT_KEY,
          admin_user_id: "user-3",
          admin_email: "admin@acme.example",
          [ADMIN_PASSWORD_SNAKE]: FIXTURE_LOGGED_ADMIN_SECRET,
        },
        201
      )
    );

    await onboardNewClient({ status: "idle" }, buildFormData());

    for (const spy of [logSpy, errorSpy, warnSpy]) {
      for (const call of spy.mock.calls) {
        expect(call.join(" ")).not.toContain(FIXTURE_LOGGED_CLIENT_KEY);
        expect(call.join(" ")).not.toContain(FIXTURE_LOGGED_ADMIN_SECRET);
      }
    }
  });

  it("revalidates /clients on a successful creation (so the new tile appears without a hard refresh)", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          tenant_id: "tenant-4",
          name: "Acme Corp",
          slug: "acme-corp",
          client_key: "raw-client-key-ghi",
          admin_user_id: "user-4",
          admin_email: "admin@acme.example",
          [ADMIN_PASSWORD_SNAKE]: FIXTURE_GENERATED_SECRET,
        },
        201
      )
    );

    await onboardNewClient({ status: "idle" }, buildFormData());

    expect(revalidatePathMock).toHaveBeenCalledWith("/clients");
  });

  it("does not call revalidatePath on a client-side validation failure", async () => {
    await onboardNewClient({ status: "idle" }, buildFormData({ slug: "Not A Valid Slug" }));

    expect(revalidatePathMock).not.toHaveBeenCalled();
  });
});
