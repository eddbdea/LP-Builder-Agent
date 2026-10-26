# Agentul SuperPractic pe Telegram
# Research-ul îl face agentul principal (Sonnet); copy-ul și pagina le face agentul „copywriter” (Opus).

# 1. Importuri
import os
import time
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AgentDefinition,      # ca să definim al doilea agent
    AssistantMessage,
    TextBlock,
    RateLimitEvent,       # informații despre limita abonamentului
)

# 2. Cheile din .env
load_dotenv()
os.environ.pop("ANTHROPIC_API_KEY", None)   # folosim abonamentul Pro, nu API-ul
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_IDS = {int(x) for x in os.environ["TELEGRAM_ALLOWED_CHAT_ID"].split(",")}
FOLDER = Path(__file__).parent              # folderul în care stau bot.py și CLAUDE.md

# Modelele și intensitățile
MODEL_RESEARCH = os.getenv("MODEL_RESEARCH", "sonnet")
EFFORT_RESEARCH = os.getenv("EFFORT_RESEARCH", "medium")
MODEL_COPY = os.getenv("MODEL_COPY", "claude-opus-5-5")
EFFORT_COPY = os.getenv("EFFORT_COPY", "medium")

# 3. Al doilea agent: copywriter-ul (Opus)
copywriter = AgentDefinition(
    description="Scrie copy-ul paginii de produs și creează pagina în Shopify, după CLAUDE.md. "
                "Folosește-l pentru Pasul 2 și Pasul 3.",
    prompt="Ești copywriter-ul SuperPractic. Scrii copy-ul în formatul fix din CLAUDE.md, "
           "pentru clientul român, fără să inventezi nimic din ce nu e în research-ul primit. "
           "Răspunzi în română.",
    tools=["Read"],
    model=MODEL_COPY,
    effort=EFFORT_COPY,
)

# 4. Setările agentului principal (research)
optiuni = ClaudeAgentOptions(
    cwd=str(FOLDER),
    setting_sources=["project"],            # citește CLAUDE.md
    allowed_tools=["WebSearch", "WebFetch", "Agent"],
    disallowed_tools=["Bash", "Edit", "Write", "NotebookEdit", "TodoWrite"],
    agents={"copywriter": copywriter},
    max_turns=25,
    model=MODEL_RESEARCH,
    effort=EFFORT_RESEARCH,
)

# Ultima informație primită despre limita abonamentului
ultima_limita = {}


# 5. Textul cu limita (pentru comanda /usage)
def text_limita():
    if not ultima_limita:
        return ""
    info = ultima_limita["info"]
    rand = "Abonament"
    if info.utilization is not None:
        rand += f": {round(info.utilization * 100)}% folosit"
    if info.resets_at:
        ora = datetime.fromtimestamp(info.resets_at).strftime("%H:%M")
        minute = max(0, int((info.resets_at - time.time()) / 60))
        rand += f" · reset la {ora} (peste {minute // 60}h {minute % 60}min)"
    if info.status == "rejected":
        rand += " · LIMITĂ ATINSĂ"
    return "\n\n— " + rand


# 6. Trimite textul la Claude și adună răspunsul
async def intreaba_claude(text):
    raspuns = ""
    async for mesaj in query(prompt=text, options=optiuni):
        # doar textul agentului principal (nu și mesajele intermediare ale copywriter-ului)
        if isinstance(mesaj, AssistantMessage) and not mesaj.parent_tool_use_id:
            for bucata in mesaj.content:
                if isinstance(bucata, TextBlock):
                    raspuns += bucata.text + "\n"
        if isinstance(mesaj, RateLimitEvent):
            ultima_limita["info"] = mesaj.rate_limit_info
    return raspuns.strip() or "(n-am primit răspuns)"


# 7. Ce face botul la fiecare mesaj
async def raspunde(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ALLOWED_IDS:
        return
    await update.message.reply_text("Mă gândesc...")
    try:
        raspuns = await intreaba_claude(update.message.text)
    except Exception as eroare:
        raspuns = f"A apărut o eroare: {eroare}"
    for i in range(0, len(raspuns), 4000):
        await update.message.reply_text(raspuns[i:i + 4000])


# 8. Comanda /usage: arată limita abonamentului
async def usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ALLOWED_IDS:
        return
    text = text_limita()
    if not text:
        text = ("Încă n-am primit de la Claude informații despre limită în această pornire a botului.\n"
                "Le vezi oricând pe claude.ai → Settings → Usage.")
    await update.message.reply_text(text.strip(" —\n"))


# 9. Pornim botul
app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("usage", usage))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, raspunde))
print("Botul a pornit. Control+C ca să-l oprești.")
app.run_polling()