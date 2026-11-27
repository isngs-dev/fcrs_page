/**
 * Onboarding checklist -- computed live from data three EXISTING admin-web
 * functions already fetch (`getBotSettings`, `listKnowledgeDocs`,
 * `getCallConfig`), never a stored/persisted status. The tenant model has
 * no lifecycle/status column at all (just `enabled`), and the tenant list
 * page's own mockup explicitly wants an "onboarding checklist card" that
 * was deliberately never built for lack of real backend data to back it --
 * this fills that gap honestly rather than fabricating a status that could
 * drift from reality (an admin checking a box without doing the work, or
 * doing the work and never checking it).
 *
 * Presentational only -- no "use client", no fetching of its own, just
 * links -- so every server component that renders it stays a server
 * component (admin-web skill: server-first), mirroring `SoftCard`'s own
 * doc comment on the same point.
 *
 * `isOnboardingComplete` is exported standalone (not just used internally)
 * so a Server Component can decide whether to render this at all, the same
 * reason `shouldShowSourcesAffordance` was split out of `message-sources.tsx`
 * into its own plain-function module.
 */
import Link from "next/link";
import { CircleCheck, Circle, ArrowRight } from "lucide-react";
import { SoftCard } from "@/components/admin/soft-card";
import type { SettingsResult } from "@/lib/settings";
import type { ListKnowledgeResult } from "@/app/(protected)/knowledge/actions";
import type { CallConfigResult } from "@/lib/calls";

function isSettingsCustomized(result: SettingsResult): boolean {
  if (result.status !== "ok") return false;
  const { settings } = result;
  return Boolean(
    settings.greeting?.trim() ||
      settings.tone?.trim() ||
      settings.escalationPolicy?.trim() ||
      (settings.businessHours && Object.keys(settings.businessHours).length > 0)
  );
}

function hasKnowledgeUploaded(result: ListKnowledgeResult): boolean {
  return result.status === "ok" && result.docs.length > 0;
}

function isMissedCallConfigured(result: CallConfigResult): boolean {
  return result.status === "ok" && Boolean(result.config.monitoredPhoneNumber) && result.config.enabled;
}

/** "Fully set up" only requires the two non-optional items -- missed-call
 * text-back is explicitly optional and never gates this. */
export function isOnboardingComplete(
  settingsResult: SettingsResult,
  docsResult: ListKnowledgeResult
): boolean {
  return isSettingsCustomized(settingsResult) && hasKnowledgeUploaded(docsResult);
}

function ChecklistItem({
  step,
  done,
  label: itemLabel,
  href,
  linkLabel,
}: {
  step: number;
  done: boolean;
  label: string;
  href: string;
  linkLabel: string;
}) {
  return (
    <li
      className={`flex items-center justify-between gap-3 rounded-[10px] border p-3.5 sm:min-h-[92px] sm:flex-col sm:items-stretch ${
        done ? "border-[var(--success-bg)] bg-[var(--success-bg)]/50" : "border-border bg-card"
      }`}
    >
      <span className="flex items-start gap-2.5 text-[13.5px] leading-snug">
        {done ? (
          <CircleCheck aria-hidden className="mt-px size-[18px] shrink-0 text-[var(--success-fg)]" />
        ) : (
          <span
            aria-hidden
            className="grid size-[18px] shrink-0 place-items-center rounded-full border border-input text-[10px] font-semibold text-muted-foreground"
          >
            {step}
          </span>
        )}
        <span className={done ? "text-muted-foreground line-through decoration-muted-foreground/50" : "font-medium text-foreground"}>
          {itemLabel}
        </span>
      </span>
      {!done ? (
        <Link
          href={href}
          className="flex w-fit shrink-0 items-center gap-1 text-[12.5px] font-semibold whitespace-nowrap text-primary hover:text-clay"
        >
          {linkLabel}
          <ArrowRight aria-hidden className="size-3.5" />
        </Link>
      ) : (
        <span className="text-[12px] font-medium text-[var(--success-fg)]">Done</span>
      )}
    </li>
  );
}

/**
 * Pine & Clay (owner, 2026-11-27): a compact step strip -- the two essentials with a
 * progress bar, then testing and the optional missed-call step -- in place of a tall list.
 */
export function OnboardingChecklist({
  settingsResult,
  docsResult,
  callConfigResult,
  settingsHref,
  knowledgeHref,
}: {
  settingsResult: SettingsResult;
  docsResult: ListKnowledgeResult;
  callConfigResult: CallConfigResult;
  settingsHref: string;
  knowledgeHref: string;
}) {
  const settingsDone = isSettingsCustomized(settingsResult);
  const knowledgeDone = hasKnowledgeUploaded(docsResult);
  const missedCallDone = isMissedCallConfigured(callConfigResult);
  const requiredDone = settingsDone && knowledgeDone;
  const essentialsDone = Number(settingsDone) + Number(knowledgeDone);

  return (
    <SoftCard className="flex flex-col gap-4 p-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-[15px] font-semibold text-foreground">
            {requiredDone ? "Your bot is set up" : "Get your bot ready"}
          </p>
          <p className="mt-0.5 text-[12.5px] text-muted-foreground">
            {requiredDone
              ? "The essentials are done. Missed-call text-back is optional, and you can always come back to test the bot."
              : "A few quick steps before this bot is ready for real visitors."}
          </p>
        </div>
        <div className="flex min-w-[180px] flex-col gap-1.5">
          <span className="tabular text-[12px] font-medium text-[var(--ink-2)]">
            {essentialsDone} of 2 essentials done
          </span>
          <span aria-hidden className="h-1.5 overflow-hidden rounded-full bg-secondary">
            <span
              className="block h-full rounded-full bg-primary transition-[width] duration-500"
              style={{ width: `${essentialsDone * 50}%` }}
            />
          </span>
        </div>
      </div>
      <ul className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-4">
        <ChecklistItem
          step={1}
          done={settingsDone}
          label="Customize your bot"
          href={settingsHref}
          linkLabel="Go to settings"
        />
        <ChecklistItem
          step={2}
          done={knowledgeDone}
          label="Upload your knowledge base"
          href={knowledgeHref}
          linkLabel="Upload documents"
        />
        <li className="flex items-center justify-between gap-3 rounded-[10px] border border-border bg-card p-3.5 sm:min-h-[92px] sm:flex-col sm:items-stretch">
          <span className="flex items-start gap-2.5 text-[13.5px] leading-snug font-medium text-foreground">
            <Circle aria-hidden className="mt-px size-[18px] shrink-0 text-muted-foreground" />
            Test your bot
          </span>
          <Link
            href={knowledgeHref}
            className="flex w-fit shrink-0 items-center gap-1 text-[12.5px] font-semibold whitespace-nowrap text-primary hover:text-clay"
          >
            Try it now
            <ArrowRight aria-hidden className="size-3.5" />
          </Link>
        </li>
        <ChecklistItem
          step={4}
          done={missedCallDone}
          label="Set up missed-call text-back (optional)"
          href={settingsHref}
          linkLabel="Set up"
        />
      </ul>
    </SoftCard>
  );
}
