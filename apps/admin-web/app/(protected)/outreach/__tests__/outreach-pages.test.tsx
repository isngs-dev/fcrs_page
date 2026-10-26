/**
 * A8 page rendering per role, using the repo's `renderToStaticMarkup` +
 * mocked `next/headers`/`fetch` pattern (no DOM).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import jwt from "jsonwebtoken";

const getMock = vi.fn();
const redirectMock = vi.fn((url: string) => {
  throw new Error(`REDIRECT:${url}`);
});
vi.mock("next/headers", () => ({ cookies: vi.fn(async () => ({ get: getMock })) }));
vi.mock("next/navigation", () => ({
  redirect: redirectMock,
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

const EmailsPage = (await import("@/app/(protected)/outreach/emails/page")).default;
const CallsPage = (await import("@/app/(protected)/outreach/calls/page")).default;
const CampaignsPage = (await import("@/app/(protected)/outreach/campaigns/page")).default;
const SettingsPage = (await import("@/app/(protected)/outreach/settings/page")).default;
const OverviewPage = (await import("@/app/(protected)/outreach/page")).default;
const IntakePage = (await import("@/app/(protected)/outreach/intake/page")).default;
const StatusPage = (await import("@/app/(protected)/outreach/status/page")).default;
const PerformancePage = (await import("@/app/(protected)/outreach/performance/page")).default;
const ExceptionsPage = (await import("@/app/(protected)/outreach/exceptions/page")).default;
const { AgentsTab } = await import("@/app/(protected)/leads/lead-agents-tab");

function as(role: string) {
  const token = jwt.sign({ sub: "u1", role, tenant_id: "t1", project_ids: [] }, process.env.JWT_SECRET as string, {
    algorithm: "HS256",
    expiresIn: "1h",
  });
  getMock.mockReturnValue({ value: token });
}

/** Route fetches by path suffix -> JSON body. */
function backend(routes: Record<string, unknown>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = new URL(String(input)).pathname;
    const key = Object.keys(routes).find((k) => path.endsWith(k));
    return key ? new Response(JSON.stringify(routes[key]), { status: 200 }) : new Response("{}", { status: 404 });
  });
}

const draft = {
  message_id: "m1", thread_id: "th", contact_id: "c1", status: "draft", band: "answer", direction: "outbound",
  from: "team@acme.com", to: "jane@example.com", subject: "Your roof quote", classification: null,
  handled_action: null, task_id: "t", last_error: null, approved_by: null,
  created_at: "2026-10-19T10:00:00Z", sent_at: null, body_text: "Hi Jane", sources: [],
};

const params = (p: Record<string, string>) => ({ searchParams: Promise.resolve(p) });

