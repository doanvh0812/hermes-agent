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

// ---------------------------------------------------------------------------
// QR login over HTTP
//
// Scanning this QR grants full control of the bot's Zalo account, so the page
// is gated on a single-use token printed to the console at start, expires on
// its own, and is torn down as soon as login completes.
// ---------------------------------------------------------------------------

const QR_TTL_MS = 10 * 60 * 1000;

const qrSession = {
    active: false,
    token: "",
    expiresAt: 0,
    state: "idle", // idle | waiting | scanned | done | expired | declined | error
    image: "",     // data: URI of the current QR
    userName: "",
    userAvatar: "",
    error: "",
    abort: null,
};

function qrSessionValid(token) {
    return (
        qrSession.active &&
        qrSession.token &&
        token === qrSession.token &&
        Date.now() < qrSession.expiresAt
    );
}

function qrReset(state) {
    qrSession.active = false;
    qrSession.state = state || "idle";
    qrSession.token = "";
    qrSession.image = "";
    qrSession.abort = null;
}

// Shared chrome for every page this bridge serves. Inlined rather than served
// as an asset: there is no static pipeline here, and these pages must render
// on a phone with nothing installed and no network beyond this host.
const QR_PAGE_CSS = `
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         margin: 0; min-height: 100vh; display: grid; place-items: center;
         background: #f5f5f7; color: #1d1d1f; padding: 20px; }
  @media (prefers-color-scheme: dark) {
    body { background: #1c1c1e; color: #f5f5f7; }
    .card { background: #2c2c2e !important; }
    code { background: #1c1c1e !important; }
  }
  .card { background: #fff; border-radius: 16px; padding: 32px;
          box-shadow: 0 2px 18px rgba(0,0,0,.12); text-align: center;
          max-width: 380px; width: 100%; }
  h1 { font-size: 19px; margin: 0 0 6px; font-weight: 600; }
  p.sub { font-size: 13.5px; opacity: .65; margin: 0 0 22px; line-height: 1.5; }
  .icon { font-size: 44px; line-height: 1; margin-bottom: 14px; }
  code { display: block; background: #f0f0f2; border-radius: 8px;
         padding: 11px 13px; font-size: 12.5px; text-align: left;
         font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         overflow-x: auto; white-space: pre; margin: 4px 0 0; }
  .hint { font-size: 12.5px; opacity: .55; margin-top: 20px; line-height: 1.55; }
  .label { font-size: 12px; opacity: .5; text-transform: uppercase;
           letter-spacing: .05em; margin: 18px 0 6px; text-align: left; }
`;

// Terminal-state notice: expired link, already-used link, or a bad token.
// Each says what happened and what to do about it — a bare 403 leaves the
// operator guessing whether they mistyped or simply waited too long.
function qrNoticeHtml(reason) {
    const views = {
        expired: {
            icon: "⏳",
            title: "Mã QR đã hết hạn",
            sub: "Link đăng nhập chỉ có hiệu lực trong 10 phút. "
               + "Mã cũ đã ngừng hoạt động — hãy tạo mã mới.",
        },
        used: {
            icon: "✓",
            title: "Đã đăng nhập xong",
            sub: "Link này đã được dùng để đăng nhập thành công và không còn "
               + "hiệu lực. Bot đang chạy bình thường, anh/chị có thể đóng trang này.",
        },
        invalid: {
            icon: "🔒",
            title: "Link không hợp lệ",
            sub: "Mã bảo mật trong link không đúng. Có thể link bị sao chép "
               + "thiếu — hãy kiểm tra lại, hoặc tạo link mới.",
        },
    };
    const v = views[reason] || views.invalid;
    const showCmd = reason !== "used";

    return `<!doctype html>
<html lang="vi"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${v.title}</title>
<style>${QR_PAGE_CSS}</style></head>
<body><div class="card">
  <div class="icon">${v.icon}</div>
  <h1>${v.title}</h1>
  <p class="sub">${v.sub}</p>
  ${showCmd ? `<div class="label">Tạo link mới trên máy chủ</div>
  <code>node index.js --qr-web</code>
  <p class="hint">Chạy lệnh trên trong thư mục <code style="display:inline;padding:2px 5px">bridge/</code>
     của plugin Zalo. Terminal sẽ in ra một link mới.</p>` : ""}
</div></body></html>`;
}

