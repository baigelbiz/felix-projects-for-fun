// WhatsApp -> Claude agent bridge (Baileys transport).
//
// Drop-in replacement for bot.js that talks to WhatsApp over Baileys' native
// WebSocket protocol instead of whatsapp-web.js + Puppeteer/Chrome. Motivation:
// whatsapp-web.js 1.34.7's downloadMedia() is broken against WhatsApp's current
// 2.3xxx web client (upstream issues wwebjs/whatsapp-web.js#201828 / #201833,
// still open, no fix; the old "pin to a 2.2xxx snapshot" workaround is dead —
// those snapshots were removed). That broke voice-note transcription and image
// handling. Baileys has no browser layer and a first-class media download API,
// which ends this class of breakage.
//
// This bot runs as a DEDICATED second WhatsApp number. Felix (ALLOWED_IDS)
// messages that number from his personal phone; the bot replies in the same
// chat. Everything downstream of the transport — the Python agent
// (agent_reply.py), the @m/@r/@s command routing, the morning briefing, the
// outbox, and state persistence — is preserved byte-for-byte from bot.js.
//
// Run with: node bot.baileys.js   (first run prints a QR to scan with the
// bot's second number, from WhatsApp > Settings > Linked Devices). Auth is
// stored in its OWN folder (.baileys_auth) so it never touches the existing
// whatsapp-web.js session in .wwebjs_auth — the old bot stays instantly
// restorable until this one is proven live.

const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  getContentType,
  downloadMediaMessage,
  Browsers,
} = require("@whiskeysockets/baileys");
const pino = require("pino");
const qrcode = require("qrcode");
const qrterm = require("qrcode-terminal");
const { execFile } = require("child_process");
const { promisify } = require("util");
const execFileP = promisify(execFile);
const path = require("path");
const fs = require("fs");
const os = require("os");
const OpenAI = require("openai").default;

require("dotenv").config({ path: path.join(__dirname, ".env") });
const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

const PYTHON = path.join(__dirname, ".venv", "bin", "python");
const SCRIPT = path.join(__dirname, "agent_reply.py");
const AUTH_DIR = path.join(__dirname, ".baileys_auth");

// Felix's primary number — only this contact can use the bot. WhatsApp may
// identify the same contact as either a phone-number JID (@s.whatsapp.net —
// Baileys' equivalent of whatsapp-web.js's @c.us) or an anonymized @lid, so we
// accept both. These are the same identities bot.js used, translated to
// Baileys' JID scheme.
const ALLOWED_IDS = [
  "18582629123@s.whatsapp.net",
  "225864126062745@lid",
  "38517333864587@lid",
];
// Match on the numeric user part too, so a device/agent suffix
// (e.g. "18582629123:12@s.whatsapp.net") or server-form drift still resolves.
const ALLOWED_USERS = new Set(ALLOWED_IDS.map((j) => j.split("@")[0].split(":")[0]));
function jidUser(jid) {
  return (jid || "").split("@")[0].split(":")[0];
}
function isAllowed(jid) {
  return ALLOWED_IDS.includes(jid) || ALLOWED_USERS.has(jidUser(jid));
}

const BOT_MARK = "🤖 "; // prefix on every bot reply, used to ignore our own messages

let agentBusy = false;
const agentQueue = [];
const STATE_FILE = path.join(__dirname, ".bot_state.json");
let botState = {};
try {
  botState = JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
} catch (_) {}

// The live socket. Reassigned on reconnect, so all senders read this global
// rather than closing over one instance.
let sock = null;
let clientReady = false;
// One-time setup (startup alert, briefing scheduler, outbox drain) must run
// once — not again on every transient reconnect's "open" event.
let startedOnce = false;
// Guards against two overlapping reconnects: Baileys can emit "close" twice in
// quick succession on a flaky connection, and without this each would spawn
// its own socket + listener set, risking messages being handled twice.
let reconnectScheduled = false;
// Preferred proactive-send target: the chat of the most recent owner message.
let lastOwnerJid = null;

