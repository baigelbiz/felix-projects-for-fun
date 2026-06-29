// Minimal zero-dependency server: serves the landing page and creates a
// seller lead in Close when the form is submitted.
//
//   CLOSE_API_KEY=api_xxx node server.js
//
// Then open http://localhost:3000

const http = require("http");
const https = require("https");
const fs = require("fs");
const path = require("path");

const PORT = process.env.PORT || 3000;
const CLOSE_API_KEY = process.env.CLOSE_API_KEY;

// --- Close custom field IDs (from the Shefa Homes org) ---
const CF = {
  propertyAddress: "cf_sIxjpsU5b9yWo5rlbK851iI2vwPM4AWxHhpKxFVtfyX",
  sellerTimeline: "cf_VlYmlPqhNsFxbFgp5RoB4efupGne5eWFHoRRn9lKOQV",
  motivationNotes: "cf_Gh5nsaLg3DLDGlJvMGPZqYieeOTrLUYKuKCYljXOFFK",
  outreachStatus: "cf_n8nDrLez2kSHOlgfvAygMC1DEH3QiausmSdBlBIXq7Y",
  dnc: "cf_uQzChf9gYKWYdgiwD1jZOauPudZ4d2Bh8b1NVaOJlCJ",
  smsOptOut: "cf_l9JCkx3Z246FRJKMzouHE1tGZbBuztwikdRoApmjiB4",
  leadSource: "cf_ZWTJcKUXAwASlZcbZG6yFGyIWctqMgVG5UEtFrCt2ME",
};

function createCloseLead(form) {
  const motivation = [
    form.condition ? `Condition: ${form.condition}` : null,
    form.reason ? `Reason: ${form.reason}` : null,
    form.notes ? `Notes: ${form.notes}` : null,
    "Source: Website — Free Cash Offer form",
  ]
    .filter(Boolean)
    .join(" | ");

  const payload = {
    name: form.name || form.address || "Website Seller Lead",
    contacts: [
      {
        name: form.name || "",
        emails: form.email ? [{ email: form.email, type: "home" }] : [],
        phones: form.phone ? [{ phone: form.phone, type: "home" }] : [],
      },
    ],
    addresses: form.address ? [{ label: "business", address_1: form.address }] : [],
    [`custom.${CF.propertyAddress}`]: form.address || "",
    [`custom.${CF.sellerTimeline}`]: form.timeline || "",
    [`custom.${CF.motivationNotes}`]: motivation,
    [`custom.${CF.outreachStatus}`]: "Hot Lead",
    [`custom.${CF.dnc}`]: "No",
    [`custom.${CF.smsOptOut}`]: "No",
    [`custom.${CF.leadSource}`]: "Other",
  };

  const body = JSON.stringify(payload);
  const auth = Buffer.from(`${CLOSE_API_KEY}:`).toString("base64");

  return new Promise((resolve, reject) => {
    const req = https.request(
      {
        method: "POST",
        hostname: "api.close.com",
        path: "/api/v1/lead/",
        headers: {
          Authorization: `Basic ${auth}`,
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            resolve(JSON.parse(data));
          } else {
            reject(new Error(`Close API ${res.statusCode}: ${data}`));
          }
        });
      }
    );
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

const server = http.createServer((req, res) => {
  // Serve the landing page
  if (req.method === "GET" && (req.url === "/" || req.url === "/free-leads.html")) {
    fs.readFile(path.join(__dirname, "free-leads.html"), (err, html) => {
      if (err) {
        res.writeHead(500);
        return res.end("Could not load page");
      }
      res.writeHead(200, { "Content-Type": "text/html" });
      res.end(html);
    });
    return;
  }

  // Handle the form submission
  if (req.method === "POST" && req.url === "/api/lead") {
    let body = "";
    req.on("data", (c) => {
      body += c;
      if (body.length > 1e5) req.destroy(); // basic abuse guard
    });
    req.on("end", async () => {
      try {
        const form = JSON.parse(body || "{}");
        if (!form.address || !form.phone) {
          res.writeHead(400, { "Content-Type": "application/json" });
          return res.end(JSON.stringify({ ok: false, error: "Missing required fields" }));
        }
        if (!CLOSE_API_KEY) throw new Error("CLOSE_API_KEY is not set");
        const lead = await createCloseLead(form);
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok: true, leadId: lead.id }));
      } catch (e) {
        console.error("Lead create failed:", e.message);
        res.writeHead(502, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok: false, error: "Could not save lead" }));
      }
    });
    return;
  }

  res.writeHead(404);
  res.end("Not found");
});

server.listen(PORT, () => {
  console.log(`Landing page running at http://localhost:${PORT}`);
  if (!CLOSE_API_KEY) {
    console.warn("⚠️  CLOSE_API_KEY not set — form submissions will fail until you set it.");
  }
});
