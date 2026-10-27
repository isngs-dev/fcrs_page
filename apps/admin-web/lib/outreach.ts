/**
 * Server functions for the Outreach area (A8) -- `"use server"`, same one-file
 * shape as `lib/voice-calls.ts`: callable from server components and directly
 * from the thin client components (forms/buttons) that then `router.refresh()`.
 *
 * Every call hits the implicit `/admin/outreach/...` routes (U2: no tenantId);
 * the backend scopes the tenant and enforces roles. Responses are camelized
 * once (`camelize`), results are `{status: "ok"} | {status: "error"}`.
 */
"use server";

import { adminApiFetch, AdminApiError } from "@/lib/api";
import { camelize } from "@/lib/outreach-format";

export type OutreachError = { status: "error"; message: string; errorCode: string; correlationId: string };
export type Result<T> = ({ status: "ok" } & T) | OutreachError;

// -- Shapes (camelCase mirrors of the A1–A8 route outputs) ----------------------

export interface Overview {
  outreachPaused: boolean;
  platformPaused: boolean;
  enabledChannels: string[];
  emailMode: string;
  activePlans: number;
  tasksDue24h: number;
  draftsAwaiting: number;
  emailsSent7d: number;
  calls7d: number;
  callsBooked7d: number;
  costs: { monthMicros: number; capMicros: number | null; byKind: Record<string, number> } | null;
}

export interface EmailMessage {
  messageId: string;
  threadId: string;
  contactId: string | null;
  status: string;
  band: string | null;
  direction: string;
  from: string;
  to: string;
  subject: string;
  classification: string | null;
  handledAction: string | null;
  lastError: string | null;
  approvedBy: string | null;
  createdAt: string;
  sentAt: string | null;
  bodyText?: string;
  sources?: { title?: string; docId?: string; chunkId?: string }[];
}

export interface CallSummary {
  callId: string;
  direction: string;
  contactId: string | null;
  fromE164: string;
  toE164: string;
  status: string;
  outcome: string | null;
  hasRecording: boolean;
  recordingConsent: boolean;
  transferReason: string | null;
  summary: string | null;
  sentiment: string | null;
  createdAt: string;
  durationS: number | null;
}

export interface CallDetail extends CallSummary {
  transcript: { turnIndex: number; speaker: string; text: string; createdAt: string }[];
}

export interface Campaign {
  campaignId: string;
  name: string;
  kind: string;
  goal: string | null;
  status: string;
  createdAt: string;
  plansActive: number;
  plansStopped: number;
  plansCompleted: number;
  endsAt: string | null;
}

export interface ImportPreview {
  importId: string;
  status: string;
  filename: string | null;
  campaignName: string;
  report: {
    total: number;
    valid: number;
    new: number;
    existing: number;
    rejectedCount: number;
    rejected: { row: number; reasons: string[] }[];
    unknownColumns: string[];
    callConsentRows: number;
    needsDncScrub: number;
    committed?: Record<string, number>;
  };
}

export interface CommandDraft {
  draftId: string;
  status: string;
  summary: string;
  action: string;
  channels: string[];
  goal: string | null;
  contactCount: number;
  sample: string[];
}

export interface AgentSettings {
  enabledChannels: string[];
  emailMode: string;
  outreachPaused: boolean;
  regionProfile: string;
  callWindowStart: string;
  callWindowEnd: string;
  maxTouchesPerContact: number;
  maxCallAttempts: number;
  minHoursBetweenTouches: number;
  firstInboundEmailDelayMinutes: number;
  monthlyCostCapMicros: number | null;
  physicalAddress: string | null;
  inboundAutoEnabled: boolean;
  emailWindowStart: string;
  emailWindowEnd: string;
}

export interface EmailConfig {
  configured: boolean;
  provider?: string;
  fromAddress?: string;
  fromName?: string | null;
  replyTo?: string | null;
  bookingUrl?: string | null;
  hasApiKey?: boolean;
  enabled?: boolean;
  replyDomain?: string | null;
  hasEventKey?: boolean;
  inboundParseUrl?: string;
  eventWebhookUrl?: string;
}

