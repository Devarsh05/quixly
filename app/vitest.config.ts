import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

// A standalone config on purpose: vite.config.ts loads the React Router plugin and
// rewrites SHOPIFY_APP_URL/HOST for the dev server, none of which should run under test.
export default defineConfig(({ mode }) => ({
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
    // The token-rotation tests exercise a real Postgres advisory lock, so they need
    // DATABASE_URL. loadEnv with an empty prefix reads every key from .env, not just VITE_*.
    env: loadEnv(mode, process.cwd(), ""),
    // Advisory locks serialize across connections; running these files in parallel would
    // have them contend on the same shop keys.
    fileParallelism: false,
  },
}));
