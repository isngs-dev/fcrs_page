"use client";

/**
 * "Add account" form (SR-17 scope item 6). Rendered ONLY for `CLIENT_ADMIN`
 * (D2). No edit form anywhere in this directory -- there is no
 * `PATCH /admin/accounts/{id}` (M3).
 */
import { useActionState, useState } from "react";
import { addAccount, type AddAccountState } from "@/app/(protected)/accounts/actions";
import { SoftCard } from "@/components/admin/soft-card";

const initialState: AddAccountState = { status: "idle" };

export function AddAccountForm() {
  const [open, setOpen] = useState(false);
  const [state, formAction, pending] = useActionState(addAccount, initialState);

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="min-h-9 rounded-[9px] bg-primary px-3.5 py-2 text-[12.5px] font-semibold whitespace-nowrap text-primary-foreground hover:opacity-90"
      >
        + Add account
      </button>
    );
  }

  return (
    <SoftCard className="flex w-full flex-col gap-3 p-5">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-bold text-foreground">Add account</h2>
        <button
          type="button"
          onClick={() => setOpen(false)}
          aria-label="Close add account form"
          className="text-xs text-muted-foreground underline underline-offset-2"
        >
          Cancel
        </button>
      </div>
      <form action={formAction} className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-[12.5px] text-[var(--ink-2)]">
          Name
          <input
            type="text"
            name="name"
            required
            className="min-h-9 rounded-lg border border-border bg-card px-2.5 text-[13px] text-foreground outline-none focus-visible:border-ring"
          />
        </label>
        <label className="flex flex-col gap-1 text-[12.5px] text-[var(--ink-2)]">
          Domain (optional)
          <input
            type="text"
            name="domain"
            placeholder="example.com"
            className="min-h-9 rounded-lg border border-border bg-card px-2.5 text-[13px] text-foreground outline-none focus-visible:border-ring"
          />
        </label>
        {state.status === "error" ? (
          <p role="alert" className="col-span-full text-xs text-[var(--danger-fg)]">
            {state.message}
          </p>
        ) : null}
        <button
          type="submit"
          disabled={pending}
          className="col-span-full min-h-10 self-start rounded-lg bg-primary px-4 text-[12.5px] font-bold text-primary-foreground transition-opacity disabled:opacity-50"
        >
          {pending ? "Saving…" : "Save account"}
        </button>
      </form>
    </SoftCard>
  );
}