// Baileys sends resolve/reject on their own, but a wedged socket could still
// leave a send pending forever. Race every send against a timeout so the
// message queue's agentBusy mutex can never hang indefinitely (same guarantee
// bot.js needed for its Puppeteer sends).
function withTimeout(promise, ms, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

async function sendText(jid, text, quoted) {
  return withTimeout(
    sock.sendMessage(jid, { text }, quoted ? { quoted } : undefined),
    45_000,
    "sendMessage"
  );
}

async function sendProactiveMessage(text) {
  const body = BOT_MARK + text.slice(0, 4000);
  if (!sock) throw new Error("sendProactiveMessage: socket not ready");

  // Try delivery targets in order of reliability, first success wins:
  //   1. the chat of the most recent incoming owner message
  //   2. each allowed id (phone JID, then @lid forms)
  const targets = [];
  if (lastOwnerJid) targets.push(lastOwnerJid);
  for (const id of ALLOWED_IDS) if (id !== lastOwnerJid) targets.push(id);

  let lastErr;
  for (const jid of targets) {
    try {
      return await sendText(jid, body);
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr || new Error("sendProactiveMessage: all delivery targets failed");
}

function saveBotState() {
  // Write via temp file + rename so a pm2 restart mid-write can't leave a
  // truncated, unparseable state file (which would silently reset botState to
  // {} on next boot and re-fire the morning briefing that day).
  const tmp = `${STATE_FILE}.tmp${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify(botState, null, 2));
  fs.renameSync(tmp, STATE_FILE);
}

// Routed through the same agentQueue/agentBusy mutex as interactive messages
// (see processQueue) — both invocations run agent_reply.py, which reads and
// rewrites the shared .whatsapp_session and .assistant_memory.json files, so
// running two at once could clobber each other's state.
function sendMorningBriefing(date) {
  agentQueue.push({
    prompt:
      "[Scheduled morning briefing] Prepare my concise morning briefing for today. " +
      "Read both business and personal calendars, search Close CRM for new leads or follow-ups due, " +
      "and include only useful action items. Use short WhatsApp-friendly bullets.",
    isBriefing: true,
    briefingDate: date,
  });
  processQueue();
}

// In-memory only (not persisted): stops the 30s poll from re-queueing the
// briefing repeatedly within the same 07:00-07:02 window. botState.briefingDate
// (persisted) is only set once the briefing has actually been sent — see
// processQueue's isBriefing branch — so a crash/restart mid-send correctly
// retries instead of silently skipping the day's briefing forever.
let briefingQueuedDate = null;

function scheduleMorningBriefing() {
  const check = () => {
    const now = new Date();
    const israel = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Jerusalem", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false,
    }).formatToParts(now).reduce((out, part) => ((out[part.type] = part.value), out), {});
    const date = `${israel.year}-${israel.month}-${israel.day}`;
    if (
      israel.hour === "07" &&
      Number(israel.minute) < 2 &&
      botState.briefingDate !== date &&
      briefingQueuedDate !== date
    ) {
      briefingQueuedDate = date;
      sendMorningBriefing(date);
    }
  };
  check();
  setInterval(check, 30_000);
}

// Outbox: external scripts (the cron watchdog) drop .txt files here to have
// them sent to Felix. In-process alerts can't fire when the bot process is
// dead — the watchdog writes here, and the alert goes out once we're back up.
const OUTBOX_DIR = path.join(__dirname, ".outbox");
let outboxDraining = false;

function startOutboxDrain() {
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
}

// Baileys returns media as a Buffer directly (no base64 round-trip, no browser
// evaluate — this is the whole reason for the migration).
async function downloadBuffer(m) {
  return downloadMediaMessage(
    m,
    "buffer",
    {},
    { logger: pino({ level: "silent" }), reuploadRequest: sock.updateMediaMessage }
  );
}

// OpenAI Whisper's hard upload limit is 25 MB. Guard so an over-limit file
// gives a clear message instead of an opaque API error.
const WHISPER_MAX_BYTES = 25 * 1024 * 1024;

const HEBREW_BIAS =
  "שיחה עסקית בעברית על נדל\"ן. שמות של אנשים, מקומות וחברות באנגלית נשארים באנגלית, " +
  "למשל: Kenneth, North Carolina, San Diego, Close CRM, Shefa Homes.";

// Pick a Whisper-accepted container extension from the WhatsApp mimetype (or
// filename). A forwarded voice note is opus-in-ogg; a phone-call recording sent
// as a file is usually m4a/mp3/wav.
function whisperExt(mimetype, fileName) {
  const mt = (mimetype || "").toLowerCase();
  if (/(ogg|opus)/.test(mt)) return "ogg";
  if (/(mpeg|mp3|mpga)/.test(mt)) return "mp3";
  if (/(mp4|m4a|aac)/.test(mt)) return "m4a";
  if (/wav/.test(mt)) return "wav";
  if (/webm/.test(mt)) return "webm";
  if (/flac/.test(mt)) return "flac";
  if (/amr/.test(mt)) return "amr";
  if (/3gp/.test(mt)) return "3gp";
  const m = (fileName || "").toLowerCase().match(/\.([a-z0-9]+)$/);
  const ok = ["ogg", "oga", "mp3", "mpga", "mpeg", "m4a", "mp4", "wav", "webm", "flac", "amr", "3gp", "3gpp"];
  if (m && ok.includes(m[1])) return m[1] === "oga" ? "ogg" : (m[1] === "3gpp" ? "3gp" : m[1]);
  return "ogg"; // sane default for WhatsApp audio
}

// ffmpeg lets us transcode any recording (incl. formats Whisper rejects like
// .amr/.3gp) into compact 16 kHz mono Opus — roughly 7 MB per hour of speech,
// so long phone calls fit under the 25 MB cap. Checked once and cached; the bot
// still works without ffmpeg, just without transcode/compression.
let _ffmpegState = null;
async function ffmpegOk() {
  if (_ffmpegState !== null) return _ffmpegState;
  try {
    await execFileP("ffmpeg", ["-version"]);
    _ffmpegState = true;
  } catch {
    _ffmpegState = false;
    console.warn("ffmpeg not found — audio goes to Whisper as-is (no transcode/compress; .amr and >25 MB recordings may fail). Install with: apt-get install -y ffmpeg");
  }
  return _ffmpegState;
}

// Download WhatsApp audio and transcribe it with Whisper. `opts.language`
// (e.g. "he") biases a language; omit it to let Whisper auto-detect — the right
// choice for phone-call recordings, which may be English, Hebrew, or mixed.
// `opts.biasPrompt` nudges the spelling of proper nouns.
async function transcribeAudio(m, opts = {}) {
  const buf = await downloadBuffer(m);
  if (!buf || !buf.length) {
    throw new Error("audio media could not be downloaded (empty response from WhatsApp)");
  }
  console.log(`-> audio downloaded: ${buf.length} bytes`);
  const rawFile = path.join(os.tmpdir(), `wa_audio_${Date.now()}.${opts.ext || "ogg"}`);
  fs.writeFileSync(rawFile, buf);
  let workFile = rawFile;
  let transcoded = null;
  try {
    if (await ffmpegOk()) {
      transcoded = path.join(os.tmpdir(), `wa_audio_${Date.now()}_16k.ogg`);
      try {
        await execFileP(
          "ffmpeg",
          ["-y", "-i", rawFile, "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "16k", transcoded],
          { timeout: 180_000 }
        );
        workFile = transcoded;
        console.log(`-> transcoded to ${fs.statSync(transcoded).size} bytes (16 kHz mono opus)`);
      } catch (te) {
        console.warn("ffmpeg transcode failed, falling back to original:", (te.message || "").slice(0, 200));
        // Keep `transcoded` set: ffmpeg writes its output as it goes, so a
        // mid-transcode failure or timeout leaves a partial file the cleanup
        // below must still unlink.
        workFile = rawFile;
      }
    }
    const size = fs.statSync(workFile).size;
    if (size > WHISPER_MAX_BYTES) {
      const err = new Error(`audio is ${(size / 1048576).toFixed(1)} MB, over Whisper's 25 MB limit`);
      err.tooLarge = true;
      throw err;
    }
    console.log("-> calling OpenAI whisper...");
    const params = { file: fs.createReadStream(workFile), model: "whisper-1" };
    if (opts.language) params.language = opts.language;
    if (opts.biasPrompt) params.prompt = opts.biasPrompt;
    const transcript = await openai.audio.transcriptions.create(params);
    return transcript.text;
  } finally {
    fs.unlink(rawFile, () => {});
    if (transcoded) fs.unlink(transcoded, () => {});
  }
}

// Unwrap the common envelope wrappers so getContentType sees the real content.
function unwrap(message) {
  let content = message;
  if (content?.ephemeralMessage) content = content.ephemeralMessage.message;
  if (content?.viewOnceMessage) content = content.viewOnceMessage.message;
  if (content?.viewOnceMessageV2) content = content.viewOnceMessageV2.message;
  if (content?.documentWithCaptionMessage)
    content = content.documentWithCaptionMessage.message;
  return content;
}

function extractText(content, type) {
  if (type === "conversation") return content.conversation || "";
  if (type === "extendedTextMessage") return content.extendedTextMessage?.text || "";
  return "";
}

async function handleMessage(m) {
  if (!m.message) return;
  const jid = m.key.remoteJid;
  // Ignore the bot's own sent messages (loop protection) and anything outside
  // the dedicated 1:1 with Felix (no groups, no other contacts).
  if (m.key.fromMe) return;
  if (!jid || jid.endsWith("@g.us") || jid === "status@broadcast") return;
  if (!isAllowed(jid)) return;

  const content = unwrap(m.message);
  if (!content) return;
  const type = getContentType(content);

  const isVoice = type === "audioMessage";
  // A recorded phone call is usually forwarded as a file attachment, not a
  // voice note — accept documents whose mimetype/filename looks like audio.
  const isAudioDoc =
    type === "documentMessage" &&
    /(audio|mpeg|mp3|mpga|m4a|mp4|ogg|opus|wav|webm|flac|aac|amr)/i.test(
      `${content.documentMessage?.mimetype || ""} ${content.documentMessage?.fileName || ""}`
    );
  const isImage = type === "imageMessage";
  const isLocation = type === "locationMessage";
  const isText = type === "conversation" || type === "extendedTextMessage";
  if (!isVoice && !isAudioDoc && !isImage && !isLocation && !isText) return;

  const body = isText ? extractText(content, type) : "";
  // Belt-and-suspenders: never answer our own prefixed replies.
  if (body.startsWith(BOT_MARK)) return;

  console.log(
    `[msg] from=${jid} type=${type} body=${JSON.stringify((body || "").slice(0, 60))}`
  );

  // Capture the live chat for proactive sends (briefing, alerts, outbox).
  lastOwnerJid = jid;

  const reply = async (text) => {
    try {
      await sendText(jid, BOT_MARK + text, m);
    } catch (e) {
      console.error("reply failed:", e.message);
    }
  };

  // "@m status|pause|resume" = Miles the Publisher, deterministic commands.
  if (isText && /^@m(iles)?\s+/i.test(body)) {
    const cmd = body.replace(/^@m(iles)?\s+/i, "").trim().toLowerCase();
    const PUB = "/root/publisher";
    let out;
    try {
      if (cmd === "pause") {
        fs.writeFileSync(path.join(PUB, "PAUSED"), new Date().toISOString());
        out = "⏸ Publishing paused. Nothing posts until you send '@m resume'.";
      } else if (cmd === "resume") {
        fs.rmSync(path.join(PUB, "PAUSED"), { force: true });
        out = "▶️ Publishing resumed. Next scheduled post goes out normally.";
      } else {
        const sched = JSON.parse(fs.readFileSync(path.join(PUB, "schedule.json"), "utf8"));
        const paused = fs.existsSync(path.join(PUB, "PAUSED"));
        const lines = sched.map((p) => {
          const st = p.posted ? "✅" : "🕘";
          const plats = (p.platforms || []).join("+");
          return `${st} ${p.date} ${p.pillar || ""} (${plats})`;
        });
        out = `Miles here${paused ? " — ⏸ PAUSED" : ""}. Schedule:\n` + lines.join("\n");
      }
    } catch (e) {
      out = `⚠️ Miles hit an error: ${e.message.slice(0, 200)}`;
    }
    await reply(out);
    return;
  }

  // "@r <request>" = Riley the Content Manager drafting from the server.
  if (isText && /^@r(iley)?\s+/i.test(body)) {
    const req = body.replace(/^@r(iley)?\s+/i, "").trim();
    execFile(PYTHON, ["/root/publisher/riley_reply.py", req],
      { timeout: 120_000, maxBuffer: 1024 * 1024, cwd: "/root/publisher" },
      async (err, stdout, stderr) => {
        const text = err ? `⚠️ Riley errored: ${(stderr || err.message).slice(0, 250)}` : stdout.trim().slice(0, 3800);
        await reply("✍️ Riley:\n" + text);
      });
    return;
  }

  // "@s <note>" = field intel for the social manager (Claude on the Mac).
  if (isText && /^@s\s+/i.test(body)) {
    const note = body.replace(/^@s\s+/i, "").trim();
    if (note) {
      const dir = path.join(__dirname, ".social_inbox");
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, `intel-${Date.now()}.txt`),
        `${new Date().toISOString()} ${note}\n`);
      await reply("📋 Logged for the social manager.");
    }
    return;
  }

  let prompt;
  let imagePath;
  if (isLocation) {
    const loc = content.locationMessage || {};
    const label = loc.name || loc.address || "";
    prompt = `[Location shared]: latitude=${loc.degreesLatitude}, longitude=${loc.degreesLongitude}` + (label ? ` (${label})` : "");
    console.log(`-> received location: ${loc.degreesLatitude},${loc.degreesLongitude}${label ? ` (${label})` : ""}`);
  } else if (isVoice || isAudioDoc) {
    // A ptt voice note stays the quick conversational assistant. A non-ptt
    // audio clip or an audio file/document is treated as a recording to
    // process: transcribed, then handled per the caption you send with it
    // (or summarized into bullets by default).
    const audioMsg = isVoice ? content.audioMessage : content.documentMessage;
    const isPttNote = isVoice && audioMsg?.ptt;
    const ext = whisperExt(audioMsg?.mimetype, audioMsg?.fileName);
    const caption = (content.documentMessage?.caption || "").trim();
    console.log(`-> transcribing ${isPttNote ? "voice note" : "audio recording"} (ext=${ext})...`);
    try {
      const transcript = await transcribeAudio(m, {
        ext,
        language: isPttNote ? "he" : undefined,
        biasPrompt: isPttNote ? HEBREW_BIAS : undefined,
      });
      console.log(`-> transcribed ${transcript.length} chars: ${transcript.slice(0, 80)}`);
      if (isPttNote) {
        prompt = `[Voice note]: ${transcript}`;
      } else {
        const instruction =
          caption ||
          "Summarize this recorded phone call into concise, WhatsApp-friendly bullet points — " +
            "key points, decisions, names, numbers/dates, and any action items or follow-ups.";
        prompt = `[Transcribed audio recording. Do this with it: ${instruction}]\n\nTranscript:\n${transcript}`;
      }
    } catch (e) {
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
      console.error("transcription failed [stack]:", e?.stack || "(no stack)");
      if (e?.tooLarge) {
        await reply(`🎙️ That recording is too big for me to transcribe (${e.message.match(/[\d.]+ MB/)?.[0] || "over 25 MB"}). Whisper caps audio at 25 MB — please send a shorter clip or a compressed copy.`);
      } else {
        await reply("🎙️ I couldn't transcribe that audio — transcription is temporarily down, or it's a format I can't read (try m4a, mp3, ogg, or wav). Please try again or type it out 🙏");
      }
      return;
    }
  } else if (isImage) {
    try {
      const buf = await downloadBuffer(m);
      if (!buf || !buf.length) {
        throw new Error("image media could not be downloaded (empty response from WhatsApp)");
      }
      const mime = content.imageMessage?.mimetype || "image/jpeg";
      const ext = (mime.split("/")[1] || "jpeg").split(";")[0];
      imagePath = path.join(os.tmpdir(), `wa_image_${Date.now()}.${ext}`);
      fs.writeFileSync(imagePath, buf);
      prompt = (content.imageMessage?.caption || "").trim() || "What's in this image?";
      console.log(`-> received image, caption: ${prompt.slice(0, 80)}`);
    } catch (e) {
      console.error(
        "image download failed:",
        `ctor=${e?.constructor?.name ?? typeof e}`,
        `message=${e?.message ?? String(e)}`
      );
      console.error("image download failed [stack]:", e?.stack || "(no stack)");
      await reply("📷 I couldn't read that image — photo downloads are temporarily down. Please describe it in text (or type out the receipt details) and I'll help right away 🙏");
      return;
    }
  } else {
    prompt = body.replace(/^@a\s*/i, "").trim();
    if (!prompt) return;
  }

  agentQueue.push({ prompt, imagePath, jid, quoted: m });
  processQueue();
}

