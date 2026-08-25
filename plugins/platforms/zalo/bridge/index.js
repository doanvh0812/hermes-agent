/*
 * hermes-zalo-bridge — local loopback bridge between the Hermes gateway
 * (Python adapter in plugins/platforms/zalo/adapter.py) and a Zalo
 * PERSONAL account via the unofficial zca-js library.
 *
 * WARNING: zca-js talks to unofficial Zalo endpoints using a personal
 * account session. Zalo may suspend or lock that account. Use a
 * secondary account.
 *
 * Security model (mirrors scripts/whatsapp-bridge):
 *   - binds loopback ONLY (127.0.0.1)
 *   - rejects requests whose Host header is not localhost/127.0.0.1
 *     (DNS-rebinding defense)
 *   - every request except /health must carry X-Bridge-Token matching
 *     BRIDGE_TOKEN (a per-spawn random secret passed by the adapter)
 *
 * Modes:
 *   node index.js                 serve HTTP bridge (cookie login)
 *   node index.js --qr-login      interactive QR login, saves session, exits
 *
 * Env:
 *   PORT=8647                     listen port (loopback)
 *   BRIDGE_TOKEN=<secret>         required for serving mode
 *   ZALO_SESSION_FILE=<path>      session JSON {cookie, imei, userAgent}
 *   ZALO_IMEI=<imei>              fallback imei if not in session file
 *   ZALO_USER_AGENT=<ua>          fallback UA if not in session file
 *   ZALO_SELF_LISTEN=1            also emit self-originated messages
 */

"use strict";

const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");

const { Zalo, ThreadType } = require("zca-js");

const PORT = parseInt(process.env.PORT || "8647", 10);
// Deterministic token: derived from the session file when not supplied, so
// ANY process holding the same session (the adapter after a gateway
// restart, a cron standalone sender) can talk to the SAME long-lived
// bridge without re-auth churn — Zalo kicks accounts that re-login often.
const SESSION_FILE =
    process.env.ZALO_SESSION_FILE ||
    path.join(
        process.env.HERMES_HOME || path.join(os.homedir(), ".hermes"),
        "zalo",
        "session.json"
    );

function deriveToken() {
    try {
        return crypto
            .createHash("sha256")
            .update(fs.readFileSync(SESSION_FILE))
            .digest("hex");
    } catch {
        return "";
    }
}

const TOKEN = process.env.BRIDGE_TOKEN || deriveToken();
const SELF_LISTEN = ["1", "true", "yes"].includes(
    String(process.env.ZALO_SELF_LISTEN || "").toLowerCase()
);

// ---------------------------------------------------------------------------
// Event ring buffer — Python polls GET /events?since=<id>
// ---------------------------------------------------------------------------

const EVENT_CAP = 500;
let events = [];
let cursor = 0;

// Directory paging (GET /friends)
const FRIEND_PAGE_SIZE = 100;
const FRIEND_MAX_PAGES = 50; // ceiling: 5000 contacts

function pushEvent(evt) {
    cursor += 1;
    events.push({ id: cursor, ...evt });
    if (events.length > EVENT_CAP) {
        events.splice(0, events.length - EVENT_CAP);
    }
}

// ---------------------------------------------------------------------------
// Login
// ---------------------------------------------------------------------------

function loadSession() {
    if (!SESSION_FILE) {
        return null;
    }
    try {
        return JSON.parse(fs.readFileSync(SESSION_FILE, "utf-8"));
    } catch {
        return null;
    }
}

function saveSession(api) {
    if (!SESSION_FILE) {
        return;
    }
    (async () => {
        try {
            const existing = loadSession() || {};
            let cookie = existing.cookie || null;
            let imei = existing.imei || process.env.ZALO_IMEI || "";
            let userAgent = existing.userAgent || process.env.ZALO_USER_AGENT || "";
            // Self-heal: pull imei/userAgent from the live context so a
            // session saved by an older bridge (empty imei) gets repaired
            // on the next refresh instead of bricking cookie login.
            try {
                const ctx = await api.getContext();
                if (ctx) {
                    cookie = ctx.cookie || cookie;
                    imei = ctx.imei || imei;
                    userAgent = ctx.userAgent || userAgent;
                }
            } catch {
                cookie = (await api.getCookie()) || cookie;
            }
            if (!cookie) {
                return;
            }
            const payload = {
                cookie,
                imei,
                userAgent,
                saved_at: new Date().toISOString(),
            };
            const tmp = SESSION_FILE + ".tmp";
            fs.mkdirSync(path.dirname(SESSION_FILE), { recursive: true });
            fs.writeFileSync(tmp, JSON.stringify(payload, null, 2));
            fs.renameSync(tmp, SESSION_FILE);
        } catch {
            // best-effort refresh; the current session stays untouched
        }
    })();
}

