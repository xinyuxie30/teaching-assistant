import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError, apiUrl } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

describe("authenticated writes", () => {
  it("reuses exact request bytes and key after a lost response, surviving a page revisit", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockRejectedValueOnce(new TypeError("connection closed"))
      .mockResolvedValueOnce(response({ revision: 1 }))
      .mockResolvedValueOnce(response({ revision: 2 }));
    vi.stubGlobal("fetch", fetch);
    const api = new ApiClient("local-test-token");
    const operation = api.operation("review:item");
    const body = { expected_revision: 0, decisions: [{ reason: "中文\r\n😀" }] };
    await expect(operation.run("/v1/grading-items/item/reviews", body)).rejects.toBeInstanceOf(ApiError);
    expect(operation.snapshot().phase).toBe("uncertain");
    expect(api.hasUnresolvedWrites()).toBe(true);
    await expect(operation.run("/v1/grading-items/item/reviews", { expected_revision: 1 }))
      .rejects.toMatchObject({ code: "pending_operation" });
    expect(fetch).toHaveBeenCalledTimes(1);
    const revisited = api.operation("review:item");
    expect(revisited).toBe(operation);
    expect(await revisited.retry()).toEqual({ revision: 1 });
    const first = fetch.mock.calls[0][1]!;
    const second = fetch.mock.calls[1][1]!;
    expect(second.body).toBe(first.body);
    expect(new Headers(second.headers).get("Idempotency-Key"))
      .toBe(new Headers(first.headers).get("Idempotency-Key"));
    expect(new Headers(first.headers).get("Authorization")).toBe("Bearer local-test-token");
    expect(first.credentials).toBe("omit");
    expect(api.hasUnresolvedWrites()).toBe(false);
    await revisited.run("/v1/grading-items/item/reviews", body);
    expect(new Headers(fetch.mock.calls[2][1]!.headers).get("Idempotency-Key"))
      .not.toBe(new Headers(first.headers).get("Idempotency-Key"));
  });

  it("does not resubmit on conflict; a changed review creates a new intention", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(response({ code: "revision_conflict" }, 409))
      .mockResolvedValueOnce(response({ revision: 3 }));
    vi.stubGlobal("fetch", fetch);
    const operation = new ApiClient("test").operation("review:item");
    await expect(operation.run("/v1/grading-items/item/reviews", { expected_revision: 1 }))
      .rejects.toMatchObject({ code: "revision_conflict" });
    expect(operation.snapshot().phase).toBe("failed");
    expect(fetch).toHaveBeenCalledTimes(1);
    await operation.run("/v1/grading-items/item/reviews", { expected_revision: 2 });
    const keys = fetch.mock.calls.map(([, options]) => new Headers(options!.headers).get("Idempotency-Key"));
    expect(keys[0]).not.toBe(keys[1]);
  });

  it("locks an unreadable successful receipt until the same operation is retried", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(new Response("broken JSON", { status: 201 }))
      .mockResolvedValueOnce(response({ id: "original-export" }, 201));
    vi.stubGlobal("fetch", fetch);
    const operation = new ApiClient("test").operation("export:batch");
    await expect(operation.run("/v1/grading-batches/batch/exports", { mode: "working" }))
      .rejects.toMatchObject({ code: "unreadable_response" });
    expect(operation.snapshot().phase).toBe("uncertain");
    expect(await operation.retry()).toEqual({ id: "original-export" });
  });

  it("keeps the request deadline active while reading the response body", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(async (_path: string, options: RequestInit) => ({
      ok: true, status: 200,
      json: () => new Promise((_resolve, reject) => {
        options.signal!.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
      }),
    })));
    const request = new ApiClient("test").get("/v1/me");
    const rejected = expect(request).rejects.toMatchObject({ code: "unreadable_response" });
    await vi.advanceTimersByTimeAsync(30_000);
    await rejected;
  });

  it("never attaches credentials to an absolute URL or an escaped API path", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>();
    vi.stubGlobal("fetch", fetch);
    const api = new ApiClient("test");
    await expect(api.get("https://unrelated.invalid/v1/me")).rejects.toThrow();
    await expect(api.get("/v1/../outside")).rejects.toThrow();
    expect(fetch).not.toHaveBeenCalled();
    expect(apiUrl("/v1/grading-items", { status: "succeeded", batch_id: "b", cursor: undefined }))
      .toBe("/v1/grading-items?batch_id=b&status=succeeded");
  });

  it("keeps import previews read-only and cannot reset an unresolved creation", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(response({ valid: true, row_count: 1, reused: 0 }))
      .mockRejectedValueOnce(new TypeError("lost response"))
      .mockResolvedValueOnce(response({ id: "original-import" }, 201));
    vi.stubGlobal("fetch", fetch);
    const api = new ApiClient("test");
    const payload = { rows: [{ answer_text: "  😀\r\n  " }] };
    await api.previewImport("assignment", payload);
    expect(fetch.mock.calls[0][0]).toBe("/v1/assignments/assignment/imports/preview");
    expect(new Headers(fetch.mock.calls[0][1]!.headers).has("Idempotency-Key")).toBe(false);
    expect(api.hasUnresolvedWrites()).toBe(false);
    const operation = api.operation("prepare:import:q1");
    await expect(operation.run("/v1/assignments/assignment/imports", payload)).rejects.toBeInstanceOf(ApiError);
    expect(() => operation.reset()).toThrow("Resolve the previous request first");
    expect(await operation.retry()).toEqual({ id: "original-import" });
    expect(fetch.mock.calls[2][1]!.body).toBe(fetch.mock.calls[1][1]!.body);
    expect(new Headers(fetch.mock.calls[2][1]!.headers).get("Idempotency-Key"))
      .toBe(new Headers(fetch.mock.calls[1][1]!.headers).get("Idempotency-Key"));
    operation.reset();
    expect(operation.snapshot()).toEqual({ phase: "idle" });
  });
});