function processQueue() {
  if (agentBusy || agentQueue.length === 0) return;
  agentBusy = true;
  const { prompt, imagePath, jid, quoted, isBriefing, briefingDate } = agentQueue.shift();
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
              let out = stdout.trim();
              if (out.startsWith("PHOTO:")) {
                const lines = out.split("\n");
                const photoPath = lines[0].replace("PHOTO:", "").trim();
                fs.unlink(photoPath, () => {});
                out = lines.slice(1).join("\n").trim() || "(generated an image, but briefings are text-only — ask me directly to see it)";
              }
              await sendProactiveMessage("Good morning.\n\n" + out);
              console.log("-> morning briefing sent");
              // Persist only now that the briefing has actually gone out — not
              // when scheduled. See bot.js for the full rationale.
              if (botState.briefingDate !== briefingDate) {
                botState.briefingDate = briefingDate;
                saveBotState();
              }
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
            await withTimeout(
              sock.sendMessage(jid, {
                image: fs.readFileSync(photoPath),
                caption: BOT_MARK + (caption || ""),
              }),
              45_000,
              "sendMessage(image)"
            );
          } finally {
            // Delete regardless of whether the send succeeded.
            fs.unlink(photoPath, () => {});
          }
        } else {
          await sendText(jid, BOT_MARK + raw.slice(0, 4000), quoted);
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

// --- Connection lifecycle -------------------------------------------------

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  sock = makeWASocket({
    auth: state,
    logger: pino({ level: "silent" }),
    browser: Browsers.macOS("Desktop"),
    markOnlineOnConnect: false,
    syncFullHistory: false,
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (u) => {
    const { connection, qr, lastDisconnect } = u;

    if (qr) {
      qrterm.generate(qr, { small: true });
      await qrcode.toFile(path.join(__dirname, "qr.png"), qr, { width: 500 });
      console.log("QR code written to qr.png — scan from WhatsApp > Settings > Linked Devices (use the bot's second number).");
    }

    if (connection === "open") {
      clientReady = true;
      console.log("READY: WhatsApp bridge is live (Baileys).");
      console.log("LINKED AS:", sock.user?.id || "(wid unknown)");
      if (!startedOnce) {
        startedOnce = true;
        if (process.env.BOT_SEND_STARTUP_ALERT !== "false") {
          await sendProactiveMessage(
            `✅ WhatsApp assistant is online (${new Date().toLocaleString("en-IL", { timeZone: "Asia/Jerusalem" })}).`
          ).catch((e) => console.error("startup alert failed:", e.message));
        }
        scheduleMorningBriefing();
        startOutboxDrain();
      }
    }

    if (connection === "close") {
      clientReady = false;
      const code = lastDisconnect?.error?.output?.statusCode;
      console.error("WhatsApp connection closed:", code, lastDisconnect?.error?.message || "");
      // Unrecoverable — the session is gone; exit so pm2 restarts us (and the
      // next boot prints a fresh QR to re-link).
      if (
        code === DisconnectReason.loggedOut ||
        code === DisconnectReason.badSession ||
        code === DisconnectReason.forbidden ||
        code === DisconnectReason.connectionReplaced
      ) {
        await sendProactiveMessage(`🚨 WhatsApp session ended (code ${code}) — needs re-link.`).catch(() => {});
        process.exit(1);
      }
      // Recoverable (network blip, restartRequired, timeout) — reconnect in
      // place. Unlike whatsapp-web.js's dead-Puppeteer disconnects, these are
      // routine and cheap, so we don't churn the whole pm2 process for them.
      // Guard against a second "close" firing before this reconnect lands —
      // without it, two overlapping start() calls would each open their own
      // socket and could both end up handling (and replying to) the same
      // incoming messages.
      if (reconnectScheduled) return;
      reconnectScheduled = true;
      setTimeout(() => start().catch((e) => {
        console.error("reconnect failed:", e.message);
        process.exit(1);
      }).finally(() => {
        reconnectScheduled = false;
      }), 2_000);
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const m of messages) {
      try {
        await handleMessage(m);
      } catch (e) {
        console.error("message handler error:", e?.message || e);
      }
    }
  });
}

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

start().catch((e) => {
  console.error("failed to start:", e);
  process.exit(1);
});
