/** Publish new, verified transcripts through the OpenClaw 2026.7.1 session SDK. */
import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { chmod, copyFile, link, lstat, mkdir, open, readFile, readdir, realpath, unlink } from "node:fs/promises";
import path from "node:path";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";

const VERSION = "2026.7.1";
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const packageRoot = await realpath(process.argv[2]);
const metadata = JSON.parse(await readFile(path.join(packageRoot, "package.json"), "utf8"));
if (metadata.name !== "openclaw" || metadata.version !== VERSION) throw new Error(`Requires OpenClaw ${VERSION}; no storage writes performed`);
const sdk = async (name) => import(pathToFileURL(path.join(packageRoot, "dist", "plugin-sdk", `${name}.js`)).href);
const { loadConfig } = await sdk("config-runtime");
const { resolveStateDir } = await sdk("state-paths");
const { resolveAgentWorkspaceDir } = await sdk("agent-runtime");
const { SessionManager } = await sdk("agent-sessions");
const { getSessionEntry, listSessionEntries, patchSessionEntry, resolveSessionFilePath, resolveStorePath } = await sdk("session-store-runtime");
const { readSessionTranscriptEvents } = await sdk("session-transcript-runtime");
const config = loadConfig();
const maintenance = { mode: "warn", pruneAfterMs: 3153600000000, modelRunPruneAfterMs: 3153600000000, maxEntries: Number.MAX_SAFE_INTEGER, resetArchiveRetentionMs: null, maxDiskBytes: null, highWaterBytes: null };

/** @param {unknown} value @returns {value is Record<string, unknown>} */
function record(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** @param {unknown} value @returns {string} */
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (record(value)) return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}

