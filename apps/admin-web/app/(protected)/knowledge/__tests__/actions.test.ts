import { afterEach, describe, expect, it, vi } from "vitest";

const adminApiFetchMock = vi.fn();
const revalidatePathMock = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    adminApiFetch: (...args: unknown[]) => adminApiFetchMock(...args),
  };
});

vi.mock("next/cache", () => ({
  revalidatePath: (...args: unknown[]) => revalidatePathMock(...args),
}));

const {
  uploadKnowledge,
  getDocStatus,
  listKnowledgeDocs,
  previewChat,
  suggestDraftAnswer,
  listCoverageGaps,
  submitTrainedAnswer,
  dismissGap,
} = await import("@/app/(protected)/knowledge/actions");
const { AdminApiError } = await import("@/lib/api");

function jsonResponse(body: Record<string, unknown>, status: number): Response {
  return new Response(JSON.stringify(body), { status });
}

function buildFormData(
  file: File | null,
  extra: { title?: string; description?: string } = {}
): FormData {
  const fd = new FormData();
  if (file) fd.set("file", file);
  if (extra.title !== undefined) fd.set("title", extra.title);
  if (extra.description !== undefined) fd.set("description", extra.description);
  return fd;
}

function makeFile(opts: { name?: string; type?: string; sizeBytes?: number } = {}): File {
  const { name = "faq.txt", type = "text/plain", sizeBytes = 100 } = opts;
  const content = new Uint8Array(sizeBytes).fill(65); // 'A' repeated
  return new File([content], name, { type });
}

