/**
 * Wraps every authenticated route. Re-confirms claims server-side via
 * `cookies()` + local decode as defense-in-depth alongside `proxy.ts`
 * (S13.1 decision 3) -- proxy is a fast pre-render gate, this is the
 * server-component-level check that runs even if proxy's matcher were ever
 * misconfigured for a given path.
 */
import { redirect } from "next/navigation";
import { getClaims } from "@/lib/auth";
import { getProfile } from "@/lib/profile";
import { getBotSettings } from "@/lib/settings";
import { listMyChatbots } from "@/lib/chatbots";
import { AdminShell } from "@/components/admin/admin-shell";
import { logout } from "@/app/(protected)/actions";

export default async function ProtectedLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const claims = await getClaims();
  if (!claims) {
    redirect("/login");
  }

  const profile = await getProfile();
  const identityLabel = profile?.name || profile?.email || claims.subject;
  // The dashboard page also reads these settings for its title. Identical
  // server fetches are memoized per request by Next, so the shell gets its
  // tenant label without exposing settings data to client state.
  const settingsResult =
    claims.role === "PLATFORM_ADMIN" ? null : await getBotSettings();
  const sidebarWorkspaceLabel =
    settingsResult?.status === "ok" ? settingsResult.settings.sidebarWorkspaceLabel : null;

  // Multi-chatbot accounts: the sidebar switcher's data. PLATFORM_ADMIN has
  // no account of its own (it browses via /clients/{tenantId}/... instead),
  // so this is only fetched for the two account-scoped roles.
  const isAccountScoped = claims.role === "CLIENT_ADMIN" || claims.role === "CLIENT_AGENT";
  const chatbotsResult = isAccountScoped ? await listMyChatbots() : null;
  const chatbots = chatbotsResult?.status === "ok" ? chatbotsResult.items : [];

  return (
    <AdminShell
      role={claims.role}
      identityLabel={identityLabel}
      sidebarWorkspaceLabel={sidebarWorkspaceLabel}
      sidebarStorageScope={claims.subject}
      logoutAction={logout}
      chatbots={chatbots}
      activeTenantId={claims.tenantId}
    >
      {children}
    </AdminShell>
  );
}
