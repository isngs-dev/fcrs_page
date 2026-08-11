import { afterEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: getMock })),
}));

const {
  buildNotificationsQuery,
  listNotifications,
  notificationTargetHref,
  NOTIFICATIONS_PAGE_SIZE,
} = await import("@/lib/notifications");

describe("NOTIFICATIONS_PAGE_SIZE", () => {
  it("is well within the backend's [1,200] clamp (D7)", () => {
    expect(NOTIFICATIONS_PAGE_SIZE).toBeGreaterThanOrEqual(1);
    expect(NOTIFICATIONS_PAGE_SIZE).toBeLessThanOrEqual(200);
  });
});

describe("buildNotificationsQuery", () => {
  it("page=1, no category -> limit=NOTIFICATIONS_PAGE_SIZE&offset=0, no category param", () => {
    const params = new URLSearchParams(buildNotificationsQuery({ page: 1 }));
    expect(params.get("limit")).toBe(String(NOTIFICATIONS_PAGE_SIZE));
    expect(params.get("offset")).toBe("0");
    expect(params.has("category")).toBe(false);
  });

  it("page=3 -> offset advances by NOTIFICATIONS_PAGE_SIZE", () => {
    const params = new URLSearchParams(buildNotificationsQuery({ page: 3 }));
    expect(params.get("offset")).toBe(String(NOTIFICATIONS_PAGE_SIZE * 2));
  });

  it("page=0 or negative -> clamped to offset=0 (page 1)", () => {
    expect(new URLSearchParams(buildNotificationsQuery({ page: 0 })).get("offset")).toBe("0");
    expect(new URLSearchParams(buildNotificationsQuery({ page: -5 })).get("offset")).toBe("0");
  });

  it("a non-finite page -> clamped to page 1", () => {
    expect(new URLSearchParams(buildNotificationsQuery({ page: NaN })).get("offset")).toBe("0");
  });

  it("category=leads is passed through", () => {
    const params = new URLSearchParams(buildNotificationsQuery({ page: 1, category: "leads" }));
    expect(params.get("category")).toBe("leads");
  });

  it("category=system is passed through", () => {
    const params = new URLSearchParams(buildNotificationsQuery({ page: 1, category: "system" }));
    expect(params.get("category")).toBe("system");
  });

  it("unreadOnly=true sends unread_only=true", () => {
    const params = new URLSearchParams(buildNotificationsQuery({ page: 1, unreadOnly: true }));
    expect(params.get("unread_only")).toBe("true");
  });

  it("never carries a tenant_id", () => {
    const qs = buildNotificationsQuery({ page: 1 });
    expect(qs).not.toMatch(/tenant/i);
  });

  it("the TypeScript type has no 'mentions' member (D4, compile-time proof)", () => {
    // @ts-expect-error -- "mentions" must not be assignable to NotificationCategory.
    const invalid: import("@/lib/notifications").NotificationCategory = "mentions";
    expect(invalid).toBe("mentions"); // unreachable if the type check above ever regresses
  });
});

