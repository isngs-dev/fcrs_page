/**
 * Server-only composition layer for the dashboard chatbot hub (SR-8).
 * It uses only two existing tenant-scoped read APIs: analytics overview for
 * historical facts and the active-conversations list's total for the live
 * count. Errors remain errors; this layer never supplies substitute metrics.
 */
import "server-only";

import { AdminApiError, adminApiFetch } from "@/lib/api";
import type { AnalyticsOverview } from "@/lib/analytics";

export const HUB_PERIODS = ["week", "month", "year"] as const;
export type HubPeriod = (typeof HUB_PERIODS)[number];
export const HUB_BUCKETS = ["day", "week"] as const;
export type HubBucket = (typeof HUB_BUCKETS)[number];

export interface HubQuery {
  period?: string;
  bucket?: string;
}

export interface HubActiveConversation {
  conversationId: string;
  status: string;
  channel: string;
  startedAt: string;
  messageCount: number;
  summary: string | null;
}

export interface ChatbotHub {
  analytics: AnalyticsOverview;
  activeConversations: { total: number; items: HubActiveConversation[] };
  closedConversations: { total: number };
  period: HubPeriod;
  bucket: HubBucket;
}

export type HubResult =
  | { status: "ok"; data: ChatbotHub }
  | { status: "error"; message: string; correlationId: string };

interface AnalyticsOverviewResponseBody {
  window: { from: string; to: string; bucket: string };
  totals: { conversations: number; user_turns: number; bot_turns: number; decided_bot_turns: number };
  intent_distribution: Record<string, number>;
  decision_distribution: Record<string, number>;
  fallback_rate: number | null;
  deflection_rate: number | null;
  grounded_rate: number | null;
  schedule: { cta_conversations: number; conversions: number; conversion_rate: number | null };
  series: {
    bucket_start: string;
    conversations: number;
    answers: number;
    escalations: number;
    bookings: number;
  }[];
}

interface ActiveConversationsResponseBody {
  items: {
    conversation_id: string;
    status: string;
    channel: string;
    started_at: string;
    message_count: number;
    summary: string | null;
  }[];
  total: number;
}

/** Only `total` is consulted for the Closed-Conversations count (D3) --
 * `limit=1` keeps the read cheap and we never parse/render its `items`. */
interface ConversationsCountResponseBody {
  total: number;
}

function resolvePeriod(value: string | undefined): HubPeriod {
  return HUB_PERIODS.includes(value as HubPeriod) ? (value as HubPeriod) : "month";
}

function resolveBucket(value: string | undefined): HubBucket {
  return HUB_BUCKETS.includes(value as HubBucket) ? (value as HubBucket) : "day";
}

/** Build the only analytics request this hub needs. The browser supplies a
 * small named selector; the server computes actual ISO window boundaries. */
export function resolveHubQuery(params: HubQuery, now = new Date()): string {
  const period = resolvePeriod(params.period);
  const bucket = resolveBucket(params.bucket);
  const days = period === "week" ? 7 : period === "year" ? 365 : 30;
  const from = new Date(now.getTime() - days * 24 * 60 * 60 * 1000);
  const query = new URLSearchParams({
    from: from.toISOString(),
    to: now.toISOString(),
    bucket,
  });
  return query.toString();
}

function toAnalyticsOverview(body: AnalyticsOverviewResponseBody): AnalyticsOverview {
  return {
    window: body.window,
    totals: {
      conversations: body.totals.conversations,
      userTurns: body.totals.user_turns,
      botTurns: body.totals.bot_turns,
      decidedBotTurns: body.totals.decided_bot_turns,
    },
    intentDistribution: body.intent_distribution,
    decisionDistribution: body.decision_distribution,
    fallbackRate: body.fallback_rate,
    deflectionRate: body.deflection_rate,
    groundedRate: body.grounded_rate,
    schedule: {
      ctaConversations: body.schedule.cta_conversations,
      conversions: body.schedule.conversions,
      conversionRate: body.schedule.conversion_rate,
    },
    series: body.series.map((entry) => ({
      bucketStart: entry.bucket_start,
      conversations: entry.conversations,
      answers: entry.answers,
      escalations: entry.escalations,
      bookings: entry.bookings,
    })),
  };
}

function toActiveConversations(body: ActiveConversationsResponseBody): ChatbotHub["activeConversations"] {
  return {
    total: body.total,
    items: body.items.map((item) => ({
      conversationId: item.conversation_id,
      status: item.status,
      channel: item.channel,
      startedAt: item.started_at,
      messageCount: item.message_count,
      summary: item.summary,
    })),
  };
}

export async function getChatbotHub(params: HubQuery): Promise<HubResult> {
  const period = resolvePeriod(params.period);
  const bucket = resolveBucket(params.bucket);
  const analyticsPath = `/admin/analytics/overview?${resolveHubQuery({ period, bucket })}`;
  // SR-12 D3: read up to 3 active conversations so the "Answered by chatbot"
  // card can render an honest multi-row activity list (still the same
  // tenant-scoped endpoint; only the `limit` value changes, additively).
  const activePath = "/admin/conversations?status=active&limit=3";
  // Closed Conversations (D3): the `total` from the same list endpoint,
  // filtered to `status=ended` -- a real, tenant-scoped fact, not derived
  // by subtraction (avoids a two-read race/skew) and not fabricated.
  const closedPath = "/admin/conversations?status=ended&limit=1";

  try {
    const [analyticsResponse, activeResponse, closedResponse] = await Promise.all([
      adminApiFetch(analyticsPath),
      adminApiFetch(activePath),
      adminApiFetch(closedPath),
    ]);
    const analytics = toAnalyticsOverview(
      (await analyticsResponse.json()) as AnalyticsOverviewResponseBody
    );
    const activeConversations = toActiveConversations(
      (await activeResponse.json()) as ActiveConversationsResponseBody
    );
    const closedBody = (await closedResponse.json()) as ConversationsCountResponseBody;
    const closedConversations = { total: closedBody.total };
    return {
      status: "ok",
      data: { analytics, activeConversations, closedConversations, period, bucket },
    };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return {
        status: "error",
        message: "Chatbot hub data could not be loaded. Please try again.",
        correlationId: error.correlationId,
      };
    }
    return {
      status: "error",
      message: "Chatbot hub data could not be loaded. Please try again.",
      correlationId: "",
    };
  }
}
