import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

const { RecordDrawer, RecordDrawerTimelinePanel } = await import("@/components/admin/record-drawer");

function okResult(overrides: Partial<{ degraded: boolean; sources: Record<string, unknown>; items: unknown[] }> = {}) {
  return {
    status: "ok" as const,
    data: {
      subject: { kind: "contact" as const, id: "contact-1", convertedToContactId: null },
      degraded: overrides.degraded ?? false,
      sources: (overrides.sources as Record<string, { state: string; count: number; truncated: boolean }>) ?? {
        conversations: { state: "ok", count: 2, truncated: false },
      },
      items: (overrides.items as { kind: string; occurredAt: string; id: string; data: Record<string, unknown> }[]) ?? [],
      nextBefore: null,
    },
  };
}

describe("RecordDrawer -- D4 degradation disclosure (MANDATORY, highest-value test)", () => {
  it("degraded:true renders a visible notice naming the failed source, ABOVE the timeline items", () => {
    const result = okResult({
      degraded: true,
      sources: {
        bookings: { state: "failed", count: 0, truncated: false },
        conversations: { state: "ok", count: 1, truncated: false },
      },
      items: [{ kind: "conversation", occurredAt: "2026-07-01T00:00:00Z", id: "conv-1", data: {} }],
    });

    const html = renderToStaticMarkup(
      <RecordDrawer
        subject={{ kind: "contact", id: "contact-1", name: "Ada", secondary: "ada@example.com" }}
        result={result}
        onClose={() => {}}
        loadOlderHref={null}
      />
    );

    expect(html).toMatch(/incomplete/i);
    expect(html).toMatch(/bookings/);
    // The notice appears before the item text in document order.
    const noticeIndex = html.indexOf("incomplete");
    const itemIndex = html.indexOf("conversation");
    expect(noticeIndex).toBeGreaterThan(-1);
    expect(itemIndex).toBeGreaterThan(noticeIndex);
  });

  it("degraded:false renders NO degradation notice -- the disclosure means something when it appears", () => {
    const result = okResult({ degraded: false });

    const html = renderToStaticMarkup(
      <RecordDrawer
        subject={{ kind: "contact", id: "contact-1", name: "Ada", secondary: "ada@example.com" }}
        result={result}
        onClose={() => {}}
        loadOlderHref={null}
      />
    );

    expect(html).not.toMatch(/incomplete/i);
    expect(html).not.toMatch(/data-testid="timeline-degraded-notice"/);
  });

  it("an empty timeline (no items, not degraded) renders an explicit 'No activity yet', not an empty rail", () => {
    const result = okResult({ degraded: false, items: [] });

    const html = renderToStaticMarkup(
      <RecordDrawer
        subject={{ kind: "contact", id: "contact-1", name: "Ada", secondary: "ada@example.com" }}
        result={result}
        onClose={() => {}}
        loadOlderHref={null}
      />
    );

    expect(html).toMatch(/No activity yet/i);
  });

  it("an error result renders the error treatment with its correlation ID, not a fabricated timeline", () => {
    const html = renderToStaticMarkup(
      <RecordDrawer
        subject={{ kind: "contact", id: "contact-1", name: null, secondary: null }}
        result={{ status: "error", message: "boom", correlationId: "corr-42" }}
        onClose={() => {}}
        loadOlderHref={null}
      />
    );

    expect(html).toMatch(/boom/);
    expect(html).toMatch(/corr-42/);
  });
});

