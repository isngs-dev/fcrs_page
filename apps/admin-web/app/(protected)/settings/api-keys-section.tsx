"use client";

/**
 * API-keys settings section (SR-20 D6). Rotate the tenant's own `pk_`
 * client key (show-once reveal, explicit confirmation naming the real
 * consequence -- rotation breaks every embedded widget until the script
 * tag is updated), and edit the Origin allowlist (a live admission
 * control -- emptying it disables the widget entirely, a real, documented
 * consequence per D6).
 *
 * The CURRENT key is never displayed (the backend cannot return it -- only
 * a hash is stored, M6) -- this section shows only whether a key exists.
 */
import { useActionState, useState, useTransition } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { SoftCard } from "@/components/admin/soft-card";
import {
  rotateApiKey,
  updateOrigins,
  type UpdateOriginsState,
} from "@/app/(protected)/settings/api-keys-actions";
import type { ApiKeyInfo } from "@/lib/api-keys";

const initialOriginsState: UpdateOriginsState = { status: "idle" };

export function ApiKeysSection({ currentInfo }: { currentInfo: ApiKeyInfo }) {
  const [hasKey, setHasKey] = useState(currentInfo.hasKey);
  const [confirmingRotate, setConfirmingRotate] = useState(false);
  const [revealedKey, setRevealedKey] = useState<string | null>(null);
  const [rotateError, setRotateError] = useState<string | null>(null);
  const [isRotating, startRotate] = useTransition();

  const [originsState, originsAction] = useActionState(updateOrigins, initialOriginsState);
  const [originsText, setOriginsText] = useState(currentInfo.allowedOrigins.join("\n"));

  function handleRotateConfirmed() {
    setRotateError(null);
    startRotate(async () => {
      const result = await rotateApiKey();
      if (result.status !== "rotated") {
        setRotateError(
          result.status === "error" ? result.message : "Could not rotate the client key."
        );
        setConfirmingRotate(false);
        return;
      }
      setRevealedKey(result.clientKey);
      setHasKey(true);
      setConfirmingRotate(false);
    });
  }

  const displayedOrigins =
    originsState.status === "saved" ? originsState.allowedOrigins : currentInfo.allowedOrigins;

  return (
    <SoftCard className="flex scroll-mt-16 flex-col gap-5 p-5" id="settings-api-keys">
      <h2 className="text-sm font-bold text-foreground">API keys</h2>

      <div className="flex flex-col gap-3">
        <p className="text-sm font-medium text-foreground">Client key</p>
        <p className="text-xs text-muted-foreground">
          {hasKey
            ? "A client key is configured for this workspace's embedded widget. The key itself is never shown again after rotation — only a fresh rotation reveals a new one."
            : "No client key has been generated yet."}
        </p>

        {revealedKey ? (
          <div className="flex flex-col gap-2 rounded-md border border-primary/40 bg-primary/5 p-3">
            <p className="text-xs font-semibold text-foreground">
              New key generated — copy it now. It will not be shown again.
            </p>
            <code className="break-all rounded bg-muted p-2 text-xs">{revealedKey}</code>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => navigator.clipboard?.writeText(revealedKey)}
            >
              Copy
            </Button>
          </div>
        ) : null}

        {rotateError ? (
          <p role="alert" className="text-sm text-destructive">
            {rotateError}
          </p>
        ) : null}

        {confirmingRotate ? (
          <div className="flex flex-col gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3">
            <p className="text-xs font-semibold text-destructive">
              Rotating immediately invalidates the current key. Every embedded widget using
              it will stop working until you update the script tag on your site with the new
              key.
            </p>
            <div className="flex gap-2">
              <Button
                type="button"
                variant="destructive"
                size="sm"
                disabled={isRotating}
                onClick={handleRotateConfirmed}
              >
                {isRotating ? "Rotating…" : "Rotate anyway"}
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setConfirmingRotate(false)}
              >
                Cancel
              </Button>
            </div>
          </div>
        ) : (
          <div>
            <Button type="button" variant="outline" onClick={() => setConfirmingRotate(true)}>
              Rotate client key
            </Button>
          </div>
        )}
      </div>

      <div className="flex flex-col gap-3 border-t border-input pt-4">
        <p className="text-sm font-medium text-foreground">Origin allowlist</p>
        <p className="text-xs text-muted-foreground">
          One origin per line, e.g. https://example.com — scheme and host only, no path, no
          wildcard. An empty list disables the embedded widget for this workspace entirely.
        </p>

        <form action={originsAction} className="flex flex-col gap-2">
          <Textarea
            name="origins"
            value={originsText}
            onChange={(e) => setOriginsText(e.target.value)}
            rows={5}
            className="font-mono text-sm"
            placeholder="https://example.com"
          />
          {originsState.status === "error" ? (
            <p role="alert" className="text-sm text-destructive">
              {originsState.message}
            </p>
          ) : originsState.status === "saved" ? (
            <p role="status" className="text-xs text-muted-foreground">
              Saved. Currently allowed: {displayedOrigins.length === 0 ? "none (widget disabled)" : displayedOrigins.join(", ")}
            </p>
          ) : null}
          <div>
            <Button type="submit" variant="outline">
              Save origins
            </Button>
          </div>
        </form>
      </div>
    </SoftCard>
  );
}
