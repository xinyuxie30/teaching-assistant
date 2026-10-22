import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import { join, relative } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

export const root = fileURLToPath(new URL("../../", import.meta.url));

export function configuration() {
  const url = new URL(process.env.TA_E2E_BASE_URL || "http://127.0.0.1:5173");
  assert(["http:", "https:"].includes(url.protocol)
    && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
    && !url.username && !url.password && url.pathname === "/" && !url.search && !url.hash,
  "TA_E2E_BASE_URL must be a loopback HTTP(S) origin without credentials or a path");
  const token = process.env.TA_E2E_TOKEN?.trim();
  assert(token && token.length >= 20, "Set TA_E2E_TOKEN to the isolated test backend's development token");
  const channel = process.env.TA_E2E_BROWSER_CHANNEL || "chrome";
  return {
    origin: url.origin, token,
    browserOptions: {
      headless: true, timeout: 30_000,
      ...(process.env.TA_E2E_BROWSER_PATH
        ? { executablePath: process.env.TA_E2E_BROWSER_PATH }
        : channel === "chromium" ? {} : { channel }),
    },
  };
}

export async function eventually(description, assertion, timeout = 15_000) {
  const deadline = Date.now() + timeout;
  let lastError;
  do {
    try { return await assertion(); }
    catch (error) {
      // Poll an unmet assertion, not an HTTP error or a broken selector.
      if (!(error instanceof assert.AssertionError)) throw error;
      lastError = error;
    }
    await delay(250);
  } while (Date.now() < deadline);
  throw new Error(`Timed out: ${description}\n${lastError?.message || ""}`, { cause: lastError });
}

export async function postAction(page, origin, path, action) {
  const [response] = await Promise.all([
    page.waitForResponse((candidate) => candidate.url() === new URL(path, origin).href
      && candidate.request().method() === "POST"),
    action(),
  ]);
  assert(response.ok(), `POST ${path}: HTTP ${response.status()}`);
  const request = response.request();
  if (!path.endsWith("/preview")) {
    assert(request.headers()["idempotency-key"], `${path} must carry an idempotency key`);
  }
  return { body: await response.json(), sent: request.postDataJSON() };
}

export async function readJson(context, config, path) {
  const response = await context.request.get(new URL(path, config.origin).href, {
    headers: { Authorization: `Bearer ${config.token}` }, timeout: 15_000,
    maxRedirects: 0,
  });
  try {
    if (response.status() !== 200) throw new Error(`GET ${path}: HTTP ${response.status()}`);
    return await response.json();
  } finally { await response.dispose(); }
}

export async function downloadFile(page, exportId, format, outputDirectory) {
  const row = page.locator(`tr[data-row-key="${exportId}"]`);
  const [download] = await Promise.all([
    page.waitForEvent("download"), row.getByRole("button", { name: format.toUpperCase(), exact: true }).click(),
  ]);
  assert.equal(await download.failure(), null, `${format} download must finish`);
  assert.equal(download.suggestedFilename(), `grades-${exportId}.${format}`);
  const stream = await download.createReadStream();
  assert(stream, "Downloaded file must be readable");
  const chunks = [];
  let bytes = 0;
  for await (const chunk of stream) {
    bytes += chunk.length;
    assert(bytes < 5_000_000, "Unexpectedly large synthetic export");
    chunks.push(chunk);
  }
  const buffer = Buffer.concat(chunks);
  assert(buffer.length > 0, "Downloaded file must not be empty");
  await writeFile(join(outputDirectory, download.suggestedFilename()), buffer);
  return buffer;
}

export function csvRecords(buffer) {
  assert.equal(buffer.subarray(0, 3).toString("hex"), "efbbbf", "CSV must retain its UTF-8 BOM");
  const python = process.env.TA_E2E_PYTHON || join(root, ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  // Use the standard CSV parser; commas, quotes and multiline cells are not string splits.
  const script = [
    "import csv, io, json, sys",
    "reader = csv.DictReader(io.StringIO(sys.stdin.read().lstrip('\\ufeff'), newline=''))",
    "rows = list(reader)",
    "print(json.dumps({'columns': reader.fieldnames, 'rows': rows}, ensure_ascii=False))",
  ].join("\n");
  return JSON.parse(execFileSync(python, ["-I", "-X", "utf8", "-c", script], {
    input: buffer, encoding: "utf8", timeout: 10_000, maxBuffer: 5_000_000,
  }));
}

export async function selectCitation(page, expectedText, start, end) {
  const answer = page.locator(".original-answer");
  assert.equal(await answer.textContent(), expectedText, "UI must preserve the exact answer version");
  const quote = Array.from(expectedText).slice(start, end).join("");
  const selected = await answer.evaluate((element, offsets) => {
    if (element.childNodes.length !== 1 || element.firstChild?.nodeType !== Node.TEXT_NODE) {
      throw new Error("Original answer must be one unmodified text node");
    }
    const points = Array.from(element.textContent);
    const range = document.createRange();
    range.setStart(element.firstChild, points.slice(0, offsets.start).join("").length);
    range.setEnd(element.firstChild, points.slice(0, offsets.end).join("").length);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    element.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    return selection.toString();
  }, { start, end });
  assert.equal(selected, quote);
  await eventually("selected quotation", async () => {
    assert.equal(await page.locator(".selected-quote").textContent(), quote);
  });
}

export async function artifactDirectory(runId) {
  const directory = join(root, ".local", "e2e", runId);
  await mkdir(directory, { recursive: true });
  return directory;
}

export async function sourceManifest() {
  const files = [];
  async function walk(directory) {
    for (const entry of await readdir(join(root, directory), { withFileTypes: true })) {
      if (entry.name === "node_modules" || entry.name === "__pycache__") continue;
      const path = join(directory, entry.name);
      if (entry.isDirectory()) await walk(path);
      else if (entry.isFile()) files.push(path);
    }
  }
  for (const directory of ["frontend/src", "frontend/e2e", "backend/src", "backend/migrations"]) await walk(directory);
  files.push("uv.lock", "pyproject.toml", "frontend/package-lock.json", "frontend/vite.config.ts", ".github/workflows/checks.yml");
  const hashes = {};
  for (const path of files.sort()) {
    hashes[relative(root, join(root, path)).replaceAll("\\", "/")] = createHash("sha256")
      .update(await readFile(join(root, path))).digest("hex");
  }
  return { scope: "Source files listed below; excludes credentials, database and installed dependencies", files: hashes };
}
