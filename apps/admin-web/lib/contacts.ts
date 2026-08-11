/**
 * Server-only data layer for the Contacts CRM page (SR-17). Mirrors
 * `lib/leads.ts`/`lib/analytics.ts`'s shape exactly (M9): a pure,
 * unit-testable query resolver, one fetch per surface (list/detail/create),
 * a discriminated `{status:"ok"|"error"}` result, snake_case -> camelCase
 * mapping, and `AdminApiError` -> an honest message + surfaced correlation
 * ID. No `tenantId` parameter anywhere (D7) -- the PLATFORM_ADMIN
 * tenant-explicit route family exists on the backend but is deliberately
 * not wired by this sprint.
 *
 * Response shape verified live against
 * `services/api/src/api/contacts/admin_routes.py`'s `ContactResponse`
 * (contact_id, account_id, lead_id, name, email, phone, owner_agent_id,
 * created_at -- no `tenant_id`, no raw consent text). There is no company
 * *name* field on a contact, only `account_id` (D5) -- the table/drawer
 * link to `/accounts/{id}` rather than fetching per-row to resolve a name
 * (the forbidden N+1).
 */
import "server-only";

import { adminApiFetch, AdminApiError } from "@/lib/api";

/** Fixed page size for the console's page/limit pagination (D6), matching
 * the leads console's existing convention (`lib/leads.ts` LEADS_PAGE_SIZE). */
export const CONTACTS_PAGE_SIZE = 25;

/** Closed mirror of api.contacts.repository's SR-29 ORDER BY allowlist.
 * `account` orders by the opaque `account_id` -- it GROUPS contacts by
 * company, it does not alpha-sort by company name (D-COMPANY-SORT). */
export const CONTACT_SORT_KEYS = ["name", "email", "account", "owner", "created"] as const;

export type ContactSortKey = (typeof CONTACT_SORT_KEYS)[number];
export type ContactSortDirection = "asc" | "desc";

const CONTACT_SORT_DEFAULT_DIRECTIONS: Record<ContactSortKey, ContactSortDirection> = {
  name: "asc",
  email: "asc",
  account: "asc",
  owner: "asc",
  created: "desc",
};

export function defaultContactSortDirection(sort: ContactSortKey): ContactSortDirection {
  return CONTACT_SORT_DEFAULT_DIRECTIONS[sort];
}

/** Mirrors `ContactResponse` (contacts/admin_routes.py:82-93) exactly. */
export interface Contact {
  contactId: string;
  accountId: string | null;
  leadId: string | null;
  name: string | null;
  email: string | null;
  phone: string | null;
  ownerAgentId: string | null;
  createdAt: string;
}

interface ContactResponseBody {
  contact_id: string;
  account_id: string | null;
  lead_id: string | null;
  name: string | null;
  email: string | null;
  phone: string | null;
  owner_agent_id: string | null;
  created_at: string;
}

interface ContactListResponseBody {
  items: ContactResponseBody[];
  total: number;
  limit: number;
  offset: number;
}

export type ContactsResult =
  | { status: "ok"; items: Contact[]; total: number; limit: number; offset: number }
  | { status: "error"; message: string; correlationId: string };

export type ContactDetailResult =
  | { status: "ok"; contact: Contact }
  | { status: "error"; message: string; correlationId: string };

export interface ContactsQueryParams {
  page: number;
  q?: string;
  accountId?: string;
  sort?: string;
  direction?: string;
}

/**
 * Pure, unit-testable query builder (D6, extended by SR-29). Clamps
 * `page >= 1`, derives `offset = (page-1) * CONTACTS_PAGE_SIZE`. `limit` is
 * always sent as `CONTACTS_PAGE_SIZE` -- the backend additionally clamps
 * `[1,200]` server-side (M1), which this function does not re-implement or
 * second-guess (D6/M3: "must not be silently corrected into a different
 * meaning client-side"). Drops any `sort` value not in `CONTACT_SORT_KEYS`
 * and any `q` shorter than 2 characters, mirroring `lib/leads.ts`'s
 * SR-25 pattern exactly. `accountId` is a real, indexed exact-match filter
 * and is sent verbatim when present -- the backend deliberately does not
 * existence-validate it on the list path (D-FILTER), so this function has
 * nothing to validate either.
 */
export function buildContactsQuery(params: ContactsQueryParams): string {
  const page = Number.isFinite(params.page) && params.page >= 1 ? Math.floor(params.page) : 1;
  const offset = (page - 1) * CONTACTS_PAGE_SIZE;

  const query = new URLSearchParams();
  query.set("limit", String(CONTACTS_PAGE_SIZE));
  query.set("offset", String(offset));

  const accountId = params.accountId?.trim();
  if (accountId) {
    query.set("account_id", accountId);
  }

  const q = params.q?.trim();
  if (q && q.length >= 2 && q.length <= 200) {
    query.set("q", q);
  }

  const sort = params.sort?.trim();
  if (sort && (CONTACT_SORT_KEYS as readonly string[]).includes(sort)) {
    const sortKey = sort as ContactSortKey;
    query.set("sort", sortKey);
    const direction = params.direction?.trim();
    query.set(
      "dir",
      direction === "asc" || direction === "desc"
        ? direction
        : CONTACT_SORT_DEFAULT_DIRECTIONS[sortKey]
    );
  }

  return query.toString();
}

function toContact(body: ContactResponseBody): Contact {
  return {
    contactId: body.contact_id,
    accountId: body.account_id,
    leadId: body.lead_id,
    name: body.name,
    email: body.email,
    phone: body.phone,
    ownerAgentId: body.owner_agent_id,
    createdAt: body.created_at,
  };
}

function mapErrorMessage(error: AdminApiError): string {
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view contacts.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  if (
    error.errorCode === "INVALID_CONTACT_SORT" ||
    error.errorCode === "INVALID_CONTACT_SORT_DIRECTION" ||
    error.errorCode === "INVALID_CONTACT_SEARCH"
  ) {
    return "That filter isn't valid -- showing all contacts.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

function mapDetailErrorMessage(error: AdminApiError): string {
  if (error.status === 404 || error.errorCode === "NOT_FOUND") {
    return "This contact could not be found.";
  }
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view this contact.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

/**
 * Fetch a page of the caller's tenant contacts. Never sends a `tenant_id`
 * (D7) -- scoping is entirely the backend's repository-layer job from the
 * caller's own session cookie. Never logs the response body (PII-minimal;
 * contacts carry name/email/phone).
 */
export async function listContacts(params: ContactsQueryParams): Promise<ContactsResult> {
  const query = buildContactsQuery(params);

  try {
    const response = await adminApiFetch(`/admin/contacts?${query}`);
    const body = (await response.json()) as ContactListResponseBody;
    return {
      status: "ok",
      items: body.items.map(toContact),
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

/** Fetch a single contact's detail for the record drawer. */
export async function getContactDetail(contactId: string): Promise<ContactDetailResult> {
  try {
    const response = await adminApiFetch(`/admin/contacts/${encodeURIComponent(contactId)}`);
    const body = (await response.json()) as ContactResponseBody;
    return { status: "ok", contact: toContact(body) };
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
