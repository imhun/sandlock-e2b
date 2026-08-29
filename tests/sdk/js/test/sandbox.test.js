import { beforeAll, afterAll, expect, test } from "vitest";
import { Sandbox } from "e2b";

const API_URL = process.env.E2B_API_URL || "http://127.0.0.1:3000";
const SANDBOX_URL = process.env.E2B_SANDBOX_URL || "http://127.0.0.1:49983";
const API_KEY = process.env.E2B_API_KEY || "local-key";

function opts() {
  return { apiUrl: API_URL, sandboxUrl: SANDBOX_URL, apiKey: API_KEY };
}

test("lifecycle", async () => {
  const sandbox = await Sandbox.create("base", opts());
  try {
    expect(await sandbox.isRunning()).toBe(true);
    const info = await sandbox.getInfo();
    expect(info.sandboxId).toBe(sandbox.sandboxId);
    expect(info.state).toBe("running");
  } finally {
    expect(await sandbox.kill()).toBe(true);
  }
});

test("command result", async () => {
  const sandbox = await Sandbox.create("base", opts());
  try {
    const result = await sandbox.commands.run("echo hello");
    expect(result.stdout).toBe("hello\n");
    expect(result.stderr).toBe("");
    expect(result.exitCode).toBe(0);
  } finally {
    await sandbox.kill();
  }
});

test("non-zero exit code", async () => {
  const sandbox = await Sandbox.create("base", opts());
  try {
    await expect(sandbox.commands.run("exit 7")).rejects.toMatchObject({
      exitCode: 7,
    });
  } finally {
    await sandbox.kill();
  }
});

test("background command with stdin", async () => {
  const sandbox = await Sandbox.create("base", opts());
  try {
    const proc = await sandbox.commands.run("cat", { background: true, stdin: true });
    expect(proc.pid).toBeGreaterThan(0);
    await proc.sendStdin("js-stdin\n");
    await proc.closeStdin();
    const result = await proc.wait();
    expect(result.stdout).toBe("js-stdin\n");
    expect(result.exitCode).toBe(0);
  } finally {
    await sandbox.kill();
  }
});

test("files roundtrip", async () => {
  const sandbox = await Sandbox.create("base", opts());
  try {
    const info = await sandbox.files.write("workspace/js.txt", "js-files");
    expect(info.name).toBe("js.txt");
    expect(info.path).toBe("workspace/js.txt");
    expect(await sandbox.files.read("workspace/js.txt")).toBe("js-files");
    expect(await sandbox.files.exists("workspace/js.txt")).toBe(true);
    await sandbox.files.remove("workspace/js.txt");
    expect(await sandbox.files.exists("workspace/js.txt")).toBe(false);
  } finally {
    await sandbox.kill();
  }
});

test("template image create", async () => {
  const sandbox = await Sandbox.create("py311", opts());
  try {
    const result = await sandbox.commands.run("echo template-js");
    expect(result.stdout).toBe("template-js\n");
    expect(result.exitCode).toBe(0);
  } finally {
    await sandbox.kill();
  }
});