// Served as one self-contained page: the bridge has no static asset pipeline
// and the page must work on a phone with nothing else installed.
function qrPageHtml(token) {
    return `<!doctype html>
<html lang="vi"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Đăng nhập Zalo Bot</title>
<style>${QR_PAGE_CSS}
  #qr { width: 260px; height: 260px; object-fit: contain; border-radius: 10px;
        background: #fff; display: block; margin: 0 auto; }
  #status { margin-top: 18px; font-size: 14px; min-height: 20px; line-height: 1.5; }
  .ok { color: #1a8f3c; } .warn { color: #b35c00; } .err { color: #c0362c; }
  .spin { display: inline-block; width: 14px; height: 14px; margin-right: 6px;
          border: 2px solid currentColor; border-right-color: transparent;
          border-radius: 50%; animation: r .8s linear infinite;
          vertical-align: -2px; }
  @keyframes r { to { transform: rotate(360deg) } }
  button { margin-top: 16px; padding: 10px 22px; font-size: 14px; font-weight: 500;
           border: 0; border-radius: 9px; background: #0068ff; color: #fff;
           cursor: pointer; }
  button:active { opacity: .8; }
</style></head>
<body><div class="card">
  <h1>Đăng nhập Zalo cho bot</h1>
  <p class="sub">Mở Zalo trên điện thoại → Thêm → Mã QR → quét mã bên dưới</p>
  <img id="qr" alt="Mã QR đăng nhập">
  <div id="status"><span class="spin"></span>Đang tạo mã…</div>
  <div id="again" style="display:none">
    <div class="label">Tạo link mới trên máy chủ</div>
    <code>node index.js --qr-web</code>
  </div>
</div>
<script>
const TOKEN = ${JSON.stringify(token)};
const qr = document.getElementById('qr');
const st = document.getElementById('status');
const again = document.getElementById('again');
function show(html, cls) { st.innerHTML = html; st.className = cls || ''; }
async function poll() {
  let r;
  try {
    r = await (await fetch('/qr/status?t=' + encodeURIComponent(TOKEN))).json();
  } catch { show('Mất kết nối tới máy chủ.', 'err'); again.style.display = 'block'; return; }
  if (r.image && qr.src !== r.image) qr.src = r.image;
  switch (r.state) {
    case 'waiting':
      show('<span class="spin"></span>Đang chờ quét…'); break;
    case 'scanned':
      show('Đã quét bởi <b>' + (r.userName || '') + '</b> — xác nhận trên điện thoại', 'ok'); break;
    case 'done':
      qr.style.opacity = .25;
      show('✓ Đăng nhập thành công. Bot đã sẵn sàng — có thể đóng trang này.', 'ok');
      return;
    case 'expired':
      qr.style.opacity = .25;
      show('Mã đã hết hạn.', 'warn'); again.style.display = 'block'; return;
    case 'declined':
      show('Bạn đã từ chối trên điện thoại.', 'warn'); again.style.display = 'block'; return;
    case 'error':
      show('Lỗi: ' + (r.error || 'không rõ'), 'err'); again.style.display = 'block'; return;
    case 'idle':
      show('Phiên không còn hiệu lực.', 'warn'); again.style.display = 'block'; return;
  }
  setTimeout(poll, 1200);
}
poll();
</script></body></html>`;
}

