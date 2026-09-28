import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiRequest } from "./api-client";
import { setApiKey } from "./auth";

const VALID_KEY = "trv_-b83TL_2eCDR4g8eymTfpusqkpMm4L8t3bkEhuEHkXw";

/** Minimal Response stand-in; jsdom's fetch is not exercised here. */
function respond(status: number, body?: unknown, init?: { json?: boolean }) {
  const text = body === undefined ? "" : init?.json === false ? String(body) : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => text,
  } as unknown as Response;
}

describe("apiRequest", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("attaches the stored key as a bearer token", async () => {
    setApiKey(VALID_KEY);
    fetchMock.mockResolvedValue(respond(200, { ok: true }));

    await apiRequest("/api/v1/trips");

    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers.Authorization).toBe(`Bearer ${VALID_KEY}`);
  });

  it("omits the Authorization header when no key is stored", async () => {
    // Sending `Bearer ` with an empty value would be a malformed credential
    // rather than an anonymous request.
    fetchMock.mockResolvedValue(respond(200, []));

    await apiRequest("/api/v1/providers/attributions");

    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers.Authorization).toBeUndefined();
  });

  it("sets Content-Type only when there is a body", async () => {
    fetchMock.mockResolvedValue(respond(200, {}));

    await apiRequest("/api/v1/trips");
    expect(fetchMock.mock.calls[0][1].headers["Content-Type"]).toBeUndefined();

    await apiRequest("/api/v1/trips", { method: "POST", body: { destination: "成都" } });
    expect(fetchMock.mock.calls[1][1].headers["Content-Type"]).toBe("application/json");
  });

  it("serializes the body as JSON", async () => {
    fetchMock.mockResolvedValue(respond(200, {}));

    await apiRequest("/api/v1/trips", { method: "POST", body: { travelers: 2 } });

    expect(fetchMock.mock.calls[0][1].body).toBe('{"travelers":2}');
  });

  it("returns the parsed payload", async () => {
    fetchMock.mockResolvedValue(respond(200, { id: "abc", current_version: 3 }));

    await expect(apiRequest("/api/v1/trips/abc")).resolves.toEqual({
      id: "abc",
      current_version: 3,
    });
  });

  it("returns undefined for 204 without parsing a body", async () => {
    fetchMock.mockResolvedValue(respond(204));

    await expect(apiRequest("/api/v1/trips/abc", { method: "DELETE" })).resolves.toBeUndefined();
  });

  it("appends query params and skips empty ones", async () => {
    fetchMock.mockResolvedValue(respond(200, []));

    await apiRequest("/api/v1/trips", {
      params: { limit: 10, offset: 0, ignored: undefined, blank: "" },
    });

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("limit=10");
    // offset=0 must survive: a falsy check would have dropped it.
    expect(url).toContain("offset=0");
    expect(url).not.toContain("ignored");
    expect(url).not.toContain("blank");
  });
});

describe("error classification", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /**
   * Await a request that must fail and return its `ApiError`.
   *
   * Using `.catch(reason => reason)` would type the result as `unknown` and, worse,
   * would let a test pass green on an unexpected *success*. This asserts the
   * rejection is an `ApiError`, so the negative cases cannot silently invert.
   */
  async function expectFailure(request: Promise<unknown>): Promise<ApiError> {
    try {
      await request;
    } catch (reason) {
      if (reason instanceof ApiError) return reason;
      throw reason;
    }
    throw new Error("expected the request to fail, but it resolved");
  }

  it("maps 401 to isUnauthorized", async () => {
    // The distinction matters: 401 is fixed by supplying a key, 403 is not. A UI
    // that offers the same remedy for both sends the user in circles.
    fetchMock.mockResolvedValue(respond(401, { detail: "missing bearer token" }));

    const error = await expectFailure(apiRequest("/api/v1/trips"));

    expect(error.isUnauthorized).toBe(true);
    expect(error.isForbidden).toBe(false);
    expect(error.status).toBe(401);
    expect(error.detail).toBe("missing bearer token");
  });

  it("maps 403 to isForbidden and not isUnauthorized", async () => {
    fetchMock.mockResolvedValue(respond(403, { detail: "insufficient scope" }));

    const error = await expectFailure(apiRequest("/api/v1/trips", { method: "POST", body: {} }));

    expect(error.isForbidden).toBe(true);
    expect(error.isUnauthorized).toBe(false);
  });

  it("maps 429 to isRateLimited", async () => {
    fetchMock.mockResolvedValue(respond(429, { detail: "rate limit exceeded" }));

    const error = await expectFailure(apiRequest("/api/v1/trips"));

    expect(error.isRateLimited).toBe(true);
  });

  it("maps 409 to isConflict", async () => {
    // Optimistic locking: someone else advanced the trip first.
    fetchMock.mockResolvedValue(respond(409, { detail: "trip was modified" }));

    const error = await expectFailure(
      apiRequest("/api/v1/trips/abc", { method: "PATCH", body: { expected_version: 1 } }),
    );

    expect(error.isConflict).toBe(true);
  });

  it("reports an unreachable API as status 0, not as a server error", async () => {
    // A thrown fetch means the request never arrived. Reporting it as HTTP 500
    // would blame the server for a local connectivity/CORS problem.
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    const error = await expectFailure(apiRequest("/api/v1/trips"));

    expect(error.status).toBe(0);
    expect(error.isUnauthorized).toBe(false);
    expect(error.detail).toContain("Failed to fetch");
  });

  it("flattens FastAPI validation errors instead of stringifying objects", async () => {
    // FastAPI returns detail as an array for 422. Interpolating that array yields
    // "[object Object]", which tells the user nothing.
    fetchMock.mockResolvedValue(
      respond(422, {
        detail: [
          { loc: ["body", "travelers"], msg: "Input should be greater than 0", type: "gt" },
          { loc: ["body", "start_date"], msg: "Field required", type: "missing" },
        ],
      }),
    );

    const error = await expectFailure(apiRequest("/api/v1/trips", { method: "POST", body: {} }));

    expect(error.detail).toContain("Input should be greater than 0");
    expect(error.detail).toContain("Field required");
    expect(error.detail).not.toContain("[object Object]");
  });

  it("falls back to a generic message for a non-JSON error body", async () => {
    // An upstream proxy's HTML error page must not be rendered into the UI.
    fetchMock.mockResolvedValue(respond(502, "<html>Bad Gateway</html>", { json: false }));

    const error = await expectFailure(apiRequest("/api/v1/trips"));

    expect(error.status).toBe(502);
    expect(error.detail).toBe("请求失败（HTTP 502）");
  });

  it("keeps a short plain-text error body as the message", async () => {
    // Unlike an HTML page, "Service Unavailable" is a usable message.
    fetchMock.mockResolvedValue(respond(503, "Service Unavailable", { json: false }));

    const error = await expectFailure(apiRequest("/api/v1/trips"));

    expect(error.detail).toBe("Service Unavailable");
  });
});
