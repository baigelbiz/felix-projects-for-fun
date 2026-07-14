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

async function sendProactiveMessage(text) {
  const sent = await client.sendMessage(ALLOWED_IDS[0], BOT_MARK + text.slice(0, 4000));
  sentByBot.add(sent.id?._serialized);
  return sent;
}

function saveBotState() {
  fs.writeFileSync(STATE_FILE, JSON.stringify(botState, null, 2));
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
  console.log("READY: WhatsApp bridge is live. Message yourself starting with '@a '.");
  if (process.env.BOT_SEND_STARTUP_ALERT !== "false") {
    await sendProactiveMessage(`✅ WhatsApp assistant is online (${new Date().toLocaleString("en-IL", { timeZone: "Asia/Jerusalem" })}).`).catch((e) => console.error("startup alert failed:", e.message));
  }
  scheduleMorningBriefing();
});

async function transcribeVoiceNote(msg) {
  const media = await msg.downloadMedia();
  const tmpFile = path.join(os.tmpdir(), `wa_voice_${Date.now()}.ogg`);
  fs.writeFileSync(tmpFile, Buffer.from(media.data, "base64"));
  try {
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
  const isVoice = msg.type === "ptt" || msg.type === "audio";
  const isImage = msg.type === "image";
  const isLocation = msg.type === "location";
  if (!isVoice && !isImage && !isLocation && (!msg.body || msg.type !== "chat")) return; // text, voice, image, or location only

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
      console.error("transcription failed:", e.message);
      msg.reply(`${BOT_MARK}⚠️ Couldn't transcribe voice note: ${e.message.slice(0, 200)}`);
      return;
    }
  } else if (isImage) {
    try {
      const media = await msg.downloadMedia();
      const ext = (media.mimetype.split("/")[1] || "jpeg").split(";")[0];
      imagePath = path.join(os.tmpdir(), `wa_image_${Date.now()}.${ext}`);
      fs.writeFileSync(imagePath, Buffer.from(media.data, "base64"));
      prompt = (msg.body || "").trim() || "What's in this image?";
      console.log(`-> received image, caption: ${prompt.slice(0, 80)}`);
    } catch (e) {
      console.error("image download failed:", e.message);
      msg.reply(`${BOT_MARK}⚠️ Couldn't download image: ${e.message.slice(0, 200)}`);
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
            await sendProactiveMessage("Good morning.\n\n" + stdout.trim());
            console.log("-> morning briefing sent");
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
          const media = MessageMedia.fromFilePath(photoPath);
          const chat = await msg.getChat();
          const sent = await chat.sendMessage(media, { caption: BOT_MARK + (caption || "") });
          sentByBot.add(sent.id?._serialized);
          fs.unlinkSync(photoPath);
        } else {
          const sent = await msg.reply(BOT_MARK + raw.slice(0, 4000));
          sentByBot.add(sent.id?._serialized);
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

client.on("auth_failure", (message) => {
  console.error("WhatsApp auth failure:", message);
  sendProactiveMessage(`🚨 WhatsApp authentication failed: ${String(message).slice(0, 300)}`).catch(() => {});
});

client.on("disconnected", (reason) => {
  console.error("WhatsApp disconnected:", reason);
});

process.on("uncaughtException", (error) => {
  console.error("uncaught exception:", error);
  sendProactiveMessage(`🚨 Bot error: ${error.message.slice(0, 300)}`).catch(() => {});
});

process.on("unhandledRejection", (error) => {
  console.error("unhandled rejection:", error);
  sendProactiveMessage(`🚨 Bot promise failure: ${String(error).slice(0, 300)}`).catch(() => {});
});

client.initialize();