/** @param {unknown} value @returns {string} */
function jsonLine(value) {
  return JSON.stringify(value).replace(/[\u0085\u2028\u2029]/g, (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`) + "\n";
}

/** @param {string} agentId @returns {Record<string, string>} */
function scope(agentId) {
  if (config.gateway?.mode === "remote") throw new Error("This v2 importer requires local Gateway state on the same host; remote mode is unsupported");
  const ids = Array.isArray(config.agents?.list) && config.agents.list.length ? config.agents.list.map((agent) => agent.id) : ["main"];
  if (!ids.includes(agentId)) throw new Error("Unknown destination agent");
  return { agentId, storePath: resolveStorePath(config.session?.store, { agentId }) };
}

/** @param {string} root @param {string} relative @param {number} limit @returns {Promise<Record<string, unknown>>} */
async function inputFile(root, relative, limit = 4 * 1024 * 1024) {
  if (typeof relative !== "string" || path.isAbsolute(relative)) throw new Error("Expected a relative prepared path");
  const base = await realpath(root);
  const filename = await realpath(path.resolve(base, relative));
  if (!filename.startsWith(base + path.sep)) throw new Error("Prepared path escapes archive");
  const info = await lstat(filename);
  if (!info.isFile() || info.size > limit) throw new Error("Prepared file exceeds its size limit");
  const value = JSON.parse(await readFile(filename, "utf8"));
  if (!record(value)) throw new Error("Expected a prepared object");
  return value;
}

/** @param {Record<string, unknown>} chat @param {string} agentId @returns {string} */
function chatId(chat, agentId) {
  if (typeof chat.session_key !== "string" || !chat.session_key.startsWith(`agent:${agentId}:claude-import:`)) throw new Error("Destination is outside imported sessions");
  const identifier = chat.session_key.split(":").at(-1);
  if (!UUID.test(identifier)) throw new Error("Invalid imported session identity");
  return identifier;
}

/** @param {unknown[]} events @param {Record<string, unknown>} chat @returns {Record<string, unknown>} */
function checkEvents(events, chat) {
  const headers = events.filter((event) => record(event) && event.type === "session");
  if (headers.length !== 1 || headers[0].version !== 3 || headers[0].id !== chat.session_key.split(":").at(-1) || Date.parse(headers[0].timestamp) !== chat.created_ms) throw new Error("Native transcript header differs from the source");
  const messages = events.filter((event) => record(event) && event.type === "message");
  if (events.some((event) => !record(event) || !["session", "message"].includes(event.type))) throw new Error("Imported history was continued, compacted, or changed; it was not overwritten");
  const facts = new Map();
  const parents = new Set();
  for (const event of messages) {
    if (!UUID.test(event.id) || facts.has(event.id) || !record(event.message) || !["user", "assistant"].includes(event.message.role)) throw new Error("Malformed native message");
    if (event.parentId !== null && !facts.has(event.parentId)) throw new Error(`Native parent is missing or out of order: record ${event.id}, parent ${event.parentId}, checked ${facts.size}`);
    if (Date.parse(event.timestamp) !== event.message.timestamp) throw new Error("Native entry and message dates differ");
    facts.set(event.id, canonical({ id: event.id, parentId: event.parentId, role: event.message.role, content: event.message.content, timestamp: event.message.timestamp }));
    if (event.parentId !== null) parents.add(event.parentId);
  }
  const digest = createHash("sha256").update([...facts.keys()].sort().map((key) => facts.get(key) + "\n").join("")).digest("hex");
  const leaves = [...facts.keys()].filter((key) => !parents.has(key)).sort();
  if (messages.length !== chat.messages || digest !== chat.digest || canonical(leaves) !== canonical([...chat.leaves].sort())) throw new Error("Native messages or branches differ from the source");
  if ((messages.at(-1)?.id ?? null) !== chat.active_leaf) throw new Error("Native active branch differs from the source append order");
  return { verified: true, messages: messages.length, digest, leaves, activeLeaf: chat.active_leaf };
}

/** @param {Record<string, string>} target @param {string} filename @param {Record<string, unknown>} chat @returns {Promise<Record<string, unknown>>} */
async function verifyStored(target, filename, chat) {
  const raw = (await readFile(filename, "utf8")).split("\n").filter((line) => line.trim()).map((line) => JSON.parse(line));
  const result = checkEvents(raw, chat);
  const events = await readSessionTranscriptEvents(target);
  if (canonical(events) !== canonical(raw)) throw new Error("SDK did not read every native transcript record");
  return result;
}

/** @param {string} filename @param {string} sessionId @returns {Promise<void>} */
async function cleanAbandonedStaging(filename, sessionId) {
  const directory = path.dirname(filename);
  for (const name of await readdir(directory).catch((error) => error.code === "ENOENT" ? [] : Promise.reject(error))) {
    const prefix = `.${sessionId}.claude-import-v2.`;
    if (!name.startsWith(prefix) || !/^\d+\.pending$/.test(name.slice(prefix.length))) continue;
    const owner = Number(name.slice(prefix.length).split(".")[0]);
    try { process.kill(owner, 0); continue; } catch (error) { if (error.code !== "ESRCH") continue; }
    const candidate = path.join(directory, name);
    const info = await lstat(candidate);
    if (info.isFile() && !info.isSymbolicLink() && info.uid === process.getuid()) await unlink(candidate);
  }
}

/** @param {Record<string, unknown>} params @returns {Promise<Record<string, unknown>>} */
async function execute(params) {
  const agentId = typeof params.agentId === "string" ? params.agentId : "main";
  const targetScope = scope(agentId);
  const workspace = resolveAgentWorkspaceDir(config, agentId);
  if (params.action === "status") return { protocol: 2, version: VERSION, workspace, storePath: targetScope.storePath, stateDir: resolveStateDir(), bootstrapMaxChars: config.agents?.defaults?.bootstrapMaxChars ?? 20000 };
  if (params.action === "list") return { keys: listSessionEntries(targetScope).map((item) => item.sessionKey) };
  const chat = await inputFile(params.archive, params.chatFile);
  const sessionId = chatId(chat, agentId);
  const sessionKey = chat.session_key;
  const target = { ...targetScope, sessionKey, sessionId };
  const filename = resolveSessionFilePath(sessionId, undefined, { agentId, storePath: targetScope.storePath });
  if (params.action === "publish") await cleanAbandonedStaging(filename, sessionId);
  const current = getSessionEntry({ ...targetScope, sessionKey, readConsistency: "latest" });
  if (current && (current.sessionId !== sessionId || path.resolve(current.sessionFile ?? filename) !== filename)) throw new Error("Existing session identity differs; nothing was replaced");
  if (params.action === "verify" || current) {
    if (!current) throw new Error("Imported session is not registered");
    const info = await lstat(filename);
    if (!info.isFile() || info.isSymbolicLink()) throw new Error("Native transcript destination is unsafe");
    const result = await verifyStored(target, filename, chat);
    return { ...result, sessionId, sessionKey, alreadyPresent: true };
  }
  if (params.action !== "publish") throw new Error("Unsupported legacy import action");
  await mkdir(path.dirname(filename), { recursive: true, mode: 0o700 });
  const directory = await realpath(path.dirname(filename));
  if (path.dirname(filename) !== directory) throw new Error("Session directory uses a symlink; explicit deployment review required");
  const pending = path.join(directory, `.${sessionId}.claude-import-v2.${process.pid}.pending`);
  const descriptor = await open(pending, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600);
  try {
    const header = { type: "session", version: 3, id: sessionId, timestamp: new Date(chat.created_ms).toISOString(), cwd: workspace };
    await descriptor.writeFile(jsonLine(header));
    for (const relative of chat.chunks) {
      const chunk = await inputFile(path.join(params.archive, "prepared"), relative);
      if (chunk.sessionKey !== sessionKey || !Array.isArray(chunk.messages) || chunk.messages.length > 100) throw new Error("Invalid prepared batch");
      for (const item of chunk.messages) {
        if (!record(item) || !UUID.test(item.eventId) || !Number.isSafeInteger(item.timestampMs) || item.timestampMs < 0 || !record(item.message) || !Array.isArray(item.message.content) || !item.message.content.every((block) => record(block) && block.type === "text" && typeof block.text === "string")) throw new Error("Invalid prepared message");
        const message = { ...item.message, timestamp: item.timestampMs, idempotencyKey: item.idempotencyKey };
        if (message.role === "assistant") message.usage = {
          input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
          contextUsage: { state: "unavailable" },
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
        };
        await descriptor.writeFile(jsonLine({ type: "message", id: item.eventId, parentId: item.parentId, timestamp: new Date(item.timestampMs).toISOString(), message }));
      }
    }
    await descriptor.sync();
  } catch (error) {
    await unlink(pending);
    throw error;
  } finally {
    await descriptor.close();
  }
  try {
    const manager = SessionManager.open(pending);
    if (manager.wasRecoveredFromCorruptHeader() || manager.getSessionId() !== sessionId) throw new Error("SDK rejected staged transcript header");
    checkEvents([manager.getHeader(), ...manager.getEntries()], chat);
    if (manager.getLeafId() !== chat.active_leaf) throw new Error("SDK resolved a different active branch");
    try {
      await link(pending, filename);
      await chmod(filename, 0o600);
    } catch (error) {
      if (["ENOTSUP", "EOPNOTSUPP", "EXDEV", "ENOSYS"].includes(error.code)) {
        await copyFile(pending, filename, constants.COPYFILE_EXCL);
        await chmod(filename, 0o600);
      } else if (error.code !== "EEXIST") throw error;
      const existingInfo = await lstat(filename);
      if (!existingInfo.isFile() || existingInfo.isSymbolicLink()) throw new Error("Native transcript destination is unsafe");
      const existingManager = SessionManager.open(filename);
      if (existingManager.getSessionId() !== sessionId) throw new Error("Existing transcript header differs");
      checkEvents([existingManager.getHeader(), ...existingManager.getEntries()], chat);
    }
    const importedAt = Date.now();
    const candidate = { sessionId, sessionFile: filename, updatedAt: importedAt, sessionStartedAt: importedAt, lastInteractionAt: importedAt, displayName: chat.title.slice(0, 500), category: chat.category, chatType: "direct" };
    await patchSessionEntry({ ...targetScope, sessionKey, fallbackEntry: candidate, maintenanceConfig: maintenance, preserveActivity: true, update: (_entry, context) => {
      if (context.existingEntry) {
        if (context.existingEntry.sessionId !== sessionId || path.resolve(context.existingEntry.sessionFile ?? filename) !== filename) throw new Error("A different session was registered concurrently");
        return null;
      }
      return candidate;
    } });
    const registered = getSessionEntry({ ...targetScope, sessionKey, readConsistency: "latest" });
    if (!registered || registered.sessionId !== sessionId) throw new Error("SDK did not persist the imported session");
    const result = await verifyStored(target, filename, chat);
    return { ...result, sessionId, sessionKey, alreadyPresent: false };
  } finally {
    await unlink(pending);
  }
}

for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  let id = null;
  let response;
  try {
    const request = JSON.parse(line);
    id = request.id;
    const result = await execute(request.params);
    response = { protocol: 2, id, ok: true, result };
  } catch (error) {
    response = { protocol: 2, id, ok: false, error: error instanceof Error ? error.message : "Legacy import failed" };
  }
  process.stdout.write(JSON.stringify(response) + "\n");
}