export interface CallConfig {
  configured: boolean;
  enabled?: boolean;
  plivoAuthId?: string;
  fromNumber?: string;
  transferNumber?: string | null;
  companyName?: string;
  voice?: string;
  voiceModel?: string | null;
  voicemailPolicy?: string;
  voicemailMessage?: string | null;
  maxCallSeconds?: number;
  maxConcurrentCalls?: number;
  recordingEnabled?: boolean;
  inboundEnabled?: boolean;
  recordingRetentionDays?: number;
  inboundAnswerUrl?: string;
  inboundHangupUrl?: string;
}

export interface Suppression {
  suppressionId: string;
  channel: string;
  value: string;
  reason: string;
  source: string;
  createdAt: string;
}

export interface LeadActivity {
  contactId: string | null;
  plan: {
    planId: string;
    status: string;
    stopReason: string | null;
    createdAt: string;
    tasks: { taskId: string; stepIndex: number; channel: string; scheduledAt: string; status: string; lastError: string | null }[];
  } | null;
  consents: { consentId: string; channel: string; status: string; basis: string; source: string; recordedAt: string }[];
  emails: EmailMessage[];
  calls: CallSummary[];
  memory?: { at: string; channel: string; kind: string; text: string }[];
}

// -- Plumbing --------------------------------------------------------------------

function toError(error: unknown): OutreachError {
  if (error instanceof AdminApiError) {
    let message = error.message;
    if (error.status === 401) message = "Your session has expired. Please log in again.";
    else if (error.status === 403) message = "You do not have permission to do this.";
    else if (error.status >= 500 || !message)
      message = `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
        error.correlationId || "n/a"
      }.`;
    return { status: "error", message, errorCode: error.errorCode, correlationId: error.correlationId };
  }
  return { status: "error", message: "Unable to reach the server. Please try again.", errorCode: "", correlationId: "" };
}

async function call<T, K extends string>(
  key: K,
  path: string,
  init?: RequestInit
): Promise<Result<{ [P in K]: T }>> {
  try {
    // Paths are relative to /admin/outreach unless they name another /admin area.
    const url = path.startsWith("/admin/") ? path : `/admin/outreach${path}`;
    const response = await adminApiFetch(url, init);
    const body = response.status === 204 ? null : await response.json();
    return { status: "ok", [key]: camelize<T>(body) } as { status: "ok" } & { [P in K]: T };
  } catch (error) {
    return toError(error);
  }
}

function json(method: string, body: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

const q = (params: Record<string, string | undefined>) => {
  const search = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) search.set(k, v);
  const text = search.toString();
  return text ? `?${text}` : "";
};

const id = encodeURIComponent;

// -- Overview + campaigns ------------------------------------------------------------

export async function getOverview() {
  return call<Overview, "overview">("overview", "/overview");
}

export async function listCampaigns() {
  return call<Campaign[], "campaigns">("campaigns", "/campaigns");
}

export async function setCampaignEnd(campaignId: string, endsAt: string | null) {
  return call<{ endsAt: string | null }, "result">("result", `/campaigns/${id(campaignId)}`, json("PATCH", { ends_at: endsAt }));
}

export async function stopCampaign(campaignId: string) {
  return call<{ plansStopped: number }, "result">("result", `/campaigns/${id(campaignId)}:stop`, { method: "POST" });
}

// -- Emails --------------------------------------------------------------------------

export async function listEmails(status?: string) {
  return call<EmailMessage[], "emails">("emails", `/emails${q({ status, limit: "100" })}`);
}

export async function getEmail(messageId: string) {
  return call<EmailMessage, "email">("email", `/emails/${id(messageId)}`);
}

export async function saveDraft(messageId: string, subject: string, bodyText: string) {
  return call<EmailMessage, "email">("email", `/emails/${id(messageId)}`, json("PUT", { subject, body_text: bodyText }));
}