describe("Outreach pages (A8)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
    redirectMock.mockClear();
  });

  it("overview shows the running state and hides spend from agents", async () => {
    as("CLIENT_AGENT");
    backend({
      "/overview": {
        outreach_paused: false, platform_paused: false, enabled_channels: ["email"], email_mode: "draft_for_review",
        active_plans: 3, tasks_due_24h: 1, drafts_awaiting: 2, emails_sent_7d: 4, calls_7d: 0, calls_booked_7d: 0,
        costs: null,
      },
    });
    const html = renderToStaticMarkup(await OverviewPage());
    expect(html).toContain("Running");
    expect(html).toContain("Drafts awaiting review");
    expect(html).not.toContain("This month");
  });

  it("a draft opens with edit, approve and discard", async () => {
    as("CLIENT_AGENT");
    backend({ "/emails": [draft], "/emails/m1": draft });
    const html = renderToStaticMarkup(await EmailsPage(params({ email: "m1" })));
    expect(html).toContain("Approve &amp; send");
    expect(html).toContain("Discard");
    expect(html).toContain('value="Your roof quote"');
  });

  it("a sent email is read-only", async () => {
    as("CLIENT_ADMIN");
    const sent = { ...draft, status: "sent", sent_at: "2026-10-19T11:00:00Z" };
    backend({ "/emails": [sent], "/emails/m1": sent });
    const html = renderToStaticMarkup(await EmailsPage(params({ status: "sent", email: "m1" })));
    expect(html).not.toContain("Approve");
    expect(html).toContain("Hi Jane");
  });

  it("a call shows its transcript and plays the recording through our proxy (U6)", async () => {
    as("CLIENT_ADMIN");
    const call = {
      call_id: "c1", direction: "outbound", contact_id: "x", from_e164: "+17865550000", to_e164: "+12025550123",
      status: "completed", outcome: "booked", has_recording: true, recording_consent: true, transfer_reason: null,
      summary: "Booked a roof inspection.", sentiment: null, created_at: "2026-10-19T10:00:00Z", duration_s: 95,
    };
    backend({
      "/calls": [call],
      "/calls/c1": { ...call, transcript: [{ turn_index: 1, speaker: "rebecca", text: "Hi Jane, this is Rebecca", created_at: "x" }] },
    });
    const html = renderToStaticMarkup(await CallsPage(params({ call: "c1" })));
    expect(html).toContain('src="/outreach/calls/c1/recording"');
    expect(html).toContain("Hi Jane, this is Rebecca");
    expect(html).toContain("Booked a roof inspection.");
    expect(html).not.toContain("plivo.com");
  });

  it("agents can start outreach but don't get the campaign list", async () => {
    as("CLIENT_AGENT");
    const spy = backend({});
    const html = renderToStaticMarkup(await CampaignsPage());
    expect(html).toContain("Import a contact list");
    expect(html).toContain("Talk to Rebecca");
    expect(spy.mock.calls.some(([u]) => String(u).includes("/campaigns"))).toBe(false);
  });

  it("admins see campaigns with a Stop button", async () => {
    as("CLIENT_ADMIN");
    backend({
      "/campaigns": [{ campaign_id: "k1", name: "Spring", kind: "outbound_csv", goal: null, status: "active",
        created_at: "2026-10-19T10:00:00Z", plans_active: 4, plans_stopped: 0, plans_completed: 1,
        ends_at: null }],
    });
    const html = renderToStaticMarkup(await CampaignsPage());
    expect(html).toContain("Spring");
    expect(html).toContain(">Stop<");
    expect(html).toContain('type="date"'); // A12 F5: the end date control
  });

  it("outreach settings are admin-only", async () => {
    as("CLIENT_AGENT");
    await expect(SettingsPage()).rejects.toThrow("REDIRECT:/");
  });

  it("settings never echo stored secrets", async () => {
    as("CLIENT_ADMIN");
    backend({
      "/settings": { enabled_channels: [], email_mode: "draft_for_review", outreach_paused: true, region_profile: "US",
        call_window_start: "09:00", call_window_end: "20:00", max_touches_per_contact: 3, max_call_attempts: 2,
        min_hours_between_touches: 48, first_inbound_email_delay_minutes: 5, monthly_cost_cap_micros: 150000000,
        physical_address: null, inbound_auto_enabled: false, email_window_start: "08:00", email_window_end: "18:00" },
      "/email-config": { configured: true, provider: "sendgrid", from_address: "a@b.com", has_api_key: true },
      "/call-config": { configured: true, plivo_auth_id: "MA1", from_number: "+18884810711", has_auth_token: true,
        recording_retention_days: 30 },
      "/suppressions": [],
    });
    const html = renderToStaticMarkup(await SettingsPage());
    expect(html).toContain("Resume outreach");
    expect(html).toContain('value="150.00"');
    expect(html).toMatch(/name="email_window_start"[^>]*value="08:00"/); // A12 F4
    expect(html).toContain("leave blank to keep");
    expect(html).toMatch(/name="retention_days"[^>]*value="30"/); // A9 H8: shown, so a save keeps it
    expect(html).not.toMatch(/name="auth_token"[^>]*value=/);
  });
});

describe("Lead intake page (A10)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  it("shows the URLs to connect and the queue, incl. Gmail's confirmation code", async () => {
    as("CLIENT_ADMIN");
    backend({
      "/admin/intake/config": {
        enabled: true, trusted_senders: ["facebookmail.com"], form_consent_wording: null,
        call_consent_wording: null, meta_page_id: null, has_meta_page_token: false,
        form_url: "https://api.example.com/public/intake/forms/tok", email_parse_url: "https://api.example.com/public/intake/email/tok2",
        meta_webhook_url: null,
      },
      "/admin/intake/events": [
        { intake_id: "i1", source: "meta_email", status: "notice", reason: "GMAIL_FORWARDING_CONFIRMATION", lead_id: null,
          fields: { code: "987654321" }, received_at: "2026-10-19T10:00:00Z", resolved_by: null },
        { intake_id: "i2", source: "web_form", status: "rejected", reason: "NO_CONTACT_DETAILS", lead_id: null,
          fields: { values: { name: "No Contact" } }, received_at: "2026-10-19T10:00:00Z", resolved_by: null },
      ],
    });
    const html = renderToStaticMarkup(await IntakePage());
    expect(html).toContain("https://api.example.com/public/intake/forms/tok");
    expect(html).toContain("Confirmation code 987654321");
    expect(html).toContain("No email or phone number");
    expect(html).not.toContain("Meta Lead Ads (direct)"); // platform app not configured
  });

  it("is admin-only", async () => {
    as("CLIENT_AGENT");
    await expect(IntakePage()).rejects.toThrow("REDIRECT:/");
  });
});