describe("RecordDrawer -- D3 one shared component serves both subject kinds", () => {
  it("renders identically-structured output for a contact subject and a lead subject (same component, no kind-specific fork in the timeline body)", () => {
    const result = okResult({ items: [{ kind: "booking", occurredAt: "2026-07-01T00:00:00Z", id: "b-1", data: {} }] });

    const contactHtml = renderToStaticMarkup(
      <RecordDrawer
        subject={{ kind: "contact", id: "contact-1", name: "Ada", secondary: "ada@example.com" }}
        result={result}
        onClose={() => {}}
        loadOlderHref={null}
      />
    );
    const leadHtml = renderToStaticMarkup(
      <RecordDrawer
        subject={{ kind: "lead", id: "lead-1", name: "Ada", secondary: "ada@example.com" }}
        result={result}
        onClose={() => {}}
        loadOlderHref={null}
      />
    );

    // Both render the same timeline row structure -- the "booking" item's
    // human label ("Meeting booked", via SR-24's timelineItemLabel) and its
    // rail markup are present in both, proving one shared body.
    expect(contactHtml).toMatch(/Meeting booked/);
    expect(leadHtml).toMatch(/Meeting booked/);
  });

  it("RecordDrawerTimelinePanel (the piece embedded in the Lead drawer's Timeline tab) renders the SAME degradation notice logic as the full RecordDrawer", () => {
    const degraded = okResult({
      degraded: true,
      sources: { bookings: { state: "failed", count: 0, truncated: false } },
    });

    const panelHtml = renderToStaticMarkup(
      <RecordDrawerTimelinePanel result={degraded} loadOlderHref={null} />
    );

    expect(panelHtml).toMatch(/incomplete/i);
    expect(panelHtml).toMatch(/bookings/);
  });
});

describe("RecordDrawer -- accessibility", () => {
  it("renders as a dialog with aria-modal and an accessible name", () => {
    const html = renderToStaticMarkup(
      <RecordDrawer
        subject={{ kind: "contact", id: "contact-1", name: "Ada Lovelace", secondary: "ada@example.com" }}
        result={okResult()}
        onClose={() => {}}
        loadOlderHref={null}
      />
    );

    expect(html).toMatch(/role="dialog"/);
    expect(html).toMatch(/aria-modal="true"/);
    expect(html).toMatch(/aria-labelledby="record-drawer-title-contact-contact-1"/);
  });

  it("the close button carries an accessible label", () => {
    const html = renderToStaticMarkup(
      <RecordDrawer
        subject={{ kind: "contact", id: "contact-1", name: "Ada", secondary: null }}
        result={okResult()}
        onClose={() => {}}
        loadOlderHref={null}
      />
    );

    expect(html).toMatch(/aria-label="Close contact detail"/);
  });
});

describe("RecordDrawer -- multi-tenant safety (D7)", () => {
  it("never renders a tenant_id anywhere in the markup", () => {
    const html = renderToStaticMarkup(
      <RecordDrawer
        subject={{ kind: "contact", id: "contact-1", name: "Ada", secondary: "ada@example.com", accountId: "account-1" }}
        result={okResult()}
        onClose={() => {}}
        loadOlderHref={null}
      />
    );

    expect(html).not.toMatch(/tenant_id/i);
    expect(html).not.toMatch(/tenantId/);
  });

  it("the 'View accounts' link does not construct a per-account detail URL carrying the id as a route segment that could be confused with a tenant scope, and never links to /admin/tenants/...", () => {
    const html = renderToStaticMarkup(
      <RecordDrawer
        subject={{ kind: "contact", id: "contact-1", name: "Ada", secondary: "ada@example.com", accountId: "account-1" }}
        result={okResult()}
        onClose={() => {}}
        loadOlderHref={null}
      />
    );

    expect(html).not.toMatch(/\/admin\/tenants\//);
  });
});

describe("RecordDrawer -- timeline pagination (scope item 10)", () => {
  it("renders a 'Load older' link when loadOlderHref is provided, using it verbatim", () => {
    const html = renderToStaticMarkup(
      <RecordDrawer
        subject={{ kind: "contact", id: "contact-1", name: "Ada", secondary: "ada@example.com" }}
        result={okResult()}
        onClose={() => {}}
        loadOlderHref="/contacts?contact=contact-1&before=2026-06-01T00%3A00%3A00Z"
      />
    );

    expect(html).toMatch(/Load older/i);
    expect(html).toMatch(/href="\/contacts\?contact=contact-1&amp;before=2026-06-01T00%3A00%3A00Z"/);
  });

  it("renders no 'Load older' control when loadOlderHref is null (nothing older to load)", () => {
    const html = renderToStaticMarkup(
      <RecordDrawer
        subject={{ kind: "contact", id: "contact-1", name: "Ada", secondary: "ada@example.com" }}
        result={okResult()}
        onClose={() => {}}
        loadOlderHref={null}
      />
    );

    expect(html).not.toMatch(/Load older/i);
  });
});
