/**
 * "Add a chatbot" screen -- a dedicated, nav-reachable destination for the
 * same platform-level onboarding power the Clients page's tile grid links
 * to. PLATFORM_ADMIN-only, gated the same way `/clients` itself is.
 *
 * Creates a new tenant (= a new, fully independent chatbot: its own bot
 * settings, knowledge base, and embed script/client key -- CLAUDE.md's
 * multi-tenant isolation model, unchanged) via the SAME `onboardNewClient`
 * action and `OnboardClientForm` the Clients page used inline before this
 * screen existed. This supersedes the older, unlinked `/tenants/new` route
 * (byte-for-byte the same form, wired to a near-duplicate action that landed
 * on `/` instead of the new tenant's own settings) -- that route is deleted
 * rather than left as a second, divergent way to do the same thing.
 */
import Link from "next/link";
import { requireRole } from "@/lib/auth";
import { OnboardClientForm } from "@/app/(protected)/clients/onboard-client-form";

export default async function AddChatbotPage() {
  await requireRole("PLATFORM_ADMIN");

  return (
    <div className="flex flex-1 flex-col items-center gap-4 p-8">
      <div className="w-full max-w-xl">
        <Link href="/clients" className="text-sm text-muted-foreground hover:underline">
          ← Back to clients
        </Link>
      </div>
      <div className="w-full max-w-xl rounded-[14px] border border-[var(--border)] bg-white p-6">
        <h1 className="text-lg font-bold text-[var(--foreground)]">Add a chatbot</h1>
        <p className="mt-1 text-[12.5px] text-[var(--muted-foreground)]">
          Creates a new chatbot for a client -- its own bot settings, knowledge base, and embed
          script, fully independent from every other client&apos;s. This also creates the
          client&apos;s first admin user. The client key (and generated admin password, if any)
          are shown exactly once — they cannot be recovered later.
        </p>
        <div className="mt-4">
          <OnboardClientForm />
        </div>
      </div>
    </div>
  );
}
