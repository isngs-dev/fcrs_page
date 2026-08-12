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

const { saveAvailability } = await import(
  "@/app/(protected)/settings/availability-actions"
);
const { AdminApiError } = await import("@/lib/api");

function jsonResponse(body: Record<string, unknown>, status: number): Response {
  return new Response(JSON.stringify(body), { status });
}

function buildFormData(overrides: Partial<Record<string, string>> = {}): FormData {
  const values: Record<string, string> = {
    timezone: "UTC",
    slotMinutes: "30",
    bufferMinutes: "0",
    weeklyHoursJson: JSON.stringify({
      mon: ["09:00", "18:00"],
      tue: ["09:00", "18:00"],
      wed: ["09:00", "18:00"],
      thu: ["09:00", "18:00"],
      fri: ["09:00", "18:00"],
    }),
    ...overrides,
  };
  const fd = new FormData();
  for (const [key, value] of Object.entries(values)) {
    fd.set(key, value);
  }
  return fd;
}

describe("saveAvailability", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    adminApiFetchMock.mockReset();
    revalidatePathMock.mockReset();
  });

  it("calls PUT /admin/schedule/availability with the validated timezone/rules shape", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          timezone: "UTC",
          rules: {
            slot_minutes: 30,
            buffer_minutes: 0,
            weekly_hours: {
              mon: [["09:00", "18:00"]],
              tue: [["09:00", "18:00"]],
              wed: [["09:00", "18:00"]],
              thu: [["09:00", "18:00"]],
              fri: [["09:00", "18:00"]],
            },
          },
          updated_at: "2026-08-12T00:00:00Z",
        },
        200
      )
    );

    const state = await saveAvailability({ status: "idle" }, buildFormData());

    expect(state.status).toBe("saved");
    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/admin/schedule/availability",
      expect.objectContaining({ method: "PUT" })
    );
    const body = JSON.parse(adminApiFetchMock.mock.calls[0][1].body);
    expect(body).toEqual({
      timezone: "UTC",
      rules: {
        slot_minutes: 30,
        buffer_minutes: 0,
        weekly_hours: {
          mon: [["09:00", "18:00"]],
          tue: [["09:00", "18:00"]],
          wed: [["09:00", "18:00"]],
          thu: [["09:00", "18:00"]],
          fri: [["09:00", "18:00"]],
        },
      },
    });
    expect(revalidatePathMock).toHaveBeenCalledWith("/settings");
  });

  it("rejects an all-closed week without calling adminApiFetch", async () => {
    const state = await saveAvailability(
      { status: "idle" },
      buildFormData({ weeklyHoursJson: "{}" })
    );

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.fieldErrors.weeklyHoursJson).toMatch(/at least one day/i);
    }
    expect(adminApiFetchMock).not.toHaveBeenCalled();
  });

  it("rejects a zero slot length client-side without calling adminApiFetch", async () => {
    const state = await saveAvailability(
      { status: "idle" },
      buildFormData({ slotMinutes: "0" })
    );

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.fieldErrors.slotMinutes).toBeTruthy();
    }
    expect(adminApiFetchMock).not.toHaveBeenCalled();
  });

  it("maps a 403 to a permission-denied form error", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(403, {
        error_code: "ROLE_NOT_PERMITTED",
        message: "Forbidden.",
        correlation_id: "corr-1",
      })
    );

    const state = await saveAvailability({ status: "idle" }, buildFormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.formError).toMatch(/permission/i);
    }
  });

  it("returns a network-failure message when adminApiFetch throws a non-AdminApiError", async () => {
    adminApiFetchMock.mockRejectedValue(new TypeError("fetch failed"));

    const state = await saveAvailability({ status: "idle" }, buildFormData());

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.formError).toMatch(/unable to reach the server/i);
    }
  });
});