describe("uploadKnowledge", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    adminApiFetchMock.mockReset();
    revalidatePathMock.mockReset();
  });

  it("rejects a missing file client-side without calling adminApiFetch", async () => {
    const state = await uploadKnowledge(undefined, { status: "idle" }, buildFormData(null));

    expect(state.status).toBe("error");
    expect(adminApiFetchMock).not.toHaveBeenCalled();
  });

  it("rejects an oversized file in the server-action re-check without calling adminApiFetch", async () => {
    const oversized = makeFile({ sizeBytes: 10_485_761 });
    const state = await uploadKnowledge(undefined, { status: "idle" }, buildFormData(oversized));

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.message).toMatch(/too large/i);
    }
    expect(adminApiFetchMock).not.toHaveBeenCalled();
  });

  it("rejects a disallowed content type in the server-action re-check without calling adminApiFetch", async () => {
    const badType = makeFile({ name: "logo.png", type: "image/png" });
    const state = await uploadKnowledge(undefined, { status: "idle" }, buildFormData(badType));

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.message).toMatch(/unsupported file type/i);
    }
    expect(adminApiFetchMock).not.toHaveBeenCalled();
  });

  it("returns an uploaded state with idempotent:false on a fresh upload (run_id set)", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({ doc_id: "doc-1", run_id: "run-1", status: "pending" }, 200)
    );

    const state = await uploadKnowledge(undefined, { status: "idle" }, buildFormData(makeFile()));

    expect(state.status).toBe("uploaded");
    if (state.status === "uploaded") {
      expect(state.docId).toBe("doc-1");
      expect(state.runId).toBe("run-1");
      expect(state.idempotent).toBe(false);
    }
  });

  it("returns an uploaded state with idempotent:true when run_id is null", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({ doc_id: "doc-2", run_id: null, status: "parsed" }, 200)
    );

    const state = await uploadKnowledge(undefined, { status: "idle" }, buildFormData(makeFile()));

    expect(state.status).toBe("uploaded");
    if (state.status === "uploaded") {
      expect(state.docId).toBe("doc-2");
      expect(state.runId).toBeNull();
      expect(state.docStatus).toBe("parsed");
      expect(state.idempotent).toBe(true);
    }
  });

  it("maps UNSUPPORTED_CONTENT_TYPE to a friendly message", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(422, {
        error_code: "UNSUPPORTED_CONTENT_TYPE",
        message: "Unsupported content type.",
        correlation_id: "corr-1",
      })
    );

    const state = await uploadKnowledge(undefined, { status: "idle" }, buildFormData(makeFile()));

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.message).toMatch(/\.txt and \.docx/i);
    }
  });

  it("maps a 413 FILE_TOO_LARGE to a friendly message", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(413, {
        error_code: "FILE_TOO_LARGE",
        message: "Upload exceeds the limit.",
        correlation_id: "corr-2",
      })
    );

    const state = await uploadKnowledge(undefined, { status: "idle" }, buildFormData(makeFile()));

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.message).toMatch(/too large/i);
    }
  });

  it("maps a 403 ROLE_NOT_PERMITTED to a permission-denied message", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(403, {
        error_code: "ROLE_NOT_PERMITTED",
        message: "Forbidden.",
        correlation_id: "corr-3",
      })
    );

    const state = await uploadKnowledge(undefined, { status: "idle" }, buildFormData(makeFile()));

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.message).toMatch(/permission/i);
    }
  });

  it("maps a 401 to a session-expired message", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(401, {
        error_code: "AUTHENTICATION_ERROR",
        message: "Expired.",
        correlation_id: "corr-4",
      })
    );

    const state = await uploadKnowledge(undefined, { status: "idle" }, buildFormData(makeFile()));

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.message).toMatch(/session has expired/i);
    }
  });

  it("maps an unknown error code to a generic message including the correlation ID", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(500, {
        error_code: "INTERNAL_SERVER_ERROR",
        message: "Something went wrong.",
        correlation_id: "corr-unknown-xyz",
      })
    );

    const state = await uploadKnowledge(undefined, { status: "idle" }, buildFormData(makeFile()));

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.message).toContain("corr-unknown-xyz");
    }
  });

  it("returns a generic network message when adminApiFetch throws a non-AdminApiError", async () => {
    adminApiFetchMock.mockRejectedValue(new TypeError("fetch failed"));

    const state = await uploadKnowledge(undefined, { status: "idle" }, buildFormData(makeFile()));

    expect(state.status).toBe("error");
    if (state.status === "error") {
      expect(state.message).toMatch(/unable to reach the server/i);
    }
  });

  it("targets the implicit /admin/ingestion/upload path when tenantId is omitted", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({ doc_id: "doc-1", run_id: "run-1", status: "pending" }, 200)
    );

    await uploadKnowledge(undefined, { status: "idle" }, buildFormData(makeFile()));

    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/admin/ingestion/upload",
      expect.objectContaining({ method: "POST" })
    );
  });

  it("targets the S12.7 tenant-scoped path when tenantId is bound (PLATFORM_ADMIN)", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({ doc_id: "doc-1", run_id: "run-1", status: "pending" }, 200)
    );

    await uploadKnowledge("tenant-x", { status: "idle" }, buildFormData(makeFile()));

    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/admin/tenants/tenant-x/ingestion/upload",
      expect.objectContaining({ method: "POST" })
    );
  });

  it("revalidates the implicit /knowledge path on a fresh (non-idempotent) upload", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({ doc_id: "doc-1", run_id: "run-1", status: "pending" }, 200)
    );

    await uploadKnowledge(undefined, { status: "idle" }, buildFormData(makeFile()));

    expect(revalidatePathMock).toHaveBeenCalledWith("/knowledge");
  });

  it("revalidates the tenant-scoped path when tenantId is bound", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({ doc_id: "doc-1", run_id: "run-1", status: "pending" }, 200)
    );

    await uploadKnowledge("tenant-x", { status: "idle" }, buildFormData(makeFile()));

    expect(revalidatePathMock).toHaveBeenCalledWith("/clients/tenant-x/knowledge");
  });

  it("does NOT revalidate on an idempotent re-upload (run_id: null, list unchanged)", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({ doc_id: "doc-2", run_id: null, status: "parsed" }, 200)
    );

    await uploadKnowledge(undefined, { status: "idle" }, buildFormData(makeFile()));

    expect(revalidatePathMock).not.toHaveBeenCalled();
  });

  it("includes title/description in the outgoing FormData when provided, trimmed", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({ doc_id: "doc-1", run_id: "run-1", status: "pending" }, 200)
    );

    await uploadKnowledge(
      undefined,
      { status: "idle" },
      buildFormData(makeFile(), { title: "  Refund policy  ", description: "  How refunds work.  " })
    );

    const [, init] = adminApiFetchMock.mock.calls[0] as [string, { body: FormData }];
    expect(init.body.get("title")).toBe("Refund policy");
    expect(init.body.get("description")).toBe("How refunds work.");
  });

  it("omits title/description entirely from the outgoing FormData when left blank", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({ doc_id: "doc-1", run_id: "run-1", status: "pending" }, 200)
    );

    await uploadKnowledge(
      undefined,
      { status: "idle" },
      buildFormData(makeFile(), { title: "   ", description: "" })
    );

    const [, init] = adminApiFetchMock.mock.calls[0] as [string, { body: FormData }];
    expect(init.body.get("title")).toBeNull();
    expect(init.body.get("description")).toBeNull();
  });
});

