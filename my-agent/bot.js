// WhatsApp -> Claude agent bridge.
//
// Links to your personal WhatsApp (QR scan, like WhatsApp Web). Watches the
// "Message yourself" chat only; messages starting with "@a " are sent to the
// Python agent and the reply is posted back into the same chat.
//
// Run with: node bot.js   (first run prints qr.png to scan)

const { Client, LocalAuth, MessageMedia } = require("whatsapp-web.js");
const qrcode = require("qrcode");
const qrterm = require("qrcode-terminal");
const { execFile } = require("child_process");
const path = require("path");
const fs = require("fs");
const os = require("os");
const OpenAI = require("openai").default;

require("dotenv").config({ path: path.join(__dirname, ".env") });
const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

const PYTHON = path.join(__dirname, ".venv", "bin", "python");
const SCRIPT = path.join(__dirname, "agent_reply.py");
// Felix's primary number — only this contact can use the bot. WhatsApp may
// identify the same contact as either a phone-number JID (@c.us) or an
// anonymized @lid, so we accept both.
const ALLOWED_IDS = ["18582629123@c.us", "225864126062745@lid", "38517333864587@lid"];
const BOT_MARK = "🤖 "; // prefix on every bot reply, used to ignore our own messages
const sentByBot = new Set(); // ids of messages the bot itself sent
let agentBusy = false;
const agentQueue = [];
const STATE_FILE = path.join(__dirname, ".bot_state.json");
let botState = {};
try {
  botState = JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
} catch (_) {}

// On Linux servers (run as root), Puppeteer needs the system Chromium with
// --no-sandbox. On Mac this var is unset and whatsapp-web.js uses its bundled
// Chromium normally.
const CHROMIUM_PATH = process.env.PUPPETEER_EXECUTABLE_PATH;

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: path.join(__dirname, ".wwebjs_auth") }),
  puppeteer: CHROMIUM_PATH
    ? {
        executablePath: CHROMIUM_PATH,
        args: [
          "--no-sandbox",
          "--disable-setuid-sandbox",
          "--disable-dev-shm-usage",
          "--disable-gpu",
          "--disable-software-rasterizer",
        ],
      }
    : {},
});

client.on("qr", async (qr) => {
  qrterm.generate(qr, { small: true });
  await qrcode.toFile(path.join(__dirname, "qr.png"), qr, { width: 500 });
  console.log("QR code written to qr.png — scan from WhatsApp > Settings > Linked Devices");
});

// A live Chat handle captured from the most recent owner message. Proactive
// sends (startup alert, morning briefing, watchdog outbox) prefer this because
// client.sendMessage(bareId, ...) was throwing "Cannot read properties of
// undefined (reading 'id')" inside wwebjs when resolving a bare id string,
// whereas sending on an already-resolved Chat works (that's the path msg.reply
// uses, which is why interactive replies never hit this bug).
let lastOwnerChat = null;

