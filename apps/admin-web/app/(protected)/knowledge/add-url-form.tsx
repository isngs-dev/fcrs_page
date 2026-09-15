"use client";

/**
 * Add-a-website form (add-a-website knowledge source feature). Sibling to
 * `<UploadForm>` on the same `/knowledge` page -- a URL input instead of a
 * file dropzone, posting to `POST /admin/ingestion/url` via `addKnowledgeUrl`
 * (`actions.ts`). Reuses the exported `<StatusPanel>` from `upload-form.tsx`
 * unchanged for the live queued/running/succeeded polling UI, since that
 * component is already fully generic over docId/runId/docStatus/idempotent
 * and doesn't care which source produced them.
 *
 * CLIENT_ADMIN-only: this whole page is already gated by `requireRole
 * ("CLIENT_ADMIN")` in `page.tsx`, so no new role check is needed here.
 * Deliberately no `tenantId` prop/bind (unlike `<UploadForm>`) -- there is
 * no PLATFORM_ADMIN tenant-scoped mirror route for this endpoint (S13.7:
 * platform admins don't get new knowledge-mutation capability).
 */
import { useId, useState, type FormEvent } from "react";
import { useActionState } from "react";
import { useFormStatus } from "react-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { addKnowledgeUrl, type UploadState } from "@/app/(protected)/knowledge/actions";
import { StatusPanel } from "@/app/(protected)/knowledge/upload-form";

const initialState: UploadState = { status: "idle" };

const URL_SHAPE_PATTERN = /^https?:\/\/[^\s]+$/i;

function SubmitButton() {
  const { pending } = useFormStatus();
  return (
    <Button
      type="submit"
      disabled={pending}
      className="mt-4 h-11 w-full justify-center rounded-[10px] bg-primary font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-60"
    >
      {pending ? "Adding…" : "Add website"}
    </Button>
  );
}

export function AddUrlForm() {
  const [state, formAction] = useActionState(addKnowledgeUrl, initialState);
  const [clientError, setClientError] = useState<string | null>(null);
  const urlId = useId();
  const titleId = useId();
  const descriptionId = useId();

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    const input = event.currentTarget.elements.namedItem("url");
    const value = input instanceof HTMLInputElement ? input.value.trim() : "";

    if (!value || !URL_SHAPE_PATTERN.test(value)) {
      event.preventDefault();
      setClientError("Enter a valid http:// or https:// URL.");
      return;
    }

    setClientError(null);
  }

  if (state.status === "uploaded") {
    return (
      <StatusPanel
        docId={state.docId}
        initialRunId={state.runId}
        initialDocStatus={state.docStatus}
        idempotent={state.idempotent}
      />
    );
  }

  return (
    <form action={formAction} onSubmit={handleSubmit} className="flex flex-col gap-4">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={urlId}>Website URL</Label>
        <Input
          id={urlId}
          name="url"
          type="url"
          placeholder="https://example.com/pricing"
          required
        />
        <p className="text-[12px] text-muted-foreground">
          The page is fetched once and its text is added to your knowledge base. Add more URLs
          for more pages.
        </p>
      </div>

      <div className="flex flex-col gap-1.5">
        <Label htmlFor={titleId}>Title (optional)</Label>
        <Input id={titleId} name="title" placeholder="Falls back to the page URL when left blank" />
      </div>

      <div className="flex flex-col gap-1.5">
        <Label htmlFor={descriptionId}>Description (optional)</Label>
        <Textarea id={descriptionId} name="description" rows={2} placeholder="What's on this page?" />
      </div>

      {clientError ? (
        <p role="alert" className="text-[12.5px] font-medium text-[var(--danger-fg)]">
          {clientError}
        </p>
      ) : null}

      {state.status === "error" ? (
        <p role="alert" className="text-[12.5px] font-medium text-[var(--danger-fg)]">
          {state.message}
        </p>
      ) : null}

      <SubmitButton />
    </form>
  );
}