describe("listKnowledgeDocs", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    adminApiFetchMock.mockReset();
  });

  it("maps a 200 body to an ok result with camelCase fields", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          docs: [
            {
              doc_id: "doc-1",
              title: "Refund policy",
              description: "How refunds work.",
              filename: "refunds.txt",
              content_type: "text/plain",
              status: "parsed",
              uploaded_by: "user-1",
              uploaded_by_name: "Jane Admin",
              created_at: "2026-08-18T12:00:00Z",
            },
          ],
        },
        200
      )
    );

    const result = await listKnowledgeDocs();

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.docs).toHaveLength(1);
      expect(result.docs[0]).toEqual({
        docId: "doc-1",
        title: "Refund policy",
        description: "How refunds work.",
        filename: "refunds.txt",
        contentType: "text/plain",
        status: "parsed",
        uploadedBy: "user-1",
        uploadedByName: "Jane Admin",
        createdAt: "2026-08-18T12:00:00Z",
      });
    }
  });

  it("maps an empty docs array to an ok result with an empty list, never an error", async () => {
    adminApiFetchMock.mockResolvedValue(jsonResponse({ docs: [] }, 200));

    const result = await listKnowledgeDocs();

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.docs).toEqual([]);
    }
  });

  it("maps an AdminApiError to an error result with the correlation id", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(500, {
        error_code: "INTERNAL_SERVER_ERROR",
        message: "Something went wrong.",
        correlation_id: "corr-list-1",
      })
    );

    const result = await listKnowledgeDocs();

    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.correlationId).toBe("corr-list-1");
      expect(result.message).toContain("corr-list-1");
    }
  });

  it("maps a network throw to a generic error result", async () => {
    adminApiFetchMock.mockRejectedValue(new TypeError("fetch failed"));

    const result = await listKnowledgeDocs();

    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/unable to reach the server/i);
    }
  });

  it("targets the tenant-scoped list path when tenantId is provided", async () => {
    adminApiFetchMock.mockResolvedValue(jsonResponse({ docs: [] }, 200));

    await listKnowledgeDocs("tenant-x");

    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/admin/tenants/tenant-x/ingestion/docs",
      expect.objectContaining({ method: "GET" })
    );
  });

  it("targets the implicit list path when tenantId is omitted", async () => {
    adminApiFetchMock.mockResolvedValue(jsonResponse({ docs: [] }, 200));

    await listKnowledgeDocs();

    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/admin/ingestion/docs",
      expect.objectContaining({ method: "GET" })
    );
  });
});

