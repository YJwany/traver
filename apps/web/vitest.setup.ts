import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach } from "vitest";
import { cleanup } from "@testing-library/react";

// React Testing Library does not auto-clean without a global test runner
// teardown, and a leaked component keeps its event listeners on `window`, which
// makes ordering-dependent failures.
afterEach(() => {
  cleanup();
});

beforeEach(() => {
  window.localStorage.clear();
});