describe("listNotifications", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("maps a 200 envelope field-by-field, snake->camel, no tenant_id", async () => {
    getMock.mockReturnValue({ value: "jwt-value" });
    const body = {
      items: [
        {
          event_id: "event-1",
          kind: "lead_captured",
          category: "leads",
          target_type: "lead",
          target_id: "lead-1",
          payload: { lead_id: "lead-1" },
          actor_id: null,
          created_at: "2026-08-05T00:00:00Z",
          read: false,
        },
      ],
      total: 1,
      unread_count: 1,
      limit: 50,
      offset: 0,
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(body), { status: 200 }));

    const result = await listNotifications({ page: 1 });

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.total).toBe(1);
      expect(result.unreadCount).toBe(1);
      expect(result.items[0]).toEqual({
        eventId: "event-1",
        kind: "lead_captured",
        category: "leads",
        targetType: "lead",
        targetId: "lead-1",
        payload: { lead_id: "lead-1" },
        actorId: null,
        createdAt: "2026-08-05T00:00:00Z",
        read: false,
      });
      expect(result.items[0]).not.toHaveProperty("tenant_id");
      expect(result.items[0]).not.toHaveProperty("tenantId");
    }
  });

  it("targets the implicit /admin/notifications path (never a tenant-explicit route)", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(
          JSON.stringify({ items: [], total: 0, unread_count: 0, limit: 50, offset: 0 }),
          { status: 200 }
        )
      );

    await listNotifications({ page: 1 });

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe(
      `http://localhost:8000/admin/notifications?limit=${NOTIFICATIONS_PAGE_SIZE}&offset=0`
    );
    expect(url).not.toMatch(/tenant/i);
  });

  it("category=leads is forwarded on the wire", async () => {
    getMock.mockReturnValue(undefined);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(
          JSON.stringify({ items: [], total: 0, unread_count: 0, limit: 50, offset: 0 }),
          { status: 200 }
        )
      );

    await listNotifications({ page: 1, category: "leads" });

    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toContain("category=leads");
  });

  it("maps a 403 ROLE_NOT_PERMITTED to a friendly permission message", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ error_code: "ROLE_NOT_PERMITTED", message: "nope", correlation_id: "corr-1" }),
        { status: 403 }
      )
    );

    const result = await listNotifications({ page: 1 });
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

    const result = await listNotifications({ page: 1 });
    expect(result.status).toBe("error");
    if (result.status === "error") expect(result.message).toMatch(/session/i);
  });

  it("maps a non-AdminApiError network throw to a generic network message, with correlation ID surfaced as empty", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("network down"));

    const result = await listNotifications({ page: 1 });
    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/unable to reach/i);
      expect(result.correlationId).toBe("");
    }
  });

  it("a tenant with no events returns 200 with an empty items array (honest empty state)", async () => {
    getMock.mockReturnValue(undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ items: [], total: 0, unread_count: 0, limit: 50, offset: 0 }),
        { status: 200 }
      )
    );

    const result = await listNotifications({ page: 1 });
    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.items).toEqual([]);
      expect(result.total).toBe(0);
      expect(result.unreadCount).toBe(0);
    }
  });

  it("never logs the response body (PII-minimal)", async () => {
    getMock.mockReturnValue(undefined);
    const consoleSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [
            {
              event_id: "event-1",
              kind: "lead_captured",
              category: "leads",
              target_type: "lead",
              target_id: "lead-1",
              payload: { lead_id: "lead-1" },
              actor_id: null,
              created_at: "2026-08-05T00:00:00Z",
              read: false,
            },
          ],
          total: 1,
          unread_count: 1,
          limit: 50,
          offset: 0,
        }),
        { status: 200 }
      )
    );

    await listNotifications({ page: 1 });

    expect(consoleSpy).not.toHaveBeenCalled();
    expect(errorSpy).not.toHaveBeenCalled();
  });
});

describe("notificationTargetHref", () => {
  const base = {
    eventId: "e1",
    kind: "lead_captured",
    category: "leads" as const,
    payload: null,
    actorId: null,
    createdAt: "2026-08-05T00:00:00Z",
    read: false,
  };

  it("a lead target links to /leads?lead=<id>", () => {
    const href = notificationTargetHref({ ...base, targetType: "lead", targetId: "lead-1" });
    expect(href).toBe("/leads?lead=lead-1");
  });

  it("a contact target links to /contacts?contact=<id>", () => {
    const href = notificationTargetHref({ ...base, targetType: "contact", targetId: "contact-1" });
    expect(href).toBe("/contacts?contact=contact-1");
  });

  it("a conversation target links to /conversations?conversation=<id>", () => {
    const href = notificationTargetHref({
      ...base,
      targetType: "conversation",
      targetId: "conv-1",
    });
    expect(href).toBe("/conversations?conversation=conv-1");
  });

  it("an ingestion_run target links to /knowledge", () => {
    const href = notificationTargetHref({
      ...base,
      targetType: "ingestion_run",
      targetId: "run-1",
    });
    expect(href).toBe("/knowledge");
  });

  it("a missing target_id returns null (no broken link)", () => {
    const href = notificationTargetHref({ ...base, targetType: "lead", targetId: null });
    expect(href).toBeNull();
  });

  it("an unknown target_type returns null", () => {
    const href = notificationTargetHref({ ...base, targetType: "something_new", targetId: "x" });
    expect(href).toBeNull();
  });
});
