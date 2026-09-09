/**
 * "Add a chatbot" self-service screen (multi-chatbot accounts).
 * CLIENT_ADMIN-only. Mirrors `clients/new/page.tsx`'s shape -- creates a
 * new tenant under the caller's OWN account (its own bot settings,
 * knowledge base, and embed script/client key, CLAUDE.md's multi-tenant
 * isolation model, unchanged) via `CreateChatbotForm`. Unlike platform
 * onboarding, this creates NO new user -- every existing member of the
 * account already reaches the new chatbot via the switcher.
 */
import Link from "next/link";
import { requireAnyRole } from "@/lib/auth";
import { CreateChatbotForm } from "@/app/(protected)/chatbots/create-chatbot-form";

export default async function AddOwnChatbotPage() {
  await requireAnyRole("CLIENT_ADMIN");

  return (
    <div className="flex flex-1 flex-col items-center gap-4 p-8">
      <div className="w-full max-w-xl">
        <Link href="/chatbots" className="text-sm text-muted-foreground hover:underline">
          ← Back to my chatbots
        </Link>
      </div>
      <div className="w-full max-w-xl rounded-[14px] border border-[var(--border)] bg-white p-6">
        <h1 className="text-lg font-bold text-[var(--foreground)]">Add a chatbot</h1>
        <p className="mt-1 text-[12.5px] text-[var(--muted-foreground)]">
          Creates a new chatbot on your account -- its own bot settings, knowledge base, and
          embed script, fully independent from every other chatbot on your account. No new user
          is created; every team member on your account can switch to it. The client key is shown
          exactly once — it cannot be recovered later.
        </p>
        <div className="mt-4">
          <CreateChatbotForm />
        </div>
      </div>
    </div>
  );
}