async function loginCookie(zalo) {
    const session = loadSession();
    const cookie = session && session.cookie ? session.cookie : null;
    const imei =
        (session && session.imei) ||
        process.env.ZALO_IMEI ||
        "";
    const userAgent =
        (session && session.userAgent) ||
        process.env.ZALO_USER_AGENT ||
        "";

    if (!cookie) {
        throw new Error(
            "No Zalo session found. Run `node index.js --qr-login` once, or set ZALO_COOKIE_JSON/ZALO_IMEI/ZALO_USER_AGENT."
        );
    }
    return zalo.login({ cookie, imei, userAgent });
}

async function loginQR(zalo) {
    // Saves ./qr.png next to cwd and waits until scanned.
    const qrPath = path.join(process.cwd(), "zalo-login-qr.png");
    console.log("QR saved to: " + qrPath + " — scan it with the Zalo app…");
    const api = await zalo.loginQR({
        userAgent: process.env.ZALO_USER_AGENT || "",
        qrPath,
    });
    // zca-js has no getIMEI(); the registered device identity lives in the
    // session context. Cookie login later REQUIRES imei + userAgent
    // ("Missing required params" otherwise), so capture all three together.
    let imei = "";
    let userAgent = process.env.ZALO_USER_AGENT || "";
    let cookie = null;
    try {
        const ctx = await api.getContext();
        imei = ctx.imei || "";
        userAgent = ctx.userAgent || userAgent;
        cookie = ctx.cookie || null;
    } catch {}
    if (!cookie) {
        cookie = await api.getCookie();
    }
    if (SESSION_FILE) {
        fs.mkdirSync(path.dirname(SESSION_FILE), { recursive: true });
        fs.writeFileSync(
            SESSION_FILE,
            JSON.stringify(
                {
                    cookie,
                    imei,
                    userAgent,
                    saved_at: new Date().toISOString(),
                },
                null,
                2
            )
        );
        console.log("Session saved to " + SESSION_FILE);
        if (!imei) {
            console.warn(
                "WARNING: imei was not present in the session context — " +
                    "cookie login may fail. Re-run --qr-login if the bridge " +
                    "reports 'Missing required params'."
            );
        }
    }
    return api;
}

// ---------------------------------------------------------------------------
// Wire up listener + api once logged in
// ---------------------------------------------------------------------------

let apiRef = null;
let ownId = "";

function threadTypeName(t) {
    return t === ThreadType.Group ? "group" : "user";
}

function attachListener(api) {
    api.listener.on("message", (message) => {
        try {
            if (message.isSelf && !SELF_LISTEN) {
                return;
            }
            const data = message.data || {};
            pushEvent({
                kind: "message",
                thread_id: message.threadId,
                thread_type: threadTypeName(message.type),
                is_self: !!message.isSelf,
                msg: {
                    content:
                        typeof data.content === "string" ? data.content : "",
                    content_obj:
                        typeof data.content === "object" && data.content !== null
                            ? data.content
                            : null,
                    msg_id: data.msgId != null ? String(data.msgId) : "",
                    cli_msg_id:
                        data.cliMsgId != null ? String(data.cliMsgId) : "",
                    ts: data.ts != null ? Number(data.ts) : 0,
                    uid_from: data.uidFrom || "",
                    title: data.title || "",
                    raw: data,
                },
            });
        } catch {
            // never let a bad event kill the listener callback
        }
    });
    api.listener.on("error", (err) => {
        console.error("[zalo-bridge] listener error:", err && err.message);
    });
}

