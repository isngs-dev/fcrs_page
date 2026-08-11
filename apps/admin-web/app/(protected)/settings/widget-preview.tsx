"use client";

/**
 * Live widget preview pane for the 6a Bot settings screen
 * (HANDOFF-SPEC.md §3 "6a Bot settings" + §2 "Widget (350×520 panel)" recipe,
 * §4 "Live preview in settings re-renders on every field change").
 *
 * This is a lightweight VISUAL MOCK of the widget's greeting screen, built
 * from Tailwind classes matching the ink & citron tokens -- it does NOT embed
 * the real widget bundle (`apps/widget`) or fetch anything. It only reflects
 * the CURRENT in-memory form field values passed down as props, so it is
 * honest about being an illustration ("as visitors see it" label, matching
 * the mock) rather than a production iframe. See `apps/widget/src/ui/*.tsx`
 * and `widgetCss.ts` for the real widget implementation this mirrors.
 */

const FALLBACK_BOT_NAME = "Your Assistant";
const FALLBACK_GREETING = "Hi! How can I help you today?";
const FALLBACK_LAUNCHER_LABEL = "Chat with us";

export function WidgetPreview({
  greeting,
  tone,
  launcherLabel,
}: {
  greeting: string;
  tone: string;
  launcherLabel: string;
}) {
  const displayGreeting = greeting.trim().length > 0 ? greeting : FALLBACK_GREETING;
  const displayLauncherLabel =
    launcherLabel.trim().length > 0 ? launcherLabel : FALLBACK_LAUNCHER_LABEL;

  return (
    <div className="flex w-full flex-col items-center gap-3.5">
      <div className="flex w-full items-center">
        <span className="text-[12.5px] font-bold text-foreground">Live preview</span>
        <span className="ml-auto text-[11px] text-muted-foreground">as visitors see it</span>
      </div>

      {/* SR-15 D1: every citron radial-gradient avatar dot in this preview
          is deleted and re-decided to a flat --secondary fill -- these are
          small decorative marks, not the design's one chromatic pair's use
          case (M3). */}
      <div className="flex w-[280px] flex-col overflow-hidden rounded-[18px] bg-card shadow-[0_12px_34px_rgba(28,27,25,.18)]">
        {/* Header */}
        <div className="flex items-center gap-2.5 bg-primary px-[15px] py-[13px] text-primary-foreground">
          <div aria-hidden className="size-[26px] shrink-0 rounded-full bg-primary-foreground/20" />
          <div className="min-w-0 flex-1">
            <p className="truncate text-[13px] font-bold">{FALLBACK_BOT_NAME}</p>
            <p className="text-[10px] text-primary-foreground/70">● Online</p>
          </div>
          <span className="text-xs text-primary-foreground/60" aria-hidden>
            ✕
          </span>
        </div>

        {/* Canvas / greeting */}
        <div className="flex flex-col items-center gap-3 bg-secondary px-[22px] py-[22px] text-center">
          <div aria-hidden className="size-12 rounded-full bg-primary/80" />
          <div>
            <p
              className="whitespace-pre-wrap text-[13px] leading-relaxed font-medium text-foreground"
              data-testid="widget-preview-greeting"
            >
              {displayGreeting}
            </p>
            {tone.trim().length > 0 ? (
              <p className="mt-1.5 text-[10.5px] text-muted-foreground">Tone: {tone}</p>
            ) : null}
          </div>
        </div>

        {/* Composer */}
        <div className="flex gap-2 border-t border-border bg-card p-[11px]">
          <div className="flex-1 rounded-full border border-border px-[13px] py-[9px] text-xs text-muted-foreground">
            Ask me anything…
          </div>
          <div
            aria-hidden
            className="grid size-[34px] shrink-0 place-items-center rounded-full bg-primary text-[13px] font-bold text-primary-foreground"
          >
            ↑
          </div>
        </div>
      </div>

      <div className="flex h-10 items-center gap-2 rounded-full bg-primary py-1.5 pl-1.5 pr-4 text-sm font-semibold text-primary-foreground shadow-[0_6px_16px_rgba(28,27,25,.18)]">
        <span aria-hidden className="size-7 shrink-0 rounded-full bg-primary-foreground/20" />
        <span className="max-w-[190px] truncate" data-testid="widget-preview-launcher-label">
          {displayLauncherLabel}
        </span>
      </div>
    </div>
  );
}
