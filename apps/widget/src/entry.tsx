/**
 * IIFE boot orchestration (S14.1 decision 3, scope item 6).
 *
 * The ONLY module with top-level side effects. Runs synchronously at
 * script-eval time (so `document.currentScript` resolves correctly in
 * config.ts), then performs the async admission handshake.
 *
 * Sequence: config -> mount shell -> mint -> render placeholder / handle
 * failure. A missing client key mounts nothing at all (step 1). Every
 * other failure mounts the shadow host (so the debug strip has somewhere
 * to render) but shows no user-facing UI unless data-debug is set.
 *
 * S14.6 (decision 2, scope item 4): the admission mint is wrapped in
 * `withRetry` so a transient boot-time network blip doesn't permanently
 * kill the widget — bounded attempts with exponential backoff + jitter,
 * then the existing honest hard-stop (no UI / debug strip only) when the
 * cap is hit. A non-retryable admission error (`INVALID_CLIENT_KEY`,
 * `ORIGIN_NOT_ALLOWED`, `TENANT_DISABLED`) is still never retried — the
 * shared `withRetry` classifier already returns those immediately.
 *
 * SR-3 (decision 2/7/8, scope item 3): BEFORE minting, check for an
 * unexpired resume record (`resume.ts#readResumeRecord`). A record can only
 * exist if a PRIOR load's admission said `resume_enabled` (session.ts only
 * writes one when opted in) — so the record itself is the on-reload gate;
 * there is no admission response to consult yet at this point. When found,
 * `hydrateFromResume` sets the session from the stored token and NO mint
 * fetch happens at all (decision 2 — reuse, not a new mint). When absent/
 * expired, the boot falls through to the untouched S14.1/S14.6 mint-with-
 * retry path below — a tenant that never opts in, or a fresh tab, sees
 * byte-for-byte the same sequence as before this sprint.
 */
import { loadConfig } from "./config";
import { mountWidget } from "./mount";
import { readResumeRecord } from "./resume";
import { getResumeSeed, hydrateFromResume, mintVisitorSession, type AdmissionResult } from "./session";
import { withRetry } from "./retry";
import { ChatWidget } from "./ui/ChatWidget";
import { DiagnosticStrip } from "./ui/DiagnosticStrip";

const LOG_PREFIX = "[chatbot-widget]";
/** Bounded boot-admission retry cap (decision 2) — small, matching the turn-retry cap in ChatWidget.tsx. */
const BOOT_ADMISSION_MAX_ATTEMPTS = 4;

async function boot(): Promise<void> {
  const configResult = loadConfig();

  if (!configResult.ok) {
    // Decision 3 step 1: a misconfigured embed must never render a broken
    // box on a client's live site — log loudly, mount nothing.
    console.error(`${LOG_PREFIX} ${configResult.error.message}`);
    return;
  }

  const { config } = configResult;

  // Mount the shadow host unconditionally once we have a valid config, so
  // the (opt-in) debug strip has a place to render on failure too.
  const { reactRoot } = mountWidget(config.mountSelector, config.position);

  // SR-3 decision 2/7: try resume BEFORE minting. A valid unexpired record
  // reuses the still-valid token — no fetch, no retry loop needed for it.
  const resumeRecord = readResumeRecord(new Date());
  if (resumeRecord) {
    hydrateFromResume(resumeRecord);
    const seed = getResumeSeed();
    console.info(`${LOG_PREFIX} resumed session from sessionStorage, conversation_id=${seed?.conversationId ?? "n/a"}`);
    reactRoot.render(
      <ChatWidget
        config={config}
        expiresAt={resumeRecord.expiresAt}
        resumeConversationId={seed?.conversationId ?? null}
      />,
    );
    return;
  }

  const admission = await withRetry<AdmissionResult>(() => mintVisitorSession(config), {
    maxAttempts: BOOT_ADMISSION_MAX_ATTEMPTS,
    onRetry: ({ attempt, error }) => {
      console.info(
        `${LOG_PREFIX} admission attempt ${attempt} failed (${error.errorCode}); retrying with backoff…`,
      );
    },
  });

  if (!admission.ok) {
    const { errorCode, message, correlationId, status } = admission.error;
    console.error(
      `${LOG_PREFIX} admission failed: ${errorCode} (status=${status ?? "n/a"}, correlation_id=${correlationId ?? "n/a"}): ${message}`,
    );
    // Decision 3 step 5: no user-facing UI on failure, never fake a
    // working widget. S14.6: the mint above already retried bounded
    // (transient network only, non-retryable errors like
    // INVALID_CLIENT_KEY/ORIGIN_NOT_ALLOWED/TENANT_DISABLED returned
    // immediately) — this is the honest hard-stop once that cap is hit.
    if (config.debug) {
      reactRoot.render(<DiagnosticStrip errorCode={errorCode} message={message} correlationId={correlationId} />);
    }
    return;
  }

  // S14.1 decision 3 step 4 / decision 5 success proof + S14.2 decision 1:
  // the real interactive chat root replaces the S14.1 placeholder at this
  // single render site. SR-3: a fresh mint never carries a resume seed
  // (resumeConversationId=null) -- ChatWidget starts a brand-new
  // conversation on the first turn, exactly as before this sprint.
  console.info(`${LOG_PREFIX} visitor session minted, expires_at=${admission.session.expiresAt}`);
  reactRoot.render(
    <ChatWidget
      config={config}
      expiresAt={admission.session.expiresAt}
      {...(admission.session.launcherLabel === undefined
        ? {}
        : { launcherLabel: admission.session.launcherLabel })}
      {...(admission.session.botName === undefined
        ? {}
        : { botName: admission.session.botName })}
      {...(admission.session.accentColor === undefined
        ? {}
        : { accentColor: admission.session.accentColor })}
      {...(admission.session.launcherPosition === undefined
        ? {}
        : { launcherPosition: admission.session.launcherPosition })}
      {...(admission.session.suggestedQuestions === undefined
        ? {}
        : { suggestedQuestions: admission.session.suggestedQuestions })}
      resumeConversationId={null}
    />,
  );
}

// Top-level side effect — the only one in this bundle. Never let a boot
// failure throw into the host page (decision 3 / CLAUDE.md no-silent-
// fallback + fail-invisible doctrine): any unexpected exception is caught
// and logged, not propagated.
void boot().catch((err: unknown) => {
  console.error(`${LOG_PREFIX} unexpected boot error:`, err);
});
