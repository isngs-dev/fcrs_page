"use client";

/**
 * "Add member" action (7b's "+ Invite member" button, relabeled honestly --
 * there is no invite/accept flow, `POST /admin/users` creates the
 * `CLIENT_AGENT` immediately with a server-generated one-time password).
 * Opens a modal form; on success shows the one-time temp password exactly
 * once, following the secrets-hygiene precedent in
 * `tenants/new/onboard-form.tsx` (`ResultView`/`CopyButton`/ack-before-close).
 */
import { useActionState, useId, useState } from "react";
import { useFormStatus } from "react-dom";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { createMemberAction, type CreateMemberState } from "@/app/(protected)/members/actions";

const initialState: CreateMemberState = { status: "idle" };

function SubmitButton() {
  const { pending } = useFormStatus();
  return (
    <Button type="submit" className="w-full" disabled={pending}>
      {pending ? "Adding member…" : "Add member"}
    </Button>
  );
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const [status, setStatus] = useState<"idle" | "copied" | "unavailable">("idle");

  async function handleCopy() {
    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(value);
        setStatus("copied");
        setTimeout(() => setStatus("idle"), 2000);
        return;
      } catch {
        // fall through to the unavailable fallback below
      }
    }
    setStatus("unavailable");
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <Button type="button" variant="outline" size="sm" onClick={handleCopy}>
        {status === "copied" ? "Copied!" : `Copy ${label}`}
      </Button>
      {status === "unavailable" ? (
        <p className="text-xs text-muted-foreground">
          Clipboard unavailable — select the text above and copy manually.
        </p>
      ) : null}
    </div>
  );
}

function ResultView({
  state,
  onDone,
}: {
  state: Extract<CreateMemberState, { status: "created" }>;
  onDone: () => void;
}) {
  const ackId = useId();
  const [acknowledged, setAcknowledged] = useState(false);

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm">
        <span className="font-medium">{state.member.name ?? state.member.email}</span> was added
        as an agent.
      </p>

      <div className="flex flex-col gap-3 rounded-md border border-destructive/40 bg-destructive/5 p-4">
        <p role="alert" className="text-sm font-medium text-destructive">
          Shown once — this temporary password is not recoverable later. Save it now.
        </p>
        <div className="flex flex-col gap-1.5">
          <Label>Temporary password</Label>
          <div className="flex items-center gap-2">
            <code className="flex-1 select-all overflow-x-auto rounded-md border border-input bg-muted px-2.5 py-1.5 text-sm">
              {state.tempPassword}
            </code>
            <CopyButton value={state.tempPassword} label="password" />
          </div>
        </div>
      </div>

      <div className="flex items-start gap-2">
        <Checkbox
          id={ackId}
          checked={acknowledged}
          onCheckedChange={(checked) => setAcknowledged(checked)}
        />
        <Label htmlFor={ackId} className="font-normal">
          I have saved the temporary password in a secure place.
        </Label>
      </div>

      <Button type="button" disabled={!acknowledged} onClick={onDone}>
        Done
      </Button>
    </div>
  );
}

export function CreateMemberDialog() {
  const [open, setOpen] = useState(false);
  const [state, formAction] = useActionState(createMemberAction, initialState);

  function close() {
    setOpen(false);
  }

  return (
    <>
      {/* .btn-dark recipe (Console.dc.html:40-42), same exact classes as
          `leads/page.tsx`'s "Export CSV" button: h:38px, radius:10px,
          13.5px/600, near-black fill / cream text, +-icon leading. */}
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex h-[38px] items-center gap-2 rounded-[10px] bg-[#333333] px-4 text-[13.5px] font-semibold whitespace-nowrap text-[#fbfaf7] transition-colors hover:bg-[#404040] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
      >
        <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round">
          <path d="M12 5v14M5 12h14" />
        </svg>
        Add member
      </button>

      {open ? (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="create-member-title"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
        >
          <div className="flex w-full max-w-sm flex-col gap-4 rounded-2xl border border-[var(--border)] bg-white p-5 shadow-xl">
            <div className="flex items-center justify-between">
              <h2 id="create-member-title" className="text-[15px] font-bold text-[var(--foreground)]">
                Add team member
              </h2>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label="Close"
                onClick={close}
              >
                ✕
              </Button>
            </div>

            {state.status === "created" ? (
              <ResultView state={state} onDone={close} />
            ) : (
              <form action={formAction} className="flex flex-col gap-4">
                <p className="text-xs text-muted-foreground">
                  Creates a new agent immediately with a server-generated temporary password —
                  there is no invite/accept step. They can review leads and conversations, but
                  cannot change tenant configuration.
                </p>

                <div className="flex flex-col gap-2">
                  <Label htmlFor="member-email">Email</Label>
                  <Input
                    id="member-email"
                    name="email"
                    type="email"
                    required
                    placeholder="agent@acme.example"
                  />
                  {state.status === "error" && state.fieldErrors.email ? (
                    <p role="alert" className="text-sm text-destructive">
                      {state.fieldErrors.email}
                    </p>
                  ) : null}
                </div>

                <div className="flex flex-col gap-2">
                  <Label htmlFor="member-name">Name (optional)</Label>
                  <Input id="member-name" name="name" placeholder="Jane Doe" />
                  {state.status === "error" && state.fieldErrors.name ? (
                    <p role="alert" className="text-sm text-destructive">
                      {state.fieldErrors.name}
                    </p>
                  ) : null}
                </div>

                {state.status === "error" && state.formError ? (
                  <p role="alert" className="text-sm text-destructive">
                    {state.formError}
                  </p>
                ) : null}

                <SubmitButton />
              </form>
            )}
          </div>
        </div>
      ) : null}
    </>
  );
}
