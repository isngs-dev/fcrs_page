"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  confirmPasswordReset,
  type ResetPasswordState,
} from "@/app/reset-password/actions";

const initialState: ResetPasswordState = { status: "idle", message: null };

function SubmitButton() {
  const { pending } = useFormStatus();
  return (
    <Button type="submit" className="w-full" disabled={pending}>
      {pending ? "Resetting..." : "Reset password"}
    </Button>
  );
}

export function ResetPasswordForm({ token }: { token: string | null }) {
  const [state, formAction] = useActionState(confirmPasswordReset, initialState);

  if (!token) {
    return (
      <div className="flex flex-col gap-4">
        <p role="alert" className="text-sm text-destructive">
          This reset link is missing its token. Please request a new one.
        </p>
        <Link href="/forgot-password" className="text-sm underline underline-offset-4">
          Request a new link
        </Link>
      </div>
    );
  }

  if (state.status === "success") {
    return (
      <div className="flex flex-col gap-4">
        <p role="status" className="text-sm">
          {state.message}
        </p>
        <Link href="/login" className="text-sm underline underline-offset-4">
          Go to sign in
        </Link>
      </div>
    );
  }

  return (
    <form action={formAction} className="flex flex-col gap-4">
      <input type="hidden" name="token" value={token} />
      <div className="flex flex-col gap-2">
        <Label htmlFor="newPassword">New password</Label>
        <Input
          id="newPassword"
          name="newPassword"
          type="password"
          autoComplete="new-password"
          minLength={12}
          required
        />
      </div>
      <div className="flex flex-col gap-2">
        <Label htmlFor="confirmPassword">Confirm new password</Label>
        <Input
          id="confirmPassword"
          name="confirmPassword"
          type="password"
          autoComplete="new-password"
          minLength={12}
          required
        />
      </div>
      {state.status === "error" ? (
        <p role="alert" className="text-sm text-destructive">
          {state.message}
        </p>
      ) : null}
      <SubmitButton />
    </form>
  );
}
