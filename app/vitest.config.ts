import { defineConfig } from "vitest/config";

// A standalone config on purpose: vite.config.ts loads the React Router plugin and
// rewrites SHOPIFY_APP_URL/HOST for the dev server, none of which should run under test.
export default defineConfig({
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
  },
});
