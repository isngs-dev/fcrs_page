"use server";

/**
 * Knowledge upload + status-poll server actions (S13.3 decisions 3, 4, 6).
 *
 * `uploadKnowledge` forwards the picked file as multipart/form-data to
 * `POST /admin/ingestion/upload` via `adminApiFetch` -- server-to-server, so
 * the httpOnly JWT cookie never needs to reach the browser (decision 3).
 *
 * `getDocStatus` reads `GET /admin/ingestion/docs/{doc_id}` for the client
 * poll loop (decision 4) -- the browser cannot call admin-api directly (the
 * JWT is server-only), so every poll tick round-trips through this action.
 */
import { revalidatePath } from "next/cache";

import { AdminApiError, adminApiFetch } from "@/lib/api";
import {
  ALLOWED_CONTENT_TYPES,
  formatBytes,
  MAX_UPLOAD_BYTES,
} from "@/lib/knowledge-constants";

const GENERIC_NETWORK_ERROR = "Unable to reach the server. Please try again.";

// ---------------------------------------------------------------------------
// uploadKnowledge
// ---------------------------------------------------------------------------

export interface UploadIdleState {
  status: "idle";
}

export interface UploadErrorState {
  status: "error";
  message: string;
  correlationId: string | null;
}

export interface UploadedState {
  status: "uploaded";
  docId: string;
  /** `null` signals an idempotent re-upload (decision 6) -- no new run was
   * enqueued; `docStatus` reflects the existing doc's current status. */
  runId: string | null;
  docStatus: string;
  idempotent: boolean;
}

export type UploadState = UploadIdleState | UploadErrorState | UploadedState;

interface AdminUploadResponseBody {
  doc_id: string;
  run_id: string | null;
  status: string;
}

/**
 * `tenantId` (S13.7): bound via `uploadKnowledge.bind(null, tenantId)` from
 * the per-client knowledge screen (the standard Next.js pattern for passing
 * an extra argument to a `useActionState` action) -- when set, targets the
 * S12.7 PLATFORM_ADMIN super-user surface
 * `POST /admin/tenants/{tenantId}/ingestion/upload` instead of the implicit
 * `POST /admin/ingestion/upload`. `undefined`/omitted preserves the existing
 * CLIENT_ADMIN behavior exactly (implicit route, `{tenantId}` never sent).
 */
export async function uploadKnowledge(
  tenantId: string | undefined,
  _prevState: UploadState,
  formData: FormData
): Promise<UploadState> {
  const file = formData.get("file");

  if (!(file instanceof File) || file.size === 0) {
    return {
      status: "error",
      message: "Choose a .txt or .docx file to upload.",
      correlationId: null,
    };
  }

  // Client-side pre-check already ran in the browser (Decision 5); this is
  // the server-action re-check -- courtesy only, the backend is the real
  // gate and still enforces both limits itself.
  if (file.size > MAX_UPLOAD_BYTES) {
    return {
      status: "error",
      message: `That file is too large (${formatBytes(file.size)}). The limit is ${formatBytes(MAX_UPLOAD_BYTES)}.`,
      correlationId: null,
    };
  }

  const contentType = (file.type || "").split(";")[0].trim().toLowerCase();
  if (
    contentType &&
    !(ALLOWED_CONTENT_TYPES as readonly string[]).includes(contentType)
  ) {
    return {
      status: "error",
      message: `Unsupported file type: "${file.type}". Only .txt and .docx files are accepted.`,
      correlationId: null,
    };
  }

  const uploadForm = new FormData();
  uploadForm.append("file", file, file.name);
  // Title/description (Knowledge Base list feature): optional, so only
  // append when non-blank -- an all-whitespace value is treated the same
  // as omitted (the backend normalizes it to null either way).
  const title = formData.get("title");
  if (typeof title === "string" && title.trim()) uploadForm.append("title", title.trim());
  const description = formData.get("description");
  if (typeof description === "string" && description.trim()) {
    uploadForm.append("description", description.trim());
  }

  const uploadPath = tenantId
    ? `/admin/tenants/${encodeURIComponent(tenantId)}/ingestion/upload`
    : "/admin/ingestion/upload";

  let response: Response;
  try {
    // No manual Content-Type -- adminApiFetch/fetch sets the multipart
    // boundary automatically for a FormData body.
    response = await adminApiFetch(uploadPath, {
      method: "POST",
      body: uploadForm,
    });
  } catch (err) {
    if (err instanceof AdminApiError) {
      return mapUploadError(err);
    }
    return {
      status: "error",
      message: GENERIC_NETWORK_ERROR,
      correlationId: null,
    };
  }

  const body = (await response.json()) as AdminUploadResponseBody;

  // Knowledge Base list feature: on a FRESH upload (run_id set -- the list
  // actually changed), revalidate the Knowledge page so the new item shows
  // up without a manual reload. Skipped on an idempotent re-upload
  // (run_id: null) since the list didn't change. Mirrors the exact
  // tenantId-conditional dual-path pattern already used by every other
  // mutating action in this app (e.g. settings/actions.ts).
  if (body.run_id !== null) {
    revalidatePath(tenantId ? `/clients/${tenantId}/knowledge` : "/knowledge");
  }

  return {
    status: "uploaded",
    docId: body.doc_id,
    runId: body.run_id,
    docStatus: body.status,
    idempotent: body.run_id === null,
  };
}

