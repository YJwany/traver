import { describe, expect, it } from "vitest";

import { EXPORT_PATHS, fallbackFilename, parseContentDisposition } from "./export";

/**
 * The filename rules, tested on their own.
 *
 * The server sends two names in the same header because HTTP header values are latin-1
 * and a Chinese destination cannot go in a plain one:
 *
 *   attachment; filename="trip-2026-10-03.md"; filename*=UTF-8''%E6%9D%AD%E5%B7%9E-2026-10-03.md
 *
 * The first is the ASCII fallback, the second the real name. Reading the wrong one is not
 * an error — the download still succeeds — it just produces a file called `trip-...` when
 * the plan is for 杭州. That is the sort of defect nothing complains about, so the parsing
 * is pinned here rather than left to a manual click.
 */

describe("parseContentDisposition", () => {
  it("prefers the RFC 5987 name, which is the only one that can carry Chinese", () => {
    const header =
      "attachment; filename=\"trip-2026-10-03.md\"; " +
      "filename*=UTF-8''%E6%9D%AD%E5%B7%9E-2026-10-03.md";
    expect(parseContentDisposition(header, "fallback.md")).toBe("杭州-2026-10-03.md");
  });

  it("falls back to the plain name when no extended one is sent", () => {
    expect(parseContentDisposition('attachment; filename="trip-2026-10-03.md"', "f.md")).toBe(
      "trip-2026-10-03.md",
    );
  });

  it("accepts an unquoted plain name", () => {
    expect(parseContentDisposition("attachment; filename=trip.ics", "f.ics")).toBe("trip.ics");
  });

  it("uses the caller's name when the header is missing entirely", () => {
    // Not an error: a proxy may strip the header, and the download must still work.
    expect(parseContentDisposition(null, "杭州.ics")).toBe("杭州.ics");
    expect(parseContentDisposition("attachment", "杭州.ics")).toBe("杭州.ics");
  });

  it("survives malformed percent-encoding instead of failing the download", () => {
    const header = "attachment; filename=\"plain.md\"; filename*=UTF-8''%E6%9D%AD%E5"
    expect(parseContentDisposition(header, "fallback.md")).toBe("plain.md");
  });

  it("does not let a percent-encoded path separator escape into a directory", () => {
    // %2F is a slash. Keeping it would put the file somewhere the user did not choose.
    const header = "attachment; filename*=UTF-8''..%2F..%2Fetc%2Fpasswd";
    expect(parseContentDisposition(header, "fallback.md")).toBe("passwd");
  });

  it("is case-insensitive about the parameter name and encoding label", () => {
    const header = "attachment; FILENAME*=utf-8''%E6%9D%AD%E5%B7%9E.md";
    expect(parseContentDisposition(header, "fallback.md")).toBe("杭州.md");
  });
});

describe("fallbackFilename", () => {
  it("keeps a Chinese destination rather than stripping it to nothing", () => {
    // The reason this is not "keep only letters and digits": `\w` is ASCII in JavaScript,
    // so that rule would delete the entire name and yield `trip.md`.
    expect(fallbackFilename("杭州", "md")).toBe("杭州.md");
  });

  it("replaces the characters a filesystem reserves", () => {
    expect(fallbackFilename("a/b\\c:d*e?f", "ics")).toBe("a-b-c-d-e-f.ics");
  });

  it("collapses whitespace and trims the result", () => {
    expect(fallbackFilename("  New   York  ", "md")).toBe("New-York.md");
  });

  it("never returns an empty base name", () => {
    expect(fallbackFilename("///", "md")).toBe("trip.md");
  });
});

describe("EXPORT_PATHS", () => {
  it("matches the routes the API actually serves", () => {
    // Spelled once so the button labels and the requests cannot drift apart. A mismatch
    // here is a 404 the user sees as "导出失败" with no hint about which half is wrong.
    expect(EXPORT_PATHS.md).toBe("export.md");
    expect(EXPORT_PATHS.ics).toBe("export.ics");
  });
});
