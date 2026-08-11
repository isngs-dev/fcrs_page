import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

const pushMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

const { ContactDrawer } = await import("@/app/(protected)/contacts/contact-drawer");

describe("ContactDrawer -- D3 URL-driven state, contact-specific adapter", () => {
  it("renders the contact's name/email in the drawer header", () => {
    const html = renderToStaticMarkup(
      <ContactDrawer
        contactId="contact-1"
        detailResult={{
          status: "ok",
          contact: {
            contactId: "contact-1",
            accountId: null,
            leadId: null,
            name: "Ada Lovelace",
            email: "ada@example.com",
            phone: null,
            ownerAgentId: null,
            createdAt: "2026-07-15T00:00:00Z",
          },
        }}
        timelineResult={{
          status: "ok",
          data: {
            subject: { kind: "contact", id: "contact-1", convertedToContactId: null },
            degraded: false,
            sources: {},
            items: [],
            nextBefore: null,
          },
        }}
        basePath="/contacts"
      />
    );

    expect(html).toMatch(/Ada Lovelace/);
    expect(html).toMatch(/ada@example\.com/);
  });

  it("an error detail result still renders a dialog (not a crash) with the error message", () => {
    const html = renderToStaticMarkup(
      <ContactDrawer
        contactId="contact-1"
        detailResult={{ status: "error", message: "This contact could not be found.", correlationId: "corr-1" }}
        timelineResult={{ status: "error", message: "n/a", correlationId: "" }}
        basePath="/contacts"
      />
    );

    expect(html).toMatch(/could not be found/i);
  });

  it("builds the 'Load older' href from the timeline's nextBefore, scoped to this contact and basePath", () => {
    const html = renderToStaticMarkup(
      <ContactDrawer
        contactId="contact-1"
        detailResult={{
          status: "ok",
          contact: {
            contactId: "contact-1",
            accountId: null,
            leadId: null,
            name: "Ada",
            email: "ada@example.com",
            phone: null,
            ownerAgentId: null,
            createdAt: "2026-07-15T00:00:00Z",
          },
        }}
        timelineResult={{
          status: "ok",
          data: {
            subject: { kind: "contact", id: "contact-1", convertedToContactId: null },
            degraded: false,
            sources: {},
            items: [{ kind: "note", occurredAt: "2026-07-01T00:00:00Z", id: "note-1", data: {} }],
            nextBefore: "2026-06-01T00:00:00Z",
          },
        }}
        basePath="/contacts"
      />
    );

    expect(html).toMatch(/contact=contact-1/);
    expect(html).toMatch(/before=2026-06-01T00%3A00%3A00Z/);
  });

  it("never includes tenant_id anywhere (D7)", () => {
    const html = renderToStaticMarkup(
      <ContactDrawer
        contactId="contact-1"
        detailResult={{
          status: "ok",
          contact: {
            contactId: "contact-1",
            accountId: "account-1",
            leadId: null,
            name: "Ada",
            email: "ada@example.com",
            phone: null,
            ownerAgentId: null,
            createdAt: "2026-07-15T00:00:00Z",
          },
        }}
        timelineResult={{
          status: "ok",
          data: {
            subject: { kind: "contact", id: "contact-1", convertedToContactId: null },
            degraded: false,
            sources: {},
            items: [],
            nextBefore: null,
          },
        }}
        basePath="/contacts"
      />
    );

    expect(html).not.toMatch(/tenant_id/i);
    expect(html).not.toMatch(/tenantId/);
  });
});