async function startServing() {
    if (!TOKEN) {
        console.error("BRIDGE_TOKEN env var is required in serving mode.");
        process.exit(2);
    }

    const zalo = new Zalo({
        selfListen: true, // lib-side flag; bridge filters isSelf itself
        checkUpdate: false,
        logging: false,
    });

    let api;
    try {
        api = await loginCookie(zalo);
    } catch (err) {
        console.error("[zalo-bridge] login failed:", err.message);
        process.exit(3);
    }
    apiRef = api;

    try {
        ownId = String(await api.getOwnId() || "");
    } catch {}

    attachListener(api);
    api.listener.start();

    saveSession(api);
    setInterval(() => {
        Promise.resolve(api.keepAlive()).catch(() => {});
        saveSession(api);
    }, 30 * 60 * 1000).unref();

    const server = http.createServer(handleRequest);
    server.on("error", (err) => {
        if (err && err.code === "EADDRINUSE") {
            // Another bridge instance already owns this port — that's the
            // long-lived daemon design working. Exit quietly; the adapter
            // will attach to the running instance via the derived token.
            console.log(
                "[zalo-bridge] port " +
                    PORT +
                    " already served by another bridge instance; exiting."
            );
            process.exit(0);
        }
        console.error("[zalo-bridge] server error:", err && err.message);
        process.exit(5);
    });
    server.listen(PORT, "127.0.0.1", () => {
        console.log(
            "[zalo-bridge] listening on 127.0.0.1:" +
                PORT +
                " own_id=" +
                (ownId ? ownId : "?")
        );
    });
}

// ---------------------------------------------------------------------------
// HTTP layer
// ---------------------------------------------------------------------------

const ALLOWED_HOSTS = new Set(["localhost", "127.0.0.1"]);

function hostAllowed(req) {
    const raw = String(req.headers.host || "");
    const host = raw.split(":")[0].toLowerCase();
    return ALLOWED_HOSTS.has(host);
}

function sendJson(res, code, obj) {
    const body = JSON.stringify(obj);
    res.writeHead(code, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
    });
    res.end(body);
}

function readBody(req, limit) {
    return new Promise((resolve, reject) => {
        let size = 0;
        const chunks = [];
        req.on("data", (c) => {
            size += c.length;
            if (size > limit) {
                reject(new Error("payload too large"));
                req.destroy();
                return;
            }
            chunks.push(c);
        });
        req.on("end", () => resolve(Buffer.concat(chunks).toString("utf-8")));
        req.on("error", reject);
    });
}

function authorized(req) {
    return (
        !req.headers["x-bridge-token"] ||
        String(req.headers["x-bridge-token"]) === TOKEN
    );
}

function resolveThreadType(name) {
    return name === "group" ? ThreadType.Group : ThreadType.User;
}