// whatsapp-web.js sends go through Puppeteer and can hang indefinitely (dead
// page, lost devtools connection) instead of ever resolving or rejecting.
// processQueue's agentBusy mutex only resets after its send finishes, so an
// unbounded hang there wedges the message queue forever — the Node process
// itself never crashes, so pm2 still reports "online" and the cron watchdog
// (check-bot.sh) never restarts it, leaving the bot silently unresponsive.
// Race every send against a timeout so it always settles one way or another.
function withTimeout(promise, ms, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

async function sendProactiveMessage(text) {
  const body = BOT_MARK + text.slice(0, 4000);

  // Try delivery paths in order of reliability, first success wins:
  //   1. the cached live Chat from an incoming owner message
  //   2. a Chat resolved via getChatById for each allowed id
  //   3. the raw client.sendMessage(id, ...) (original behaviour) as last resort
  const attempts = [];
  if (lastOwnerChat) attempts.push(() => lastOwnerChat.sendMessage(body));
  // Always deliver to the LINKED account's own "Message yourself" chat, whatever
  // number scanned the QR. Fixes proactive messages vanishing when the linked
  // number differs from the hardcoded ALLOWED_IDS (they'd send to a contact the
  // owner isn't viewing). This is the same chat the owner types "@a" into.
  const selfId = client.info?.wid?._serialized;
  if (selfId) attempts.push(() => client.sendMessage(selfId, body));
  for (const id of ALLOWED_IDS) {
    attempts.push(async () => (await client.getChatById(id)).sendMessage(body));
  }
  for (const id of ALLOWED_IDS) {
    attempts.push(() => client.sendMessage(id, body));
  }

  // withTimeout races an attempt against a timer but can't cancel the
  // underlying Puppeteer call — a "timed out" attempt that isn't actually
  // hung keeps running and can still succeed after we've already moved on
  // to (and succeeded via) a fallback, delivering the same message twice.
  // Track late resolutions so we can skip starting a further attempt once
  // any of them — timed-out or not — has actually gone through.
  let settled = null;
  let lastErr;
  for (const attempt of attempts) {
    if (settled) return settled;
    const attemptPromise = attempt();
    attemptPromise.then((sent) => {
      if (settled) return;
      settled = sent || true;
      if (sent?.id?._serialized) sentByBot.add(sent.id._serialized);
    }, () => {});
    try {
      const sent = await withTimeout(attemptPromise, 30_000, "sendProactiveMessage attempt");
      if (sent?.id?._serialized) sentByBot.add(sent.id._serialized);
      return sent;
    } catch (e) {
      lastErr = e;
    }
  }
  if (settled) return settled;
  throw lastErr || new Error("sendProactiveMessage: all delivery attempts failed");
}

function saveBotState() {
  // Write via temp file + rename so a pm2 restart mid-write can't leave a
  // truncated, unparseable state file (which would silently reset botState
  // to {} on next boot and re-fire the morning briefing that day).
  const tmp = `${STATE_FILE}.tmp${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify(botState, null, 2));
  fs.renameSync(tmp, STATE_FILE);
}

// Routed through the same agentQueue/agentBusy mutex as interactive messages
// (see processQueue) instead of a separate execFile call — both invocations
// run agent_reply.py, which reads and rewrites the shared .whatsapp_session
// and .assistant_memory.json files, so running two at once could clobber
// each other's state if a briefing fires while a user message is in flight.
function sendMorningBriefing() {
  agentQueue.push({
    prompt:
      "[Scheduled morning briefing] Prepare my concise morning briefing for today. " +
      "Read both business and personal calendars, search Close CRM for new leads or follow-ups due, " +
      "and include only useful action items. Use short WhatsApp-friendly bullets.",
    isBriefing: true,
  });
  processQueue();
}

function scheduleMorningBriefing() {
  const check = () => {
    const now = new Date();
    const israel = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Jerusalem", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false,
    }).formatToParts(now).reduce((out, part) => ((out[part.type] = part.value), out), {});
    const date = `${israel.year}-${israel.month}-${israel.day}`;
    if (israel.hour === "07" && Number(israel.minute) < 2 && botState.briefingDate !== date) {
      botState.briefingDate = date;
      saveBotState();
      sendMorningBriefing();
    }
  };
  check();
  setInterval(check, 30_000);
}

client.on("ready", async () => {
  clientReady = true;
  console.log("READY: WhatsApp bridge is live. Message yourself starting with '@a '.");
  console.log("LINKED AS:", client.info?.wid?._serialized || "(wid unknown)");
  if (process.env.BOT_SEND_STARTUP_ALERT !== "false") {
    await sendProactiveMessage(`✅ WhatsApp assistant is online (${new Date().toLocaleString("en-IL", { timeZone: "Asia/Jerusalem" })}).`).catch((e) => console.error("startup alert failed:", e.message));
  }
  scheduleMorningBriefing();
});

// Outbox: external scripts (the cron watchdog) drop .txt files here to have
// them sent to Felix. In-process alerts can't fire when the bot process is
// dead — the watchdog writes here, and the alert goes out once we're back up.
const OUTBOX_DIR = path.join(__dirname, ".outbox");
let clientReady = false;
// A drain can take much longer than the 15s tick (each sendProactiveMessage
// attempt can itself take up to 30s, several attempts deep) — setInterval
// doesn't wait for one tick to finish before scheduling the next, so without
// this guard a still-in-flight drain and a new tick would both pick up the
// same un-unlinked file and send it twice.
let outboxDraining = false;

setInterval(async () => {
  if (!clientReady || outboxDraining) return;
  outboxDraining = true;
  try {
    let files;
    try {
      files = fs.readdirSync(OUTBOX_DIR).filter((f) => f.endsWith(".txt"));
    } catch {
      return; // no outbox dir yet
    }
    for (const f of files.sort()) {
      const full = path.join(OUTBOX_DIR, f);
      try {
        const text = fs.readFileSync(full, "utf8").trim();
        if (text) {
          await sendProactiveMessage(text);
          console.log(`-> outbox sent: ${f}`);
        }
        fs.unlinkSync(full);
      } catch (e) {
        console.error(`outbox send failed (${f}):`, e.message);
      }
    }
  } finally {
    outboxDraining = false;
  }
}, 15_000);

async function transcribeVoiceNote(msg) {
  const media = await msg.downloadMedia();
  if (!media || !media.data) {
    throw new Error("voice note media could not be downloaded (empty response from WhatsApp)");
  }
  console.log(`-> voice media downloaded: ${media.data.length} b64 chars, mimetype=${media.mimetype || "(none)"}`);
  const tmpFile = path.join(os.tmpdir(), `wa_voice_${Date.now()}.ogg`);
  fs.writeFileSync(tmpFile, Buffer.from(media.data, "base64"));
  try {
    console.log("-> calling OpenAI whisper...");
    const transcript = await openai.audio.transcriptions.create({
      file: fs.createReadStream(tmpFile),
      model: "whisper-1",
      language: "he",
      // Bias Whisper to keep embedded English names/terms in English letters
      // instead of forcing them into Hebrew spelling.
      prompt:
        "שיחה עסקית בעברית על נדל\"ן. שמות של אנשים, מקומות וחברות באנגלית נשארים באנגלית, " +
        "למשל: Kenneth, North Carolina, San Diego, Close CRM, Shefa Homes.",
    });
    return transcript.text;
  } finally {
    fs.unlinkSync(tmpFile);
  }
}

client.on("message_create", async (msg) => {
  console.log(
    `[msg] fromMe=${msg.fromMe} from=${msg.from} to=${msg.to} chat=${msg.id?.remote} body=${JSON.stringify(
      (msg.body || "").slice(0, 60)
    )}`
  );

  // This bot account is dedicated to Felix's own number. Only respond to
  // messages sent BY that number TO this bot account — ignore the bot's own
  // sent messages (fromMe) and ignore everyone else.
  if (msg.fromMe) return;
  if (!ALLOWED_IDS.includes(msg.from)) return;
  // Never answer our own replies (loop protection)
  if (sentByBot.has(msg.id?._serialized) || (msg.body || "").startsWith(BOT_MARK)) return;

  // Capture a live Chat handle for proactive sends (briefing, alerts, outbox).
  // This is what makes those delivery paths work — see sendProactiveMessage.
  msg.getChat().then((c) => { lastOwnerChat = c; }).catch(() => {});
  const isVoice = msg.type === "ptt" || msg.type === "audio";
  const isImage = msg.type === "image";
  const isLocation = msg.type === "location";
  if (!isVoice && !isImage && !isLocation && (!msg.body || msg.type !== "chat")) return; // text, voice, image, or location only

  // "@m status|pause|resume" = Miles the Publisher, deterministic commands.
  if (msg.type === "chat" && /^@m(iles)?\s+/i.test(msg.body || "")) {
    const cmd = msg.body.replace(/^@m(iles)?\s+/i, "").trim().toLowerCase();
    const PUB = "/root/publisher";
    let reply;
    try {
      if (cmd === "pause") {
        fs.writeFileSync(path.join(PUB, "PAUSED"), new Date().toISOString());
        reply = "⏸ Publishing paused. Nothing posts until you send '@m resume'.";
      } else if (cmd === "resume") {
        fs.rmSync(path.join(PUB, "PAUSED"), { force: true });
        reply = "▶️ Publishing resumed. Next scheduled post goes out normally.";
      } else {
        const sched = JSON.parse(fs.readFileSync(path.join(PUB, "schedule.json"), "utf8"));
        const paused = fs.existsSync(path.join(PUB, "PAUSED"));
        const lines = sched.map((p) => {
          const st = p.posted ? "✅" : "🕘";
          const plats = (p.platforms || []).join("+");
          return `${st} ${p.date} ${p.pillar || ""} (${plats})`;
        });
        reply = `Miles here${paused ? " — ⏸ PAUSED" : ""}. Schedule:\n` + lines.join("\n");
      }
    } catch (e) {
      reply = `⚠️ Miles hit an error: ${e.message.slice(0, 200)}`;
    }
    try {
      const sent = await msg.reply(BOT_MARK + reply);
      if (sent?.id?._serialized) sentByBot.add(sent.id._serialized);
    } catch (e) { console.error("miles reply failed:", e.message); }
    return;
  }

  // "@r <request>" = Riley the Content Manager drafting from the server.
  if (msg.type === "chat" && /^@r(iley)?\s+/i.test(msg.body || "")) {
    const req = msg.body.replace(/^@r(iley)?\s+/i, "").trim();
    execFile(PYTHON, ["/root/publisher/riley_reply.py", req],
      { timeout: 120_000, maxBuffer: 1024 * 1024, cwd: "/root/publisher" },
      async (err, stdout, stderr) => {
        const text = err ? `⚠️ Riley errored: ${(stderr || err.message).slice(0, 250)}` : stdout.trim().slice(0, 3800);
        try {
          const sent = await msg.reply(BOT_MARK + "✍️ Riley:\n" + text);
          if (sent?.id?._serialized) sentByBot.add(sent.id._serialized);
        } catch (e) { console.error("riley reply failed:", e.message); }
      });
    return;
  }

  // "@s <note>" = field intel for the social manager (Claude on the Mac).
  // Logged to .social_inbox/, pulled into the repo by the publisher's daily
  // run — no agent call, just capture and confirm.
  if (msg.type === "chat" && /^@s\s+/i.test(msg.body || "")) {
    const note = msg.body.replace(/^@s\s+/i, "").trim();
    if (note) {
      const dir = path.join(__dirname, ".social_inbox");
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, `intel-${Date.now()}.txt`),
        `${new Date().toISOString()} ${note}\n`);
      try {
        const sent = await msg.reply(`${BOT_MARK}📋 Logged for the social manager.`);
        if (sent?.id?._serialized) sentByBot.add(sent.id._serialized);
      } catch (e) {
        console.error("intel ack failed:", e.message);
      }
    }
    return;
  }

  let prompt;
  let imagePath;
  if (isLocation) {
    const loc = msg.location || {};
    const label = loc.name || loc.description || loc.address || "";
    prompt = `[Location shared]: latitude=${loc.latitude}, longitude=${loc.longitude}` + (label ? ` (${label})` : "");
    console.log(`-> received location: ${loc.latitude},${loc.longitude}${label ? ` (${label})` : ""}`);
  } else if (isVoice) {
    console.log("-> transcribing voice note...");
    try {
      const transcript = await transcribeVoiceNote(msg);
      prompt = `[Voice note]: ${transcript}`;
      console.log(`-> transcribed: ${transcript.slice(0, 80)}`);
    } catch (e) {
      // The previous "transcription failed: <message>" logged too little to
      // diagnose (an OpenAI SDK error's .message can be a single char). Surface
      // the HTTP status, code, type, and any API error payload so the real
      // cause (bad key, quota, unsupported model/format, network) is visible.
      const detail = e?.error || e?.response?.data || {};
      console.error(
        "transcription failed:",
        `status=${e?.status ?? ""}`,
        `code=${e?.code ?? ""}`,
        `type=${e?.type ?? ""}`,
        `ctor=${e?.constructor?.name ?? typeof e}`,
        `message=${e?.message ?? String(e)}`,
        `detail=${JSON.stringify(detail)}`
      );
      console.error("transcription failed [stack]:", e?.stack || "(no stack — error is not an Error object)");
      // Voice-note media download is currently broken by a whatsapp-web.js vs.
      // WhatsApp Web version drift (downloadMedia throws inside the WA page).
      // Give the user an actionable message instead of the raw internal error.
      try {
        const sent = await msg.reply(`${BOT_MARK}🎙️ I couldn't read that voice note — voice transcription is temporarily down. Please type it out and I'll help right away 🙏`);
        if (sent?.id?._serialized) sentByBot.add(sent.id._serialized);
      } catch (e) {
        console.error("voice-note error reply failed:", e.message);
      }
      return;
    }
  } else if (isImage) {
    try {
      const media = await msg.downloadMedia();
      if (!media || !media.data) {
        throw new Error("image media could not be downloaded (empty response from WhatsApp)");
      }
      const ext = (media.mimetype.split("/")[1] || "jpeg").split(";")[0];
      imagePath = path.join(os.tmpdir(), `wa_image_${Date.now()}.${ext}`);
      fs.writeFileSync(imagePath, Buffer.from(media.data, "base64"));
      prompt = (msg.body || "").trim() || "What's in this image?";
      console.log(`-> received image, caption: ${prompt.slice(0, 80)}`);
    } catch (e) {
      // Same root cause as the voice-note failure above: a whatsapp-web.js vs.
      // WhatsApp Web version drift makes downloadMedia() throw an opaque error
      // for all incoming media, not just audio. Log full diagnostics (a raw
      // .message can be a single opaque character, e.g. "r") and give the user
      // an actionable message instead of echoing that back.
      console.error(
        "image download failed:",
        `status=${e?.status ?? ""}`,
        `code=${e?.code ?? ""}`,
        `ctor=${e?.constructor?.name ?? typeof e}`,
        `message=${e?.message ?? String(e)}`
      );
      console.error("image download failed [stack]:", e?.stack || "(no stack — error is not an Error object)");
      try {
        const sent = await msg.reply(`${BOT_MARK}📷 I couldn't read that image — photo downloads are temporarily down. Please describe it in text (or type out the receipt details) and I'll help right away 🙏`);
        if (sent?.id?._serialized) sentByBot.add(sent.id._serialized);
      } catch (e2) {
        console.error("image error reply failed:", e2.message);
      }
      return;
    }
  } else {
    prompt = msg.body.replace(/^@a\s*/i, "").trim();
    if (!prompt) return;
  }

  agentQueue.push({ prompt, imagePath, msg });
  processQueue();
});

function processQueue() {
  if (agentBusy || agentQueue.length === 0) return;
  agentBusy = true;
  const { prompt, imagePath, msg, isBriefing } = agentQueue.shift();
  console.log(`-> agent: ${prompt.slice(0, 80)}`);
  const scriptArgs = imagePath ? [SCRIPT, prompt, imagePath] : [SCRIPT, prompt];
  execFile(
    PYTHON,
    scriptArgs,
    { timeout: 300_000, maxBuffer: 10 * 1024 * 1024, cwd: __dirname },
    async (err, stdout, stderr) => {
      if (imagePath) fs.unlink(imagePath, () => {});

      if (isBriefing) {
        try {
          if (err) {
            console.error("morning briefing failed:", (stderr || err.message).trim());
            await sendProactiveMessage(`⚠️ Morning briefing failed: ${(stderr || err.message).slice(0, 300)}`).catch(() => {});
          } else {
            try {
              let body = stdout.trim();
              // sendProactiveMessage only sends text — a PHOTO: marker (the briefing
              // prompt shouldn't trigger one, but nothing stops the model from calling
              // generate_image/gif_search) would otherwise be relayed verbatim as a raw
              // /tmp path, and its file would never get cleaned up.
              if (body.startsWith("PHOTO:")) {
                const lines = body.split("\n");
                const photoPath = lines[0].replace("PHOTO:", "").trim();
                fs.unlink(photoPath, () => {});
                body = lines.slice(1).join("\n").trim() || "(generated an image, but briefings are text-only — ask me directly to see it)";
              }
              await sendProactiveMessage("Good morning.\n\n" + body);
              console.log("-> morning briefing sent");
            } catch (e) {
              console.error("morning briefing send failed:", e.message);
            }
          }
        } finally {
          agentBusy = false;
          processQueue();
        }
        return;
      }

      if (err) {
        console.error("agent error (full):\n", stderr || err.message);
        await sendProactiveMessage(`⚠️ Agent/tool failure: ${(stderr || err.message).slice(0, 500)}`).catch(() => {});
      }

      const raw = err
        ? `⚠️ agent error: ${(stderr || err.message).slice(0, 1500)}`
        : stdout.trim() || "(empty reply)";

      console.log(`<- agent: ${raw.slice(0, 120)}`);

      try {
        if (raw.startsWith("PHOTO:")) {
          const lines = raw.split("\n");
          const photoPath = lines[0].replace("PHOTO:", "").trim();
          const caption = lines.slice(1).join("\n").trim();
          try {
            const media = MessageMedia.fromFilePath(photoPath);
            const chat = await withTimeout(msg.getChat(), 30_000, "msg.getChat");
            const sent = await withTimeout(
              chat.sendMessage(media, { caption: BOT_MARK + (caption || "") }),
              30_000,
              "chat.sendMessage"
            );
            if (sent?.id?._serialized) sentByBot.add(sent.id._serialized);
          } finally {
            // Delete regardless of whether the send above succeeded — a hung/failed
            // sendMessage (the exact "Puppeteer send hangs" case this file already
            // guards against with withTimeout) used to leave this file on disk forever.
            fs.unlink(photoPath, () => {});
          }
        } else {
          const sent = await withTimeout(msg.reply(BOT_MARK + raw.slice(0, 4000)), 30_000, "msg.reply");
          if (sent?.id?._serialized) sentByBot.add(sent.id._serialized);
        }
      } catch (e) {
        console.error("reply failed:", e.message);
      } finally {
        agentBusy = false;
        processQueue();
      }
    }
  );
}

// These four handlers all cover states where the WhatsApp client (or the
// whole process) is broken but Node itself hasn't crashed — pm2 would still
// report the process as "online" and the watchdog would never restart it,
// leaving the bot silently unresponsive. Exiting lets pm2's own autorestart
// bring it back immediately (the persisted LocalAuth session avoids a fresh
// QR scan unless WhatsApp itself logged the session out).
client.on("auth_failure", async (message) => {
  console.error("WhatsApp auth failure:", message);
  await sendProactiveMessage(`🚨 WhatsApp authentication failed: ${String(message).slice(0, 300)}`).catch(() => {});
  process.exit(1);
});

client.on("disconnected", (reason) => {
  console.error("WhatsApp disconnected:", reason);
  process.exit(1);
});

process.on("uncaughtException", async (error) => {
  console.error("uncaught exception:", error);
  await sendProactiveMessage(`🚨 Bot error: ${error.message.slice(0, 300)}`).catch(() => {});
  process.exit(1);
});

process.on("unhandledRejection", async (error) => {
  console.error("unhandled rejection:", error);
  await sendProactiveMessage(`🚨 Bot promise failure: ${String(error).slice(0, 300)}`).catch(() => {});
  process.exit(1);
});

client.initialize();
