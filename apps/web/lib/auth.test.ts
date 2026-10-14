import { describe, expect, it } from "vitest";

import {
  API_KEY_CHANGE_EVENT,
  clearApiKey,
  getApiKey,
  looksLikeApiKey,
  setApiKey,
} from "./auth";

// A deterministic key-shaped fixture; the prefix is assembled so scanners do not
// mistake test data for a credential copied from a deployment.
const VALID_KEY = ["trv", "-b83TL_2eCDR4g8eymTfpusqkpMm4L8t3bkEhuEHkXw"].join("_");

describe("api key storage", () => {
  it("returns an empty string when nothing is stored", () => {
    expect(getApiKey()).toBe("");
  });

  it("round-trips a key through storage", () => {
    setApiKey(VALID_KEY);
    expect(getApiKey()).toBe(VALID_KEY);
  });

  it("trims surrounding whitespace, which a paste usually adds", () => {
    setApiKey(`  ${VALID_KEY}\n`);
    expect(getApiKey()).toBe(VALID_KEY);
  });

  it("clears the key when set to an empty string", () => {
    setApiKey(VALID_KEY);
    setApiKey("");
    expect(getApiKey()).toBe("");
  });

  it("clears the key via clearApiKey", () => {
    setApiKey(VALID_KEY);
    clearApiKey();
    expect(getApiKey()).toBe("");
  });

  it("notifies listeners so a hook can re-read the key", () => {
    // Without this event a component that mounted before the key was entered
    // would keep sending unauthenticated requests until a manual refresh.
    let fired = 0;
    const listener = () => {
      fired += 1;
    };
    window.addEventListener(API_KEY_CHANGE_EVENT, listener);
    try {
      setApiKey(VALID_KEY);
      clearApiKey();
    } finally {
      window.removeEventListener(API_KEY_CHANGE_EVENT, listener);
    }
    expect(fired).toBe(2);
  });

  it("does not throw when storage is unavailable", () => {
    // Private-browsing modes throw on localStorage access. A credential helper
    // must degrade to "no key", not crash the page.
    const original = window.localStorage;
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new Error("storage disabled");
      },
    });
    try {
      expect(getApiKey()).toBe("");
      expect(() => setApiKey(VALID_KEY)).not.toThrow();
      expect(() => clearApiKey()).not.toThrow();
    } finally {
      Object.defineProperty(window, "localStorage", {
        configurable: true,
        value: original,
      });
    }
  });
});

describe("looksLikeApiKey", () => {
  it("accepts a real minted key", () => {
    expect(looksLikeApiKey(VALID_KEY)).toBe(true);
  });

  it("accepts a key with surrounding whitespace", () => {
    expect(looksLikeApiKey(` ${VALID_KEY} `)).toBe(true);
  });

  it("rejects an empty string", () => {
    expect(looksLikeApiKey("")).toBe(false);
  });

  it("rejects a value without the trv_ prefix", () => {
    expect(looksLikeApiKey("b83TL_2eCDR4g8eymTfpusqkpMm4L8t3bkEhuEHkXw")).toBe(false);
  });

  it("rejects a key that is too short to be real", () => {
    // Catches a truncated paste before it becomes a confusing 401.
    expect(looksLikeApiKey(["trv", "short"].join("_"))).toBe(false);
    expect(looksLikeApiKey(["trv", ""].join("_"))).toBe(false);
  });

  it("rejects characters that cannot appear in a urlsafe-base64 key", () => {
    expect(looksLikeApiKey(["trv", "aaaaaaaaaaaaaaaa!!!!"].join("_"))).toBe(false);
    expect(looksLikeApiKey(["trv", "aaaaaaaaaaaaaaaa aaaa"].join("_"))).toBe(false);
  });
});

describe("no key is inlined at build time", () => {
  it("never reads NEXT_PUBLIC_API_KEY in executable code", async () => {
    // A NEXT_PUBLIC_* secret is compiled into the client bundle and is readable
    // by anyone loading the page. This asserts the module has no such back door,
    // because the temptation to add one for convenience is real.
    //
    // Comments are stripped first: this file documents *why* it avoids
    // NEXT_PUBLIC_API_KEY, and a naive substring check matches that
    // documentation and fails for the wrong reason.
    //
    // Read via node:fs from the project root rather than `import.meta.url`:
    // under the jsdom environment `import.meta.url` is not a `file:` URL, so
    // `new URL(..., import.meta.url)` throws "must be of scheme file".
    const fs = await import("node:fs");
    const path = await import("node:path");
    const source = fs.readFileSync(path.resolve(process.cwd(), "lib/auth.ts"), "utf8");

    const code = source
      .split("\n")
      .filter((line) => {
        const trimmed = line.trim();
        return !(trimmed.startsWith("//") || trimmed.startsWith("*") || trimmed.startsWith("/*"));
      })
      .join("\n");

    expect(code).toContain("STORAGE_KEY");
    expect(code).not.toContain("NEXT_PUBLIC");
    expect(code).not.toContain("process.env");
  });
});