async function startQrLogin() {
    const { Zalo: ZaloCls } = require("zca-js");
    const zalo = new ZaloCls({ selfListen: false, checkUpdate: false, logging: false });
    qrSession.state = "waiting";

    try {
        const api = await zalo.loginQR(
            { userAgent: process.env.ZALO_USER_AGENT || "" },
            (event) => {
                const t = event && event.type;
                // Numeric enum: 0 generated, 1 expired, 2 scanned, 3 declined, 4 got-info
                if (t === 0 && event.data && event.data.image) {
                    const img = String(event.data.image);
                    qrSession.image = img.startsWith("data:")
                        ? img
                        : "data:image/png;base64," + img;
                    qrSession.state = "waiting";
                    if (event.actions && event.actions.abort) {
                        qrSession.abort = event.actions.abort;
                    }
                } else if (t === 1) {
                    qrSession.state = "expired";
                } else if (t === 2) {
                    qrSession.state = "scanned";
                    qrSession.userName = (event.data && event.data.display_name) || "";
                    qrSession.userAvatar = (event.data && event.data.avatar) || "";
                } else if (t === 3) {
                    qrSession.state = "declined";
                }
            }
        );

        // Persist exactly like --qr-login does: cookie login later REQUIRES
        // imei + userAgent, so all three are captured together.
        let imei = "", userAgent = process.env.ZALO_USER_AGENT || "", cookie = null;
        try {
            const ctx = await api.getContext();
            imei = ctx.imei || "";
            userAgent = ctx.userAgent || userAgent;
            cookie = ctx.cookie || null;
        } catch {}
        if (!cookie) cookie = await api.getCookie();

        fs.mkdirSync(path.dirname(SESSION_FILE), { recursive: true });
        const tmp = SESSION_FILE + ".tmp";
        fs.writeFileSync(tmp, JSON.stringify(
            { cookie, imei, userAgent, saved_at: new Date().toISOString() }, null, 2));
        fs.renameSync(tmp, SESSION_FILE);
        try { fs.chmodSync(SESSION_FILE, 0o600); } catch {}

        // Take over as the live account without a restart.
        apiRef = api;
        try { ownId = String((await api.getOwnId()) || ""); } catch {}
        attachListener(api);
        api.listener.start();

        qrSession.state = "done";
        qrSession.active = false;   // burn the token; the page keeps polling
        qrSession.token = "";
        console.log("[zalo-bridge] QR login complete via web; own_id=" + ownId);
    } catch (err) {
        qrSession.state = "error";
        qrSession.error = String((err && err.message) || err).slice(0, 200);
        qrSession.active = false;
        qrSession.token = "";
        console.error("[zalo-bridge] web QR login failed:", qrSession.error);
    }
}

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
    const url = new URL(req.url, "http://127.0.0.1");

    // The QR pages are reached from a phone on the LAN, so they cannot sit
    // behind the loopback Host check. They carry their own single-use,
    // self-expiring token instead — and they are the ONLY routes exempt.
    const isQrRoute = url.pathname === "/qr" || url.pathname === "/qr/status";

    // DNS-rebinding guard: loopback bind alone is not enough when a victim
    // browser resolves an attacker hostname to 127.0.0.1.
    if (!isQrRoute && !hostAllowed(req)) {
        res.writeHead(403).end();
        return;
    }

    if (url.pathname === "/qr") {
        const token = url.searchParams.get("t") || "";
        if (!qrSessionValid(token)) {
            // Distinguish the three ways in, because the fix differs: an
            // expired or already-used link needs a new one; a wrong token
            // means the URL was mistyped or tampered with.
            let reason = "invalid";
            if (qrSession.state === "done") {
                reason = "used";
            } else if (
                qrSession.token &&
                token === qrSession.token &&
                Date.now() >= qrSession.expiresAt
            ) {
                reason = "expired";
            } else if (!qrSession.active && qrSession.state === "expired") {
                reason = "expired";
            }
            res.writeHead(reason === "used" ? 200 : 403, {
                "Content-Type": "text/html; charset=utf-8",
                "Cache-Control": "no-store",
                "Content-Security-Policy":
                    "default-src 'none'; style-src 'unsafe-inline'",
                "Referrer-Policy": "no-referrer",
            });
            res.end(qrNoticeHtml(reason));
            return;
        }
        res.writeHead(200, {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            // The page embeds only its own inline script and a data: image.
            "Content-Security-Policy":
                "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
                + "script-src 'unsafe-inline'; connect-src 'self'",
            "Referrer-Policy": "no-referrer",
        });
        res.end(qrPageHtml(token));
        return;
    }

    if (url.pathname === "/qr/status") {
        const token = url.searchParams.get("t") || "";
        // A finished session burns its token, so the page must still be able
        // to read the terminal state it was waiting for.
        const terminal = ["done", "expired", "declined", "error"];
        if (!qrSessionValid(token) && !terminal.includes(qrSession.state)) {
            sendJson(res, 403, { state: "idle" });
            return;
        }
        if (qrSession.active && Date.now() >= qrSession.expiresAt) {
            qrReset("expired");
        }
        sendJson(res, 200, {
            state: qrSession.state,
            image: qrSession.image,
            userName: qrSession.userName,
            error: qrSession.error,
        });
        return;
    }

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

function lanAddress() {
    const nets = os.networkInterfaces();
    for (const name of Object.keys(nets)) {
        for (const ni of nets[name] || []) {
            if (ni.family === "IPv4" && !ni.internal) return ni.address;
        }
    }
    return "127.0.0.1";
}

function announceQrLink() {
    qrSession.token = crypto.randomBytes(24).toString("hex");
    qrSession.expiresAt = Date.now() + QR_TTL_MS;
    qrSession.active = true;
    qrSession.state = "waiting";
    qrSession.image = "";
    qrSession.error = "";

    const host = process.env.ZALO_QR_HOST || lanAddress();
    const link = `http://${host}:${PORT}/qr?t=${qrSession.token}`;
    const line = "─".repeat(Math.min(link.length + 4, 78));
    console.log(
        `\n${line}\n  Mở link này để quét QR (hết hạn sau ${QR_TTL_MS / 60000} phút):\n\n`
        + `  ${link}\n\n${line}\n`
    );
    startQrLogin();
}

(async () => {
    if (process.argv.includes("--qr-web")) {
        // Serve the bridge, then immediately open a QR session and print the
        // link. Used on headless hosts where the PNG cannot be opened.
        if (!TOKEN) {
            // No session file yet on a fresh host, so the derived token is
            // empty; require an explicit one rather than serving unauthenticated.
            console.error(
                "BRIDGE_TOKEN must be set for --qr-web on a host with no session yet."
            );
            process.exit(2);
        }
        const server = http.createServer(handleRequest);
        server.on("error", (err) => {
            console.error("[zalo-bridge] server error:", err && err.message);
            process.exit(5);
        });
        const bindHost = process.env.ZALO_QR_BIND || "0.0.0.0";
        server.listen(PORT, bindHost, () => {
            console.log(`[zalo-bridge] QR server on ${bindHost}:${PORT}`);
            announceQrLink();
        });
        return;
    }

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
