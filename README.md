# Felix Projects for Fun

A collection of AI-powered tools built for fun.

---

## 🛍️ Shefa Listings — Second-hand listing generator

Snap a photo of anything you want to sell → Claude analyzes it → searches live market prices → generates Hebrew listings ready to post on **Yad2** and **Facebook Marketplace**.

### Features
- 📊 Live market price research (searches Yad2 & Facebook Marketplace)
- 📋 Ready-to-post Yad2 listing (title, description, price, category)
- 💙 Ready-to-post Facebook Marketplace listing
- 💡 Selling tips
- 🤖 Telegram bot: send a photo, get a listing back in chat

### Setup

1. Copy `.env.example` to `.env` and fill in your keys:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   TELEGRAM_BOT_TOKEN=...       # optional
   WEBHOOK_BASE_URL=https://... # optional, needed for Telegram webhook
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run:
   ```bash
   python listing_app.py
   ```

4. Open **http://localhost:8080**

### Telegram bot setup (optional)

After deploying to a public URL, call:
```
GET /set_webhook
```
to register the webhook with Telegram. Then send a photo to your bot.