describe("getDocStatus", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    adminApiFetchMock.mockReset();
  });

  it("maps a 200 body with latest_run to a DocStatusOk with a run", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          doc_id: "doc-1",
          filename: "faq.txt",
          content_type: "text/plain",
          status: "pending",
          content_hash: "abc",
          latest_run: {
            run_id: "run-1",
            status: "running",
            chars_out: null,
            errors: null,
            duration_ms: null,
          },
          parsed_preview: null,
        },
        200
      )
    );

    const result = await getDocStatus("doc-1");

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.run?.status).toBe("running");
      expect(result.docStatus).toBe("pending");
    }
  });

  it("maps a 200 body without latest_run to a DocStatusOk with run: null", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          doc_id: "doc-2",
          filename: "faq.txt",
          content_type: "text/plain",
          status: "pending",
          content_hash: "abc",
          latest_run: null,
          parsed_preview: null,
        },
        200
      )
    );

    const result = await getDocStatus("doc-2");

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.run).toBeNull();
    }
  });

  it("maps a 404 DOC_NOT_FOUND to an error variant", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(404, {
        error_code: "DOC_NOT_FOUND",
        message: "Not found.",
        correlation_id: "corr-5",
      })
    );

    const result = await getDocStatus("doc-missing");

    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.errorCode).toBe("DOC_NOT_FOUND");
    }
  });

  it("maps a network throw to an error variant", async () => {
    adminApiFetchMock.mockRejectedValue(new TypeError("fetch failed"));

    const result = await getDocStatus("doc-3");

    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/unable to reach the server/i);
    }
  });
});

describe("previewChat (Train the Agent: test the bot)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    adminApiFetchMock.mockReset();
  });

  it("maps a 200 body to an ok result with camelCase fields", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          reply: "We're open Monday through Friday.",
          decision: "answer",
          confidence: 0.82,
          sources: [{ doc_id: "doc-1", chunk_id: "c1", score: 0.9, matched_by: ["vector"] }],
        },
        200
      )
    );

    const result = await previewChat("what are your hours?");

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.reply).toBe("We're open Monday through Friday.");
      expect(result.decision).toBe("answer");
      expect(result.confidence).toBe(0.82);
      expect(result.sources).toEqual([
        { docId: "doc-1", chunkId: "c1", score: 0.9, matchedBy: ["vector"] },
      ]);
    }
  });

  it("posts the message as JSON to the implicit path when tenantId is omitted", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({ reply: "hi", decision: "answer", confidence: null, sources: [] }, 200)
    );

    await previewChat("hello");

    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/admin/training/chat",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ message: "hello" }) })
    );
  });

  it("targets the tenant-scoped path when tenantId is provided", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({ reply: "hi", decision: "answer", confidence: null, sources: [] }, 200)
    );

    await previewChat("hello", "tenant-x");

    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/admin/tenants/tenant-x/training/chat",
      expect.objectContaining({ method: "POST" })
    );
  });

  it("maps an AdminApiError to an error result", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(422, {
        error_code: "LLM_NOT_CONFIGURED",
        message: "LLM is not configured.",
        correlation_id: "corr-chat-1",
      })
    );

    const result = await previewChat("hello");

    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.correlationId).toBe("corr-chat-1");
    }
  });

  it("maps a network throw to a generic error result", async () => {
    adminApiFetchMock.mockRejectedValue(new TypeError("fetch failed"));

    const result = await previewChat("hello");

    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/unable to reach the server/i);
    }
  });
});

