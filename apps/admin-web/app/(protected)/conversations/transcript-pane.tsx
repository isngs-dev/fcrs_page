/**
 * Transcript pane (HANDOFF-SPEC.md §3 "4a": "transcript pane (source-
 * attribution line under bot replies, centered 'Lead captured' event chip on
 * #eef7a8, take-over composer locked until takeover)"). Restyled from the
 * mock's raw markup (`Chatbot System Designs.dc.html#4a`, lines 59-86) to
 * real data with three mandated honest deviations from the mock:
 *
 *  1. "Source-attribution line" -> the real message schema has `intent` +
 *     `confidence` (float), NOT a source/citation/document field. This
 *     renders a "confidence: 0.94" style meta line under bot messages when
 *     `confidence` is present, and omits the line entirely otherwise. It is
 *     never labelled "source" and never fabricates a document name.
 *  2. "Lead captured" event chip -> omitted entirely. There is no
 *     lead-linkage field anywhere in `ConversationDetailResponse` (no
 *     `lead_id`), so rendering this chip would fabricate a connection the
 *     backend doesn't provide (CLAUDE.md §3, no silent fallbacks).
 *  3. Take-over composer -> renders visually per the mock but is always
 *     `disabled` (real HTML attribute, not just visual) with an honest
 *     permanent hint, since there is no live-bot-activity signal to detect
 *     and no admin-send-message endpoint to wire it to.
 *
 * SR-27 slice 2: geometry fixed to `Console.dc.html:404-423` -- monospace
 * 15px/600 conversation id in the header, visitor bubbles at
 * `border-radius:16px 16px 4px 16px` on `#fbfaf7`/near-black text with a
 * 70% max-width, bot bubbles at `16px 16px 16px 4px` with a 28px avatar.
 * Reply-bar decision (D13) unchanged -- see `page.tsx`'s header comment for
 * the endpoint evidence; still rendered, still `disabled`, still honest.
 */
import { formatDateTime, statusBadgeStyle } from "@/app/(protected)/conversations/presentation";
import { MessageSources } from "@/app/(protected)/conversations/message-sources";
import { shouldShowSourcesAffordance } from "@/app/(protected)/conversations/message-sources-presentation";
import { getMessageSources, type ConversationDetail, type MessageSourcesResult } from "@/lib/conversations";

/**
 * Grounding spot-check (SR-2) -- a Server Function bound with this
 * conversation's `conversationId`/`tenantId` and passed down to the
 * client-side `MessageSources` toggle so it can fetch a bot message's
 * resolved sources on expand without exposing `lib/conversations.ts`
 * (marked `import "server-only"`) to client bundling. Mirrors the
 * `updatePostAction` inline-`"use server"` pattern.
 */
function bindFetchMessageSources(conversationId: string, tenantId: string | undefined) {
  async function fetchMessageSources(messageId: string): Promise<MessageSourcesResult> {
    "use server";
    return getMessageSources(conversationId, messageId, tenantId);
  }
  return fetchMessageSources;
}

function MessageBubble({
  role,
  content,
  intent,
  confidence,
  createdAt,
  messageId,
  sourceCount,
  fetchSourcesAction,
}: {
  role: string;
  content: string;
  intent: string | null;
  confidence: number | null;
  createdAt: string;
  messageId: string;
  sourceCount: number;
  fetchSourcesAction: (messageId: string) => Promise<MessageSourcesResult>;
}) {
  const isVisitor = role === "user" || role === "visitor";
  const isBot = role === "assistant" || role === "bot";

  // SR-15 D10: chat bubbles use the design's 16px hero/bubble radius (a
  // deliberate exception to the shared 5-step ramp for cards/chips/tables --
  // scope item 8 calls this out explicitly). D1: the bot avatar's citron
  // radial gradient is deleted and re-decided to a flat --secondary fill --
  // it is a small decorative dot, not a place the design's one chromatic
  // pair belongs.
  const bubble = isVisitor ? (
    <div
      className="max-w-[70%] self-end rounded-[16px_16px_4px_16px] px-4 py-[11px] text-[13px] leading-relaxed"
      style={{ background: "#fbfaf7", color: "#333333" }}
      title={formatDateTime(createdAt)}
    >
      {content}
    </div>
  ) : (
    <div className="flex max-w-[70%] items-end gap-2 self-start">
      {isBot ? (
        <span
          aria-hidden
          className="mb-0.5 size-[28px] shrink-0 rounded-full bg-secondary"
        />
      ) : null}
      <div
        className="rounded-[16px_16px_16px_4px] bg-secondary px-4 py-[11px] text-[13px] leading-relaxed text-foreground"
        title={formatDateTime(createdAt)}
      >
        {content}
      </div>
    </div>
  );

  return (
    <div className="flex flex-col gap-1">
      {bubble}
      {isBot && confidence !== null ? (
        <span className="ml-9 text-[10.5px] text-muted-foreground">
          {intent ? `${intent} · ` : ""}confidence {confidence.toFixed(2)}
        </span>
      ) : null}
      {shouldShowSourcesAffordance(role, sourceCount) ? (
        <div className="ml-9">
          <MessageSources
            messageId={messageId}
            sourceCount={sourceCount}
            replyContent={content}
            fetchSourcesAction={fetchSourcesAction}
          />
        </div>
      ) : null}
    </div>
  );
}