export async function approveEmail(messageId: string) {
  return call<{ status: string }, "result">("result", `/emails/${id(messageId)}:approve`, { method: "POST" });
}

export async function discardEmail(messageId: string) {
  return call<{ status: string }, "result">("result", `/emails/${id(messageId)}:discard`, { method: "POST" });
}

// -- Calls ---------------------------------------------------------------------------

export async function listCalls() {
  return call<CallSummary[], "calls">("calls", "/calls?limit=100");
}

export async function getCall(callId: string) {
  return call<CallDetail, "call">("call", `/calls/${id(callId)}`);
}

// -- Starting outreach (A5) ----------------------------------------------------------

export async function uploadImport(form: FormData) {
  return call<ImportPreview, "preview">("preview", "/imports", { method: "POST", body: form });
}

export async function commitImport(importId: string, consentAttested: boolean, startOutreach: boolean) {
  return call<{ report: ImportPreview["report"] }, "result">(
    "result",
    `/imports/${id(importId)}:commit`,
    json("POST", { consent_attested: consentAttested, start_outreach: startOutreach })
  );
}

export async function discardImport(importId: string) {
  return call<{ status: string }, "result">("result", `/imports/${id(importId)}:discard`, { method: "POST" });
}

export async function addContact(values: Record<string, string | boolean | undefined>) {
  return call<{ contactId: string; contact: string; plan: string; warnings: string[] }, "result">("result", "/contacts:add", json("POST", values));
}

export async function createCommand(text: string) {
  return call<CommandDraft, "draft">("draft", "/commands", json("POST", { text }));
}

export async function confirmCommand(draftId: string) {
  return call<{ result: Record<string, number> }, "result">("result", `/commands/${id(draftId)}:confirm`, { method: "POST" });
}

export async function discardCommand(draftId: string) {
  return call<{ status: string }, "result">("result", `/commands/${id(draftId)}:discard`, { method: "POST" });
}

// -- Settings ------------------------------------------------------------------------

export async function getAgentSettings() {
  return call<AgentSettings, "settings">("settings", "/settings");
}

export async function saveAgentSettings(values: Record<string, unknown>) {
  return call<AgentSettings, "settings">("settings", "/settings", json("PUT", values));
}

export async function setOutreachPaused(paused: boolean) {
  return call<{ outreachPaused: boolean }, "result">("result", paused ? "/pause" : "/resume", { method: "POST" });
}

export async function getEmailConfig() {
  return call<EmailConfig, "config">("config", "/email-config");
}

export async function saveEmailConfig(values: Record<string, unknown>) {
  return call<EmailConfig, "config">("config", "/email-config", json("PUT", values));
}

export async function getCallAgentConfig() {
  return call<CallConfig, "config">("config", "/call-config");
}

export async function saveCallAgentConfig(values: Record<string, unknown>) {
  return call<CallConfig, "config">("config", "/call-config", json("PUT", values));
}

export async function listSuppressions(search?: string) {
  return call<Suppression[], "suppressions">("suppressions", `/suppressions${q({ q: search, limit: "100" })}`);
}

export async function addSuppression(channel: string, value: string) {
  return call<Suppression, "suppression">("suppression", "/suppressions", json("POST", { channel, value, reason: "manual" }));
}

export async function removeSuppression(suppressionId: string) {
  return call<null, "result">("result", `/suppressions/${id(suppressionId)}`, { method: "DELETE" });
}

// -- Lead drawer "Agents" tab (U11) ---------------------------------------------------

export async function getLeadActivity(leadId: string) {
  return call<LeadActivity, "activity">("activity", `/leads/${id(leadId)}/activity`);
}

/** The same view for a contact with no lead (added by hand / CSV). */
export type ContactActivity = LeadActivity & { name: string | null; email: string | null };

export async function getContactActivity(contactId: string) {
  return call<ContactActivity, "activity">("activity", `/contacts/${id(contactId)}/activity`);
}