async function handleRequest(req, res) {
    // DNS-rebinding guard: loopback bind alone is not enough when a victim
    // browser resolves an attacker hostname to 127.0.0.1.
    if (!hostAllowed(req)) {
        res.writeHead(403).end();
        return;
    }

    const url = new URL(req.url, "http://127.0.0.1");

    if (url.pathname === "/health") {
        sendJson(res, 200, {
            ok: true,
            ready: !!apiRef,
            own_id: ownId,
            self_listen: SELF_LISTEN,
        });
        return;
    }

    if (!authorized(req)) {
        sendJson(res, 401, { error: "bad bridge token" });
        return;
    }

    if (!apiRef) {
        sendJson(res, 503, { error: "bridge not ready" });
        return;
    }

    try {
        if (req.method === "GET" && url.pathname === "/events") {
            const since = parseInt(url.searchParams.get("since") || "0", 10);
            const out = events.filter((e) => e.id > since);
            sendJson(res, 200, { cursor, events: out });
            return;
        }

        if (req.method === "GET" && url.pathname === "/own-id") {
            sendJson(res, 200, { own_id: ownId });
            return;
        }

        // ---- Directory endpoints (allowlist tooling) --------------------
        // The adapter's access gate is friendship-based: it needs display
        // names/phones to tell the operator WHO is asking, and group names
        // so approvals are legible.

        if (req.method === "GET" && url.pathname === "/friends") {
            const out = [];
            let page = 1;
            for (;;) {
                const batch = await apiRef.getAllFriends(FRIEND_PAGE_SIZE, page);
                if (!Array.isArray(batch) || batch.length === 0) {
                    break;
                }
                for (const u of batch) {
                    const id = String((u && u.userId) || "");
                    if (!id) {
                        continue;
                    }
                    out.push({
                        id,
                        name: String(u.displayName || u.zaloName || ""),
                        phone: String(u.phoneNumber || ""),
                    });
                }
                if (batch.length < FRIEND_PAGE_SIZE) {
                    break;
                }
                page += 1;
                if (page > FRIEND_MAX_PAGES) {
                    break; // safety ceiling
                }
            }
            sendJson(res, 200, { friends: out });
            return;
        }

        if (req.method === "GET" && url.pathname === "/user-info") {
            const uid = String(url.searchParams.get("id") || "");
            if (!uid) {
                sendJson(res, 400, { error: "id required" });
                return;
            }
            const info = await apiRef.getUserInfo(uid);
            const profiles = (info && info.changed_profiles) || {};
            const prof = profiles[uid] || {};
            sendJson(res, 200, {
                id: uid,
                name: String(prof.displayName || prof.zaloName || ""),
                phone: String(prof.phoneNumber || ""),
                is_friend: Number(prof.isFr || 0) === 1,
            });
            return;
        }

        if (req.method === "GET" && url.pathname === "/groups") {
            const all = await apiRef.getAllGroups();
            const ids = Object.keys((all && all.gridVerMap) || {});
            const out = [];
            if (ids.length > 0) {
                const info = await apiRef.getGroupInfo(ids);
                const map = (info && info.gridInfoMap) || {};
                for (const gid of ids) {
                    const g = map[gid] || {};
                    out.push({
                        id: gid,
                        name: String(g.name || ""),
                        members: Array.isArray(g.memVerList)
                            ? g.memVerList.length
                            : 0,
                    });
                }
            }
            sendJson(res, 200, { groups: out });
            return;
        }

        if (req.method === "POST" && url.pathname === "/send") {
            const body = JSON.parse(await readBody(req, 1024 * 1024));
            const tid = String(body.thread_id || "");
            if (!tid) {
                sendJson(res, 400, { error: "thread_id required" });
                return;
            }
            const ttype = resolveThreadType(body.thread_type);
            const result = await apiRef.sendMessage(String(body.msg || ""), tid, ttype);
            sendJson(res, 200, {
                ok: true,
                message: result && result.message ? result.message : null,
            });
            return;
        }

        if (req.method === "POST" && url.pathname === "/send-media") {
            const body = JSON.parse(await readBody(req, 4 * 1024 * 1024));
            const tid = String(body.thread_id || "");
            const paths = Array.isArray(body.paths)
                ? body.paths.map(String).filter((p) => path.isAbsolute(p))
                : [];
            if (!tid || paths.length === 0) {
                sendJson(res, 400, { error: "thread_id and paths required" });
                return;
            }
            const ttype = resolveThreadType(body.thread_type);
            const result = await apiRef.sendMessage(
                { msg: String(body.caption || ""), attachments: paths },
                tid,
                ttype
            );
            sendJson(res, 200, {
                ok: true,
                message: result && result.message ? result.message : null,
                attachment: Array.isArray(result && result.attachment)
                    ? result.attachment
                    : [],
            });
            return;
        }

        if (req.method === "POST" && url.pathname === "/typing") {
            const body = JSON.parse(await readBody(req, 64 * 1024));
            const tid = String(body.thread_id || "");
            if (tid) {
                await apiRef.sendTypingEvent(tid, resolveThreadType(body.thread_type));
            }
            sendJson(res, 200, { ok: true });
            return;
        }

        sendJson(res, 404, { error: "not found" });
    } catch (err) {
        sendJson(res, 500, { error: String((err && err.message) || err) });
    }
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

(async () => {
    if (process.argv.includes("--qr-login")) {
        const zalo = new Zalo({ selfListen: false, checkUpdate: false, logging: false });
        try {
            await loginQR(zalo);
            console.log("QR login complete.");
            process.exit(0);
        } catch (err) {
            console.error("QR login failed:", err.message);
            process.exit(4);
        }
    }
    await startServing();
})();
