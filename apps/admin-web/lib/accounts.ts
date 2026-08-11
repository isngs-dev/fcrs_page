/**
 * Server-only data layer for the Accounts CRM page (SR-17). Mirrors
 * `lib/contacts.ts`'s shape exactly. No `tenantId` parameter (D7).
 *
 * Response shape verified live against
 * `services/api/src/api/accounts/admin_routes.py`'s `AccountResponse`
 * (account_id, name, domain, created_at -- no `tenant_id`, no `industry`,
 * no contact count). Per D5, the mockup's "Industry" and "Contacts (count)"
 * columns have no backing field and are NOT rendered anywhere in this
 * module or the page that consumes it -- see the sprint report.
 *
 * There is no `PATCH /admin/accounts/{id}` endpoint (M3) -- this module
 * intentionally exposes no update function.
 */
import "server-only";

import { adminApiFetch, AdminApiError } from "@/lib/api";

/** Fixed page size the console requests per page (D6). The backend clamps
 * `limit` to `[1,200]` server-side (M3) -- this value is well within that
 * range and is not a re-implementation of the clamp. */
export const ACCOUNTS_PAGE_SIZE = 25;

/** Closed mirror of api.accounts.repository's SR-29 ORDER BY allowlist. */
export const ACCOUNT_SORT_KEYS = ["name", "domain", "created"] as const;

export type AccountSortKey = (typeof ACCOUNT_SORT_KEYS)[number];
export type AccountSortDirection = "asc" | "desc";

const ACCOUNT_SORT_DEFAULT_DIRECTIONS: Record<AccountSortKey, AccountSortDirection> = {
  name: "asc",
  domain: "asc",
  created: "desc",
};

export function defaultAccountSortDirection(sort: AccountSortKey): AccountSortDirection {
  return ACCOUNT_SORT_DEFAULT_DIRECTIONS[sort];
}

/** Server-side clamp bounds mirrored for documentation/tests only (M3) --
 * NOT enforced client-side; the backend is the sole enforcement point. */
export const ACCOUNTS_SERVER_LIMIT_MIN = 1;
export const ACCOUNTS_SERVER_LIMIT_MAX = 200;

/** Mirrors `AccountResponse` (accounts/admin_routes.py:47-53) exactly. */
export interface Account {
  accountId: string;
  name: string;
  domain: string | null;
  createdAt: string;
}

interface AccountResponseBody {
  account_id: string;
  name: string;
  domain: string | null;
  created_at: string;
}

interface AccountListResponseBody {
  items: AccountResponseBody[];
  total: number;
  limit: number;
  offset: number;
}

export type AccountsResult =
  | { status: "ok"; items: Account[]; total: number; limit: number; offset: number }
  | { status: "error"; message: string; correlationId: string };

export type AccountDetailResult =
  | { status: "ok"; account: Account }
  | { status: "error"; message: string; correlationId: string };

export interface AccountsQueryParams {
  page: number;
  q?: string;
  sort?: string;
  direction?: string;
}

/**
 * Pure, unit-testable query builder (D6, extended by SR-29). Clamps
 * `page >= 1`, derives `offset = (page-1) * ACCOUNTS_PAGE_SIZE`. Always
 * sends `limit` as the fixed page size -- the server's own `[1,200]` clamp
 * (`accounts/admin_routes.py`) is the sole enforcement point. Drops any
 * `sort` value not in `ACCOUNT_SORT_KEYS` (unknown/blank -> no sort param at
 * all, so a stray/hand-edited `?sort=bogus` degrades to the server's default
 * order rather than a hard 422 on first paint) and any `q` shorter than 2
 * characters (mirrors the server's own minimum, avoiding a guaranteed 422
 * round trip for a single keystroke).
 */
export function buildAccountsQuery(params: AccountsQueryParams): string {
  const page = Number.isFinite(params.page) && params.page >= 1 ? Math.floor(params.page) : 1;
  const offset = (page - 1) * ACCOUNTS_PAGE_SIZE;

  const query = new URLSearchParams();
  query.set("limit", String(ACCOUNTS_PAGE_SIZE));
  query.set("offset", String(offset));

  const q = params.q?.trim();
  if (q && q.length >= 2 && q.length <= 200) {
    query.set("q", q);
  }

  const sort = params.sort?.trim();
  if (sort && (ACCOUNT_SORT_KEYS as readonly string[]).includes(sort)) {
    const sortKey = sort as AccountSortKey;
    query.set("sort", sortKey);
    const direction = params.direction?.trim();
    query.set(
      "dir",
      direction === "asc" || direction === "desc"
        ? direction
        : ACCOUNT_SORT_DEFAULT_DIRECTIONS[sortKey]
    );
  }

  return query.toString();
}

function toAccount(body: AccountResponseBody): Account {
  return {
    accountId: body.account_id,
    name: body.name,
    domain: body.domain,
    createdAt: body.created_at,
  };
}

function mapErrorMessage(error: AdminApiError): string {
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view accounts.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  if (
    error.errorCode === "INVALID_ACCOUNT_SORT" ||
    error.errorCode === "INVALID_ACCOUNT_SORT_DIRECTION" ||
    error.errorCode === "INVALID_ACCOUNT_SEARCH"
  ) {
    return "That filter isn't valid -- showing all accounts.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

function mapDetailErrorMessage(error: AdminApiError): string {
  if (error.status === 404 || error.errorCode === "NOT_FOUND") {
    return "This account could not be found.";
  }
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view this account.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

/** Fetch a page of the caller's tenant accounts. Never sends a `tenant_id`
 * (D7). */
export async function listAccounts(params: AccountsQueryParams): Promise<AccountsResult> {
  const query = buildAccountsQuery(params);

  try {
    const response = await adminApiFetch(`/admin/accounts?${query}`);
    const body = (await response.json()) as AccountListResponseBody;
    return {
      status: "ok",
      items: body.items.map(toAccount),
      total: body.total,
      limit: body.limit,
      offset: body.offset,
    };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return { status: "error", message: mapErrorMessage(error), correlationId: error.correlationId };
    }
    return {
      status: "error",
      message: "Unable to reach the server. Please try again.",
      correlationId: "",
    };
  }
}

/** Fetch a single account's detail (used to render a name behind a contact's
 * `?contact=` link target on `/accounts/{id}` -- no N+1 fan-out: this is
 * only ever called for the ONE account whose detail page/drawer is open). */
export async function getAccountDetail(accountId: string): Promise<AccountDetailResult> {
  try {
    const response = await adminApiFetch(`/admin/accounts/${encodeURIComponent(accountId)}`);
    const body = (await response.json()) as AccountResponseBody;
    return { status: "ok", account: toAccount(body) };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return { status: "error", message: mapDetailErrorMessage(error), correlationId: error.correlationId };
    }
    return {
      status: "error",
      message: "Unable to reach the server. Please try again.",
      correlationId: "",
    };
  }
}