export async function takeOver(contactId: string) {
  return call<{ eventId: string; tasksCancelled: number }, "result">("result", `/contacts/${id(contactId)}:takeover`, { method: "POST" });
}

// -- Lead intake (A10) -----------------------------------------------------------------

export interface IntakeConfig {
  enabled: boolean;
  trustedSenders: string[];
  formConsentWording: string | null;
  callConsentWording: string | null;
  metaPageId: string | null;
  hasMetaPageToken: boolean;
  formUrl: string;
  emailParseUrl: string;
  metaWebhookUrl: string | null;
}

export interface IntakeEvent {
  intakeId: string;
  source: string;
  status: string;
  reason: string | null;
  leadId: string | null;
  fields: {
    values?: Record<string, string>;
    answers?: Record<string, string>;
    form?: string | null;
    from?: string;
    subject?: string;
    code?: string;
  };
  receivedAt: string;
}

export async function getIntakeConfig() {
  return call<IntakeConfig, "config">("config", "/admin/intake/config");
}

export async function saveIntakeConfig(values: Record<string, unknown>) {
  return call<IntakeConfig, "config">("config", "/admin/intake/config", json("PUT", values));
}

export async function listIntakeQueue() {
  return call<IntakeEvent[], "events">("events", "/admin/intake/events?queue=true&limit=100");
}

export async function resolveIntakeEvent(intakeId: string) {
  return call<{ status: string }, "result">("result", `/admin/intake/events/${id(intakeId)}:resolve`, { method: "POST" });
}

// -- Status, performance, exceptions (A13) ------------------------------------------------

export interface StatusRow {
  contactId: string;
  name: string | null;
  email: string | null;
  leadId: string | null;
  status: string;
}

export interface Performance {
  days: number;
  leads: number;
  contacted: number;
  medianResponseMinutes: number | null;
  emailsSent: number;
  replyRate: number | null;
  meetings: number;
  leadToMeeting: number | null;
  attended: number;
  noShow: number;
  showRate: number | null;
  bounceRate: number | null;
  complaintRate: number | null;
  optOutRate: number | null;
  callsPlaced: number;
  callConnectionRate: number | null;
  costs: { totalMicros: number; perLeadMicros: number | null; perMeetingMicros: number | null } | null;
  meetingsToMark: { eventId: string; name: string | null; email: string | null; startsAt: string }[];
}

export interface Exceptions {
  tasks: { taskId: string; channel: string; status: string; lastError: string | null; attempts: number; scheduledAt: string; contactId: string; name: string | null; planStatus: string }[];
  deadEvents: { eventId: string; kind: string; attempts: number; lastError: string | null; createdAt: string }[];
  blocked7d: Record<string, number>;
  needsHuman7d: number;
  intakeQueue: number;
}

export async function getStatuses(status?: string) {
  return call<{ counts: Record<string, number>; contacts: StatusRow[] }, "data">("data", `/statuses${q({ status, limit: "200" })}`);
}

export async function getPerformance(days: number) {
  return call<Performance, "performance">("performance", `/performance?days=${days}`);
}

export async function setMeetingOutcome(eventId: string, outcome: "attended" | "no_show") {
  return call<{ status: string }, "result">("result", `/meetings/${id(eventId)}:outcome`, json("POST", { outcome }));
}

export async function getExceptions() {
  return call<Exceptions, "exceptions">("exceptions", "/exceptions");
}

export async function retryTask(taskId: string) {
  return call<{ status: string }, "result">("result", `/tasks/${id(taskId)}:retry`, { method: "POST" });
}

export async function dismissTask(taskId: string) {
  return call<{ status: string }, "result">("result", `/tasks/${id(taskId)}:dismiss`, { method: "POST" });
}

export async function retryEvent(eventId: string) {
  return call<{ status: string }, "result">("result", `/events/${id(eventId)}:retry`, { method: "POST" });
}
