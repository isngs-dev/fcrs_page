"use client";

/**
 * "Add a chatbot" form -- used by the dedicated `/clients/new` screen.
 * Binds to `clients/actions.ts`'s `onboardNewClient`, which revalidates
 * `/clients` and lands the admin on the new tenant's own settings page
 * (where its embed script already lives, via `InstallSnippet`).
 *
 * Was previously also duplicated inline on the Clients page and at the
 * now-deleted `/tenants/new` route (a near-identical form bound to a
 * divergent action that landed on `/`) -- both were consolidated into this
 * one form + its one dedicated page rather than left as multiple ways to do
 * the same thing.
 */
import { useActionState, useId, useState } from "react";
import { useFormStatus } from "react-dom";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { onboardNewClient, type OnboardState } from "@/app/(protected)/clients/actions";

const initialState: OnboardState = { status: "idle" };

function SubmitButton() {
  const { pending } = useFormStatus();
  return (
    <Button type="submit" className="w-full" disabled={pending}>
      {pending ? "Creating chatbot..." : "Create chatbot"}
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

function SecretRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label>{label}</Label>
      <div className="flex items-center gap-2">
        <code className="flex-1 select-all overflow-x-auto rounded-md border border-input bg-muted px-2.5 py-1.5 text-sm">
          {value}
        </code>
        <CopyButton value={value} label={label} />
      </div>
    </div>
  );
}

function ResultView({ state }: { state: Extract<OnboardState, { status: "created" }> }) {
  const ackId = useId();
  const [acknowledged, setAcknowledged] = useState(false);

  const copyAllText = [
    `Client: ${state.tenant.name} (${state.tenant.slug})`,
    `Tenant ID: ${state.tenant.tenantId}`,
    `Admin email: ${state.tenant.adminEmail}`,
    `Client key: ${state.clientKey}`,
    state.generatedPassword ? `Admin password: ${state.generatedPassword}` : null,
  ]
    .filter(Boolean)
    .join("\n");

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <p className="text-sm">
          Chatbot for <span className="font-medium">{state.tenant.name}</span> (
          <span className="font-mono">{state.tenant.slug}</span>) created. Admin user{" "}
          <span className="font-medium">{state.tenant.adminEmail}</span>.
        </p>
      </div>

      <Card className="border-destructive/40 bg-destructive/5">
        <CardContent className="flex flex-col gap-4 pt-4">
          <p role="alert" className="text-sm font-medium text-destructive">
            Shown once — this key{state.generatedPassword ? " and password are" : " is"} not
            recoverable later. Save {state.generatedPassword ? "them" : "it"} now.
          </p>

          <SecretRow label="client key" value={state.clientKey} />

          {state.generatedPassword ? (
            <SecretRow label="admin password" value={state.generatedPassword} />
          ) : (
            <p className="text-sm text-muted-foreground">
              You supplied the admin&apos;s password directly — it is not echoed back here.
            </p>
          )}

          <div className="flex justify-end">
            <CopyButton value={copyAllText} label="all" />
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
          I have saved the client key{state.generatedPassword ? " and password" : ""} in a secure
          place.
        </Label>
      </div>

      <Button
        type="button"
        disabled={!acknowledged}
        onClick={() => window.location.assign(`/clients/${state.tenant.tenantId}/analytics`)}
      >
        Done — go to {state.tenant.name}
      </Button>
    </div>
  );
}

export function OnboardClientForm() {
  const [state, formAction] = useActionState(onboardNewClient, initialState);
  const [autoGenerate, setAutoGenerate] = useState(true);

  if (state.status === "created") {
    return <ResultView state={state} />;
  }

  const fieldErrors = state.status === "error" ? state.fieldErrors : {};
  const formError = state.status === "error" ? state.formError : null;
  const partialCreationWarning = state.status === "error" ? state.partialCreationWarning : null;

  return (
    <form action={formAction} className="flex flex-col gap-4">
      {partialCreationWarning ? (
        <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
          {partialCreationWarning}
        </p>
      ) : null}

      <div className="flex flex-col gap-2">
        <Label htmlFor="name">Client name</Label>
        <Input id="name" name="name" required placeholder="Acme Corp" />
        {fieldErrors.name ? (
          <p role="alert" className="text-sm text-destructive">
            {fieldErrors.name}
          </p>
        ) : null}
      </div>

      <div className="flex flex-col gap-2">
        <Label htmlFor="slug">URL slug</Label>
        <Input id="slug" name="slug" required placeholder="acme-corp" />
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

      <div className="flex flex-col gap-2">
        <Label htmlFor="adminEmail">Admin email</Label>
        <Input
          id="adminEmail"
          name="adminEmail"
          type="email"
          required
          placeholder="admin@acme.example"
        />
        {fieldErrors.adminEmail ? (
          <p role="alert" className="text-sm text-destructive">
            {fieldErrors.adminEmail}
          </p>
        ) : null}
      </div>

      <div className="flex flex-col gap-2">
        <Label htmlFor="adminName">Admin name (optional)</Label>
        <Input id="adminName" name="adminName" placeholder="Jane Doe" />
        {fieldErrors.adminName ? (
          <p role="alert" className="text-sm text-destructive">
            {fieldErrors.adminName}
          </p>
        ) : null}
      </div>

      <div className="flex items-start gap-2">
        <Checkbox
          id="autoGeneratePassword"
          name="autoGeneratePassword"
          checked={autoGenerate}
          onCheckedChange={(checked) => setAutoGenerate(checked)}
        />
        <Label htmlFor="autoGeneratePassword" className="font-normal">
          Auto-generate a secure password
        </Label>
      </div>

      {!autoGenerate ? (
        <div className="flex flex-col gap-2">
          <Label htmlFor="adminPassword">Admin password</Label>
          <Input
            id="adminPassword"
            name="adminPassword"
            type="password"
            autoComplete="new-password"
            required
          />
          {fieldErrors.adminPassword ? (
            <p role="alert" className="text-sm text-destructive">
              {fieldErrors.adminPassword}
            </p>
          ) : (
            <p className="text-xs text-muted-foreground">At least 12 characters.</p>
          )}
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">
          The server will generate a password and show it once, alongside the client key, after
          creation.
        </p>
      )}

      {formError ? (
        <p role="alert" className="text-sm text-destructive">
          {formError}
        </p>
      ) : null}

      <SubmitButton />
    </form>
  );
}
