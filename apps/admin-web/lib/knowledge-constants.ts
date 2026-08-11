/**
 * Shared file-constraint constants + helpers for the knowledge upload screen
 * (S13.3 decision 5). Sourced verbatim from the real backend values, not
 * invented:
 *  - `MAX_UPLOAD_BYTES` mirrors `ingestion_max_upload_bytes`
 *    (services/api/src/api/config.py:77).
 *  - `ALLOWED_CONTENT_TYPES` mirrors `_ALLOWED_CONTENT_TYPES`
 *    (services/api/src/api/ingestion/routes.py:39-42).
 * Used by both the client-side pre-check (upload-form.tsx) and the
 * server-action re-check (actions.ts) -- the backend remains the real,
 * authoritative gate in both cases.
 */

export const MAX_UPLOAD_BYTES = 10_485_760;

export const ALLOWED_CONTENT_TYPES = [
  "text/plain",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
] as const;

export const ALLOWED_EXTENSIONS = [".txt", ".docx"] as const;

/** `<input accept>` value: both extensions and MIME types, for maximum
 * browser compatibility (some browsers filter by extension, some by type). */
export const FILE_INPUT_ACCEPT = [...ALLOWED_EXTENSIONS, ...ALLOWED_CONTENT_TYPES].join(",");

/**
 * Run-status terminal-state predicate (S13.3 decision 4). The poll loop
 * stops once a run reaches `succeeded` or `failed`; `queued`/`running` are
 * non-terminal and keep polling.
 */
export function isTerminalRunStatus(status: string): boolean {
  return status === "succeeded" || status === "failed";
}

/**
 * Defensively render `ingestion_runs.errors`, which is loosely typed on the
 * backend as `list[Any] | dict[str, Any] | None` (repository.py:74, 427).
 * Never assumes a fixed shape; never throws. No-silent-fallback (CLAUDE.md
 * §3): this is what actually surfaces the real recorded error to the admin.
 */
export function formatRunErrors(errors: unknown): string {
  if (errors === null || errors === undefined) {
    return "Ingestion failed, but no error detail was recorded.";
  }

  if (Array.isArray(errors)) {
    if (errors.length === 0) {
      return "Ingestion failed, but no error detail was recorded.";
    }
    return errors
      .map((entry) => (typeof entry === "string" ? entry : safeStringify(entry)))
      .join("\n");
  }

  if (typeof errors === "object") {
    return safeStringify(errors);
  }

  return String(errors);
}

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** Human-readable MiB rendering for size-limit messages. */
export function formatBytes(bytes: number): string {
  const mib = bytes / (1024 * 1024);
  return `${mib.toFixed(mib < 10 ? 1 : 0)} MiB`;
}

/**
 * Status badge recipe for the 5a design (HANDOFF-SPEC.md §2 Badges / §3
 * "5a Knowledge"). The mock shows INDEXED/PROCESSING/FAILED; the REAL
 * backend enum is different on both fields (see upload-form.tsx's header
 * comment for the full trace):
 *  - doc.status ∈ {"pending", "parsed", "failed"}
 *    (services/api/src/api/ingestion/repository.py:128, tasks.py:215,282,398)
 *  - run.status ∈ {"queued", "running", "succeeded", "failed"}
 *    (repository.py:244, tasks.py:181,211,246,277,319,353,393)
 *
 * This maps every REAL value honestly onto the 5a visual language (success
 * green = done, monochrome = in progress, danger red = failed -- SR-15
 * restyle: "progress" no longer uses the Ink & Citron era's soft-citron
 * tint, it uses the shared --secondary/--ink-2 pair, matching M3's finding
 * that the new system's only two chromatic values are success and danger)
 * rather than collapsing them into the mock's three invented labels.
 * Nothing here invents a status the backend doesn't report.
 */
export type BadgeTone = "success" | "progress" | "failed" | "neutral";

export interface StatusBadgeSpec {
  label: string;
  tone: BadgeTone;
}

const TONE_CLASSES: Record<BadgeTone, string> = {
  success: "bg-[#eaf3ec] text-[#3f7d57]",
  progress: "bg-secondary text-foreground",
  failed: "bg-[#f6e3df] text-[#a24b4b]",
  neutral: "bg-secondary text-[var(--ink-2)]",
};

export function badgeToneClassName(tone: BadgeTone): string {
  return TONE_CLASSES[tone];
}

/**
 * Combines doc.status + the latest run's status (if any) into one honest
 * badge. Run status is more granular/current while a run is in flight;
 * doc.status is the resting state once a run completes (or if none has run
 * yet). Falls back to rendering the raw string for any unrecognized value
 * instead of silently hiding it (CLAUDE.md §3 no-silent-fallback).
 */
export function statusBadge(docStatus: string, runStatus: string | null): StatusBadgeSpec {
  if (runStatus === "running") return { label: "Processing", tone: "progress" };
  if (runStatus === "queued") return { label: "Queued", tone: "neutral" };
  if (runStatus === "failed" || docStatus === "failed") return { label: "Failed", tone: "failed" };
  if (runStatus === "succeeded" || docStatus === "parsed") return { label: "Indexed", tone: "success" };
  if (docStatus === "pending") return { label: "Pending", tone: "neutral" };

  // Defensive fallback: surface the real value rather than hide it.
  return { label: docStatus, tone: "neutral" };
}

/**
 * Pure, framework-agnostic polling driver (S13.3 decision 4). Extracted out
 * of the status-panel React component so the stop-on-terminal-state and
 * poll-cap behavior is unit-testable with fake timers without a DOM/RTL
 * dependency (this repo has neither wired up).
 *
 * Calls `pollOnce()` immediately, then every `intervalMs`. After each result,
 * `onResult` is invoked with the result and the 1-based poll count so far.
 * Polling stops automatically -- clearing its own interval -- the moment
 * `isTerminal(result)` is true, or once `maxPolls` attempts have completed
 * without a terminal result (in which case `onCapped()` fires once). Returns
 * a `stop()` function for the caller's cleanup (e.g. a React effect's
 * unmount), safe to call multiple times.
 */
export function schedulePolling<T>(opts: {
  pollOnce: () => Promise<T>;
  isTerminal: (result: T) => boolean;
  onResult: (result: T, pollCount: number) => void;
  onCapped?: () => void;
  intervalMs: number;
  maxPolls: number;
}): () => void {
  let stopped = false;
  let count = 0;
  let timer: ReturnType<typeof setInterval> | null = null;

  function stop(): void {
    if (stopped) return;
    stopped = true;
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  async function tick(): Promise<void> {
    if (stopped) return;
    const result = await opts.pollOnce();
    if (stopped) return;
    count += 1;
    opts.onResult(result, count);
    if (opts.isTerminal(result)) {
      stop();
      return;
    }
    if (count >= opts.maxPolls) {
      opts.onCapped?.();
      stop();
    }
  }

  void tick();
  timer = setInterval(() => {
    void tick();
  }, opts.intervalMs);

  return stop;
}
