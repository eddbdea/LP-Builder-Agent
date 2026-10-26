# 1. Importuri
import os
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, TextBlock

# 2. Cheile
load_dotenv()
os.environ.pop("ANTHROPIC_API_KEY", None)
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
MY_ID = int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])
FOLDER = Path(__file__).parent

# 3. Setările lui Claude
optiuni = ClaudeAgentOptions(
    cwd=str(FOLDER),
    setting_sources=["project"],
    allowed_tools=["WebSearch", "WebFetch"],
    model=os.getenv("CLAUDE_MODEL", "sonnet"),
)


# 4. Trimite textul la Claude și adună răspunsul
async def intreaba_claude(text):
    raspuns = ""
    async for mesaj in query(prompt=text, options=optiuni):
        if isinstance(mesaj, AssistantMessage):
            for bucata in mesaj.content:
                if isinstance(bucata, TextBlock):
                    raspuns += bucata.text + "\n"
    return raspuns.strip() or "(n-am primit răspuns)"


# 5. Ce face botul la fiecare mesaj
async def raspunde(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != MY_ID:
        return
    await update.message.reply_text("Mă gândesc...")
    raspuns = await intreaba_claude(update.message.text)
    for i in range(0, len(raspuns), 4000):
        await update.message.reply_text(raspuns[i:i + 4000])


# 6. Pornim botul
app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT, raspunde))
print("Botul a pornit. Ctrl+C ca să-l oprești.")
app.run_polling()