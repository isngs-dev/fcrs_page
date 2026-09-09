"use client";

/**
 * "Add a chatbot" self-service form -- used by `/chatbots/new`. Binds to
 * `chatbots/actions.ts`'s `createChatbotAction`. Mirrors
 * `clients/onboard-client-form.tsx`'s styling, minus the admin-user/
 * password fields (this action creates no user).
 */
import { useActionState, useId, useState } from "react";
import { useFormStatus } from "react-dom";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { createChatbotAction, type CreateChatbotState } from "@/app/(protected)/chatbots/actions";

const initialState: CreateChatbotState = { status: "idle" };

function SubmitButton() {
  const { pending } = useFormStatus();
  return (
    <Button type="submit" className="w-full" disabled={pending}>
      {pending ? "Creating chatbot..." : "Create chatbot"}
    </Button>
  );
}

function CopyButton({ value }: { value: string }) {
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
        {status === "copied" ? "Copied!" : "Copy client key"}
      </Button>
      {status === "unavailable" ? (
        <p className="text-xs text-muted-foreground">
          Clipboard unavailable — select the text above and copy manually.
        </p>
      ) : null}
    </div>
  );
}

function ResultView({ state }: { state: Extract<CreateChatbotState, { status: "created" }> }) {
  const ackId = useId();
  const [acknowledged, setAcknowledged] = useState(false);

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm">
        Chatbot <span className="font-medium">{state.name}</span> (
        <span className="font-mono">{state.slug}</span>) created — every member of your account
        can now switch to it.
      </p>

      <Card className="border-destructive/40 bg-destructive/5">
        <CardContent className="flex flex-col gap-4 pt-4">
          <p role="alert" className="text-sm font-medium text-destructive">
            Shown once — this client key is not recoverable later. Save it now.
          </p>
          <div className="flex flex-col gap-1.5">
            <Label>Client key</Label>
            <div className="flex items-center gap-2">
              <code className="flex-1 select-all overflow-x-auto rounded-md border border-input bg-muted px-2.5 py-1.5 text-sm">
                {state.clientKey}
              </code>
              <CopyButton value={state.clientKey} />
            </div>
          </div>
        </CardContent>
      </Card>

      <div className="flex items-start gap-2">
        <Checkbox
          id={ackId}
          checked={acknowledged}
          onCheckedChange={(checked) => setAcknowledged(checked)}
        />
        <Label htmlFor={ackId} className="font-normal">
          I have saved the client key in a secure place.
        </Label>
      </div>

      <Button
        type="button"
        disabled={!acknowledged}
        onClick={() => window.location.assign("/chatbots")}
      >
        Done — go to my chatbots
      </Button>
    </div>
  );
}

export function CreateChatbotForm() {
  const [state, formAction] = useActionState(createChatbotAction, initialState);

  if (state.status === "created") {
    return <ResultView state={state} />;
  }

  const fieldErrors = state.status === "error" ? state.fieldErrors : {};
  const formError = state.status === "error" ? state.formError : null;

  return (
    <form action={formAction} className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        <Label htmlFor="name">Chatbot name</Label>
        <Input id="name" name="name" required placeholder="Roofing Leads Bot" />
        {fieldErrors.name ? (
          <p role="alert" className="text-sm text-destructive">
            {fieldErrors.name}
          </p>
        ) : null}
      </div>

      <div className="flex flex-col gap-2">
        <Label htmlFor="slug">URL slug</Label>
        <Input id="slug" name="slug" required placeholder="roofing-leads-bot" />
        {fieldErrors.slug ? (
          <p role="alert" className="text-sm text-destructive">
            {fieldErrors.slug}
          </p>
        ) : (
          <p className="text-xs text-muted-foreground">
            Lowercase letters, numbers, and single hyphens only.
          </p>
        )}
      </div>

      {formError ? (
        <p role="alert" className="text-sm text-destructive">
          {formError}
        </p>
      ) : null}

      <SubmitButton />
    </form>
  );
}