describe("suggestDraftAnswer (Train the Agent: suggest a reply)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    adminApiFetchMock.mockReset();
  });

  it("maps a 200 body to an ok result with the draft suggestion", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({ suggestion: "We serve the greater Atlanta area." }, 200)
    );

    const result = await suggestDraftAnswer("What areas do you serve?");

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.suggestion).toBe("We serve the greater Atlanta area.");
    }
  });

  it("posts the question as JSON to the implicit path when tenantId is omitted", async () => {
    adminApiFetchMock.mockResolvedValue(jsonResponse({ suggestion: "A draft." }, 200));

    await suggestDraftAnswer("What areas do you serve?");

    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/admin/training/suggest-answer",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ question: "What areas do you serve?" }),
      })
    );
  });

  it("targets the tenant-scoped path when tenantId is provided", async () => {
    adminApiFetchMock.mockResolvedValue(jsonResponse({ suggestion: "A draft." }, 200));

    await suggestDraftAnswer("What areas do you serve?", "tenant-x");

    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/admin/tenants/tenant-x/training/suggest-answer",
      expect.objectContaining({ method: "POST" })
    );
  });

  it("maps an AdminApiError to an error result", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(422, {
        error_code: "LLM_NOT_CONFIGURED",
        message: "LLM is not configured.",
        correlation_id: "corr-suggest-1",
      })
    );

    const result = await suggestDraftAnswer("What areas do you serve?");

    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.correlationId).toBe("corr-suggest-1");
    }
  });

  it("maps a network throw to a generic error result", async () => {
    adminApiFetchMock.mockRejectedValue(new TypeError("fetch failed"));

    const result = await suggestDraftAnswer("What areas do you serve?");

    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/unable to reach the server/i);
    }
  });

  it("does not call revalidatePath -- nothing is saved by this action", async () => {
    adminApiFetchMock.mockResolvedValue(jsonResponse({ suggestion: "A draft." }, 200));

    await suggestDraftAnswer("What areas do you serve?");

    expect(revalidatePathMock).not.toHaveBeenCalled();
  });
});

describe("listCoverageGaps (Train the Agent: coverage check)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    adminApiFetchMock.mockReset();
  });

  it("maps a 200 body to an ok result with camelCase fields", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse(
        {
          gaps: [
            {
              message_id: "m1",
              question: "How much does an inspection cost?",
              question_message_id: "q1",
              decision: "escalate",
              confidence: 0.1,
              created_at: "2026-08-18T12:00:00Z",
            },
          ],
        },
        200
      )
    );

    const result = await listCoverageGaps();

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.gaps).toEqual([
        {
          messageId: "m1",
          question: "How much does an inspection cost?",
          questionMessageId: "q1",
          decision: "escalate",
          confidence: 0.1,
          createdAt: "2026-08-18T12:00:00Z",
        },
      ]);
    }
  });

  it("maps an empty gaps array to an ok result with an empty list", async () => {
    adminApiFetchMock.mockResolvedValue(jsonResponse({ gaps: [] }, 200));

    const result = await listCoverageGaps();

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.gaps).toEqual([]);
    }
  });

  it("targets the tenant-scoped path when tenantId is provided", async () => {
    adminApiFetchMock.mockResolvedValue(jsonResponse({ gaps: [] }, 200));

    await listCoverageGaps("tenant-x");

    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/admin/tenants/tenant-x/training/gaps",
      expect.objectContaining({ method: "GET" })
    );
  });

  it("maps a network throw to a generic error result", async () => {
    adminApiFetchMock.mockRejectedValue(new TypeError("fetch failed"));

    const result = await listCoverageGaps();

    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/unable to reach the server/i);
    }
  });
});