function mapUploadError(err: AdminApiError): UploadErrorState {
  if (err.errorCode === "UNSUPPORTED_CONTENT_TYPE") {
    return {
      status: "error",
      message: "Unsupported file type. Only .txt and .docx files are accepted.",
      correlationId: err.correlationId || null,
    };
  }

  if (err.status === 413 || err.errorCode === "FILE_TOO_LARGE") {
    return {
      status: "error",
      message: `That file is too large. The limit is ${formatBytes(MAX_UPLOAD_BYTES)}.`,
      correlationId: err.correlationId || null,
    };
  }

  if (err.status === 403 || err.errorCode === "ROLE_NOT_PERMITTED") {
    return {
      status: "error",
      message: "You do not have permission to upload knowledge documents.",
      correlationId: err.correlationId || null,
    };
  }

  if (err.status === 401) {
    return {
      status: "error",
      message: "Your session has expired. Please sign in again.",
      correlationId: err.correlationId || null,
    };
  }

  return {
    status: "error",
    message: `${err.message} (correlation ID: ${err.correlationId || "unknown"})`,
    correlationId: err.correlationId || null,
  };
}

// ---------------------------------------------------------------------------
// getDocStatus
// ---------------------------------------------------------------------------

export interface DocStatusRun {
  runId: string;
  status: string;
  charsOut: number | null;
  errors: unknown;
  durationMs: number | null;
}

export interface DocStatusOk {
  status: "ok";
  docId: string;
  docStatus: string;
  run: DocStatusRun | null;
  parsedPreview: string | null;
}

export interface DocStatusError {
  status: "error";
  errorCode: string | null;
  message: string;
}

export type DocStatusResult = DocStatusOk | DocStatusError;

interface AdminDocStatusResponseBody {
  doc_id: string;
  filename: string;
  content_type: string;
  status: string;
  content_hash: string;
  latest_run: {
    run_id: string;
    status: string;
    chars_out: number | null;
    errors: unknown;
    duration_ms: number | null;
  } | null;
  parsed_preview: string | null;
}

/**
 * `tenantId` (S13.7): when provided, targets the S12.7 PLATFORM_ADMIN
 * super-user surface `GET /admin/tenants/{tenantId}/ingestion/docs/{docId}`
 * instead of the implicit `GET /admin/ingestion/docs/{docId}`. Called
 * directly from a client component's poll loop (not via `useActionState`),
 * so this takes `tenantId` as a normal parameter rather than a bound arg.
 */
