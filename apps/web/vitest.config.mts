import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

// jsdom, because the units under test are browser-facing: they read
// `localStorage`, build `fetch` requests and dispatch DOM events. A node
// environment would force mocks for all three and test less real behaviour.
//
// This file is `.mts`, not `.ts`: `@vitejs/plugin-react` is ESM-only, and a
// CommonJS-transpiled config cannot `require()` it.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["**/*.test.{ts,tsx}"],
    // `.next` holds compiled output that also matches the include pattern.
    exclude: ["node_modules/**", ".next/**"],
  },
  resolve: {
    alias: {
      // ESM has no `__dirname`; derive it from the module URL instead.
      "@": fileURLToPath(new URL(".", import.meta.url)),
    },
  },
});
