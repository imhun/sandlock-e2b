import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["test/**/*.test.js"],
    // 120s, not the 30s this used to be: a *create* through the local
    // control plane measures ~20s on this host (2026-10-11: the `command
    // result` case took 23.3s end to end, `lifecycle` timed out at 30s on the
    // same run), so the old budget turned the box's own latency into a red
    // test. The SDK's own request timeouts are what bound a *hang*; this pair
    // only has to be looser than the slowest honest create here.
    testTimeout: 120000,
    hookTimeout: 120000,
  },
});