export function TranscriptPane({
  conversation,
  tenantId,
}: {
  conversation: ConversationDetail;
  tenantId?: string;
}) {
  const badge = statusBadgeStyle(conversation.status);
  const fetchSourcesAction = bindFetchMessageSources(conversation.conversationId, tenantId);

  return (
    <div className="flex flex-1 flex-col bg-secondary">
      <div className="flex items-center gap-3 border-b border-border bg-card px-5 py-3.5">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <p className="truncate font-mono text-[15px] font-semibold text-foreground">
              {conversation.conversationId}
            </p>
            <span
              className="flex h-5 items-center gap-1 rounded-[5px] px-2 text-[10px] font-bold"
              style={{ background: badge.bg, color: badge.fg }}
            >
              {badge.dot ? (
                <span aria-hidden className="size-[6px] rounded-full" style={{ background: badge.dot }} />
              ) : null}
              {badge.label}
            </span>
          </div>
          <p className="truncate text-[12px] text-muted-foreground">
            {conversation.channel} · started {formatDateTime(conversation.startedAt)}
            {conversation.endedAt ? ` · ended ${formatDateTime(conversation.endedAt)}` : ""}
          </p>
        </div>
      </div>

      <div className="flex flex-1 flex-col gap-3 overflow-y-auto p-6">
        {conversation.summary ? (
          <p className="self-center rounded-full bg-border px-3 py-1 text-center text-[10.5px] text-muted-foreground">
            {conversation.summary}
          </p>
        ) : null}
        {conversation.messages.length === 0 ? (
          <p role="status" className="self-center text-[13px] text-muted-foreground">
            No messages in this conversation yet.
          </p>
        ) : (
          conversation.messages.map((message) => (
            <MessageBubble
              key={message.messageId}
              role={message.role}
              content={message.content}
              intent={message.intent}
              confidence={message.confidence}
              createdAt={message.createdAt}
              messageId={message.messageId}
              sourceCount={message.sourceCount}
              fetchSourcesAction={fetchSourcesAction}
            />
          ))
        )}
      </div>

      <div className="flex flex-col gap-1.5 border-t border-border bg-card px-[22px] py-4">
        <span className="text-[11px] text-muted-foreground">
          Agent takeover isn&apos;t available yet -- there is no admin reply endpoint for this
          console. Replies below are disabled.
        </span>
        <div className="flex items-center gap-2">
          <label htmlFor="takeover-composer" className="sr-only">
            Take over to type a reply
          </label>
          <input
            id="takeover-composer"
            type="text"
            disabled
            placeholder="Take over to type…"
            className="h-[42px] flex-1 rounded-[11px] border border-border bg-secondary px-3.5 text-[13px] text-muted-foreground outline-none disabled:cursor-not-allowed"
          />
          {/* SR-15 D1: the disabled send button's citron-adjacent fill is
              deleted and re-decided to --secondary -- it is inert (no-silent-
              fallback), so it should read as visibly inactive, not accented. */}
          <button
            type="button"
            disabled
            aria-label="Send (disabled -- no admin reply endpoint exists)"
            className="grid size-[42px] shrink-0 place-items-center rounded-full bg-secondary text-[14px] text-muted-foreground disabled:cursor-not-allowed"
          >
            <span aria-hidden>↑</span>
          </button>
        </div>
      </div>
    </div>
  );
}