describe("Lead drawer Agents tab (U11)", () => {
  it("shows the plan with Take over, consents, emails and calls", () => {
    const html = renderToStaticMarkup(
      <AgentsTab
        platformView={false}
        result={{
          status: "ok",
          activity: {
            contactId: "c1",
            plan: { planId: "p1", status: "active", stopReason: null, createdAt: "2026-10-19T10:00:00Z",
              tasks: [{ taskId: "t1", stepIndex: 0, channel: "call", scheduledAt: "2026-10-19T10:00:00Z", status: "scheduled", lastError: null }] },
            consents: [{ consentId: "k", channel: "call", status: "granted", basis: "express_consent", source: "widget", recordedAt: "2026-10-19T10:00:00Z" }],
            emails: [],
            calls: [],
            memory: [{ at: "2026-10-19T10:00:00Z", channel: "call", kind: "asked", text: "Does the warranty cover storms?" }],
          },
        }}
      />
    );
    expect(html).toContain("What Rebecca knows");
    expect(html).toContain("Does the warranty cover storms?");
    expect(html).toContain("Take over (stop Rebecca)");
    expect(html).toContain("Granted");
    expect(html).toContain("No emails.");
  });

  it("says so when the lead has no agent activity, and in the platform view", () => {
    const empty = renderToStaticMarkup(
      <AgentsTab platformView={false} result={{ status: "ok", activity: { contactId: null, plan: null, consents: [], emails: [], calls: [] } }} />
    );
    expect(empty).toContain("No agent activity");
    expect(renderToStaticMarkup(<AgentsTab platformView result={null} />)).toContain("Switch to this chatbot");
  });
});

describe("Status, performance, exceptions (A13)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    getMock.mockReset();
  });

  const perf = {
    days: 30, leads: 4, contacted: 2, median_response_minutes: 7, emails_sent: 2, reply_rate: 0.5,
    meetings: 1, lead_to_meeting: 0.25, attended: 1, no_show: 0, show_rate: 1, bounce_rate: 0.5,
    complaint_rate: 0, opt_out_rate: null, calls_placed: 0, call_connection_rate: null,
    costs: { total_micros: 8000000, per_lead_micros: 2000000, per_meeting_micros: 8000000 },
    meetings_to_mark: [{ event_id: "e1", name: "Jane", email: "j@x.com", starts_at: "2026-10-19T10:00:00Z" }],
  };

  it("status shows every state with its count and links to the lead", async () => {
    as("CLIENT_ADMIN");
    backend({
      "/statuses": {
        counts: { new: 1, contacted: 0, engaged: 2, qualified: 0, meeting_booked: 1, human_handoff: 0,
          invalid_contact: 0, not_interested: 0, opted_out: 0 },
        contacts: [{ contact_id: "c1", name: "Jane", email: "j@x.com", lead_id: "l1", status: "engaged" }],
      },
    });
    const html = renderToStaticMarkup(await StatusPage(params({})));
    expect(html).toContain("All (4)");
    expect(html).toContain("Engaged (2)");
    expect(html).toContain('href="/leads?lead=l1&amp;tab=agents"');
  });

  it("performance shows rates, costs for admins and meetings to mark", async () => {
    as("CLIENT_ADMIN");
    backend({ "/performance": perf });
    const html = renderToStaticMarkup(await PerformancePage(params({ days: "30" })));
    expect(html).toContain("25%");
    expect(html).toContain("$2.00");
    expect(html).toContain("Did these meetings happen?");
    expect(html).toContain(">Attended<");
  });

  it("agents get performance without cost; status and exceptions stay admin-only", async () => {
    as("CLIENT_AGENT");
    backend({ "/performance": { ...perf, costs: null } });
    const html = renderToStaticMarkup(await PerformancePage(params({})));
    expect(html).not.toContain("Cost per lead");
    await expect(StatusPage(params({}))).rejects.toThrow("REDIRECT:/");
    await expect(ExceptionsPage()).rejects.toThrow("REDIRECT:/");
  });

  it("exceptions explain codes in plain words, with retry only for live plans", async () => {
    as("CLIENT_ADMIN");
    backend({
      "/exceptions": {
        tasks: [
          { task_id: "t1", channel: "email", status: "failed", last_error: "LLM_ERROR", attempts: 3,
            scheduled_at: "2026-10-19T10:00:00Z", contact_id: "c1", name: "Jane", plan_status: "active" },
          { task_id: "t2", channel: "call", status: "failed", last_error: "PLIVO_401", attempts: 1,
            scheduled_at: "2026-10-19T10:00:00Z", contact_id: "c2", name: "Ana", plan_status: "stopped" },
        ],
        dead_events: [], blocked_7d: { NO_CONSENT: 3 }, needs_human_7d: 2, intake_queue: 1,
      },
    });
    const html = renderToStaticMarkup(await ExceptionsPage());
    expect(html).toContain("The AI model was unavailable");
    expect(html).toContain("No permission for that channel: 3");
    expect(html.match(/>Retry</g)?.length).toBe(1); // only the live plan's step
    expect(html.match(/>Dismiss</g)?.length).toBe(2);
  });
});