export async function getDocStatus(docId: string, tenantId?: string): Promise<DocStatusResult> {
  const path = tenantId
    ? `/admin/tenants/${encodeURIComponent(tenantId)}/ingestion/docs/${encodeURIComponent(docId)}`
    : `/admin/ingestion/docs/${encodeURIComponent(docId)}`;

  let response: Response;
  try {
    response = await adminApiFetch(path, {
      method: "GET",
    });
  } catch (err) {
    if (err instanceof AdminApiError) {
      return {
        status: "error",
        errorCode: err.errorCode || null,
        message:
          err.errorCode === "DOC_NOT_FOUND"
            ? "Document not found."
            : `${err.message} (correlation ID: ${err.correlationId || "unknown"})`,
      };
    }
    return { status: "error", errorCode: null, message: GENERIC_NETWORK_ERROR };
  }

  const body = (await response.json()) as AdminDocStatusResponseBody;

  return {
    status: "ok",
    docId: body.doc_id,
    docStatus: body.status,
    run: body.latest_run
      ? {
          runId: body.latest_run.run_id,
          status: body.latest_run.status,
          charsOut: body.latest_run.chars_out,
          errors: body.latest_run.errors,
          durationMs: body.latest_run.duration_ms,
        }
      : null,
    parsedPreview: body.parsed_preview,
  };
}

// ---------------------------------------------------------------------------
// listKnowledgeDocs
// ---------------------------------------------------------------------------

export interface KnowledgeDocListItem {
  docId: string;
  title: string | null;
  description: string | null;
  filename: string;
  contentType: string;
  status: string;
  uploadedBy: string | null;
  uploadedByName: string | null;
  createdAt: string;
}

export interface ListKnowledgeOk {
  status: "ok";
  docs: KnowledgeDocListItem[];
}

export interface ListKnowledgeError {
  status: "error";
  message: string;
  correlationId: string | null;
}

export type ListKnowledgeResult = ListKnowledgeOk | ListKnowledgeError;

interface AdminKnowledgeDocListItemBody {
  doc_id: string;
  title: string | null;
  description: string | null;
  filename: string;
  content_type: string;
  status: string;
  uploaded_by: string | null;
  uploaded_by_name: string | null;
  created_at: string;
}

interface AdminKnowledgeListResponseBody {
  docs: AdminKnowledgeDocListItemBody[];
}

/**
 * Knowledge Base list feature: fetches every knowledge doc for the caller's
 * tenant, newest upload first (the backend already sorts -- this never
 * re-sorts). `tenantId` (S13.7 pattern): when provided, targets the
 * PLATFORM_ADMIN super-user surface `GET /admin/tenants/{tenantId}/ingestion/docs`
 * instead of the implicit `GET /admin/ingestion/docs`. Called server-side
 * from the RSC page (not a client poll loop), so a network/auth failure
 * renders an honest inline error rather than a blank/fabricated list.
 */
export async function listKnowledgeDocs(tenantId?: string): Promise<ListKnowledgeResult> {
  const path = tenantId
    ? `/admin/tenants/${encodeURIComponent(tenantId)}/ingestion/docs`
    : "/admin/ingestion/docs";

  let response: Response;
  try {
    response = await adminApiFetch(path, { method: "GET" });
  } catch (err) {
    if (err instanceof AdminApiError) {
      return {
        status: "error",
        message: `${err.message} (correlation ID: ${err.correlationId || "unknown"})`,
        correlationId: err.correlationId || null,
      };
    }
    return { status: "error", message: GENERIC_NETWORK_ERROR, correlationId: null };
  }

  const body = (await response.json()) as AdminKnowledgeListResponseBody;

  return {
    status: "ok",
    docs: body.docs.map((doc) => ({
      docId: doc.doc_id,
      title: doc.title,
      description: doc.description,
      filename: doc.filename,
      contentType: doc.content_type,
      status: doc.status,
      uploadedBy: doc.uploaded_by,
      uploadedByName: doc.uploaded_by_name,
      createdAt: doc.created_at,
    })),
  };
}
