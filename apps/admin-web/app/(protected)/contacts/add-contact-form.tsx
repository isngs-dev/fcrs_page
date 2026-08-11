"use client";

/**
 * "Add contact" form (SR-17 scope item 6). Rendered ONLY for `CLIENT_ADMIN`
 * -- the page never mounts this component for `CLIENT_AGENT` at all (D2:
 * hidden, not disabled). A simple toggle-open panel rather than a modal, to
 * avoid a second focus-trap implementation alongside the drawer's.
 */
import { useActionState, useState } from "react";
import { addContact, type AddContactState } from "@/app/(protected)/contacts/actions";
import { SoftCard } from "@/components/admin/soft-card";

const initialState: AddContactState = { status: "idle" };

export function AddContactForm() {
  const [open, setOpen] = useState(false);
  const [state, formAction, pending] = useActionState(addContact, initialState);

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="min-h-9 rounded-[9px] bg-primary px-3.5 py-2 text-[12.5px] font-semibold whitespace-nowrap text-primary-foreground hover:opacity-90"
      >
        + Add contact
      </button>
    );
  }

  return (
    <SoftCard className="flex w-full flex-col gap-3 p-5">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-bold text-foreground">Add contact</h2>
        <button
          type="button"
          onClick={() => setOpen(false)}
          aria-label="Close add contact form"
          className="text-xs text-muted-foreground underline underline-offset-2"
        >
          Cancel
        </button>
      </div>
      <form action={formAction} className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Name" name="name" />
        <Field label="Email" name="email" type="email" />
        <Field label="Phone" name="phone" />
        <Field label="Account ID (optional)" name="accountId" />
        <div className="col-span-full flex flex-col gap-2 rounded-lg border border-border bg-secondary p-3">
          <label className="flex items-start gap-2 text-[12.5px] text-[var(--ink-2)]">
            <input type="checkbox" name="consentGranted" required className="mt-0.5" />
            <span>I confirm this contact has given consent to store their information.</span>
          </label>
          <Field label="Consent purpose" name="consentPurpose" placeholder="e.g. CRM record" />
          <Field
            label="Consent text"
            name="consentText"
            placeholder="What the contact agreed to"
            textarea
          />
        </div>
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
          {pending ? "Saving…" : "Save contact"}
        </button>
      </form>
    </SoftCard>
  );
}

function Field({
  label,
  name,
  type = "text",
  placeholder,
  textarea = false,
}: {
  label: string;
  name: string;
  type?: string;
  placeholder?: string;
  textarea?: boolean;
}) {
  return (
    <label className="flex flex-col gap-1 text-[12.5px] text-[var(--ink-2)]">
      {label}
      {textarea ? (
        <textarea
          name={name}
          placeholder={placeholder}
          rows={2}
          className="rounded-lg border border-border bg-card p-2 text-[13px] text-foreground outline-none focus-visible:border-ring"
        />
      ) : (
        <input
          type={type}
          name={name}
          placeholder={placeholder}
          className="min-h-9 rounded-lg border border-border bg-card px-2.5 text-[13px] text-foreground outline-none focus-visible:border-ring"
        />
      )}
    </label>
  );
}