describe("submitTrainedAnswer (Train the Agent: teach an answer)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    adminApiFetchMock.mockReset();
    revalidatePathMock.mockReset();
  });

  it("maps a 200 body to an ok result and revalidates /knowledge", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({ doc_id: "doc-1", run_id: "run-1", training_answer_id: "ta-1" }, 200)
    );

    const result = await submitTrainedAnswer("How much?", "Free.", "q1");

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.docId).toBe("doc-1");
      expect(result.runId).toBe("run-1");
      expect(result.trainingAnswerId).toBe("ta-1");
    }
    expect(revalidatePathMock).toHaveBeenCalledWith("/knowledge");
  });

  it("posts question/answer/source_message_id as JSON to the implicit path", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({ doc_id: "doc-1", run_id: "run-1", training_answer_id: "ta-1" }, 200)
    );

    await submitTrainedAnswer("How much?", "Free.", "q1");

    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/admin/training/answer",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ question: "How much?", answer: "Free.", source_message_id: "q1" }),
      })
    );
  });

  it("targets the tenant-scoped path and revalidates the tenant-scoped route when tenantId is provided", async () => {
    adminApiFetchMock.mockResolvedValue(
      jsonResponse({ doc_id: "doc-1", run_id: "run-1", training_answer_id: "ta-1" }, 200)
    );

    await submitTrainedAnswer("How much?", "Free.", undefined, "tenant-x");

    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/admin/tenants/tenant-x/training/answer",
      expect.objectContaining({ method: "POST" })
    );
    expect(revalidatePathMock).toHaveBeenCalledWith("/clients/tenant-x/knowledge");
  });

  it("maps an AdminApiError to an error result and does NOT revalidate", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(422, {
        error_code: "VALIDATION_ERROR",
        message: "must not be blank",
        correlation_id: "corr-answer-1",
      })
    );

    const result = await submitTrainedAnswer("How much?", "");

    expect(result.status).toBe("error");
    expect(revalidatePathMock).not.toHaveBeenCalled();
  });

  it("maps a network throw to a generic error result", async () => {
    adminApiFetchMock.mockRejectedValue(new TypeError("fetch failed"));

    const result = await submitTrainedAnswer("How much?", "Free.");

    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/unable to reach the server/i);
    }
  });
});

describe("dismissGap (Train the Agent: not a real question)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    adminApiFetchMock.mockReset();
    revalidatePathMock.mockReset();
  });

  it("maps a 200 body to an ok result and revalidates /knowledge", async () => {
    adminApiFetchMock.mockResolvedValue(jsonResponse({ training_answer_id: "ta-3" }, 200));

    const result = await dismissGap("I won't.", "q1");

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.trainingAnswerId).toBe("ta-3");
    }
    expect(revalidatePathMock).toHaveBeenCalledWith("/knowledge");
  });

  it("posts question/source_message_id as JSON to the implicit path", async () => {
    adminApiFetchMock.mockResolvedValue(jsonResponse({ training_answer_id: "ta-3" }, 200));

    await dismissGap("I won't.", "q1");

    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/admin/training/dismiss",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ question: "I won't.", source_message_id: "q1" }),
      })
    );
  });

  it("targets the tenant-scoped path and revalidates the tenant-scoped route when tenantId is provided", async () => {
    adminApiFetchMock.mockResolvedValue(jsonResponse({ training_answer_id: "ta-3" }, 200));

    await dismissGap("I won't.", undefined, "tenant-x");

    expect(adminApiFetchMock).toHaveBeenCalledWith(
      "/admin/tenants/tenant-x/training/dismiss",
      expect.objectContaining({ method: "POST" })
    );
    expect(revalidatePathMock).toHaveBeenCalledWith("/clients/tenant-x/knowledge");
  });

  it("maps an AdminApiError to an error result and does NOT revalidate", async () => {
    adminApiFetchMock.mockRejectedValue(
      new AdminApiError(422, {
        error_code: "VALIDATION_ERROR",
        message: "must not be blank",
        correlation_id: "corr-dismiss-1",
      })
    );

    const result = await dismissGap("");

    expect(result.status).toBe("error");
    expect(revalidatePathMock).not.toHaveBeenCalled();
  });

  it("maps a network throw to a generic error result", async () => {
    adminApiFetchMock.mockRejectedValue(new TypeError("fetch failed"));

    const result = await dismissGap("I won't.");

    expect(result.status).toBe("error");
    if (result.status === "error") {
      expect(result.message).toMatch(/unable to reach the server/i);
    }
  });
});
