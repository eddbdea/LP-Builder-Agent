# Agentul SuperPractic pe Telegram
# Research-ul îl face agentul principal (Sonnet); copy-ul și pagina le face agentul „copywriter” (Opus).
# Are unealta Shopify și ține minte conversația până dai /nou.

# 1. Importuri
import asyncio
import os
import time
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
from claude_agent_sdk import (
    ClaudeSDKClient,      # NOU: conversație care ține minte mesajele
    ClaudeAgentOptions,
    AgentDefinition,
    AssistantMessage,
    TextBlock,
    RateLimitEvent,
    tool,                 # NOU: ca să construim unealta Shopify
    create_sdk_mcp_server,
)

# 2. Cheile din .env
load_dotenv()
os.environ.pop("ANTHROPIC_API_KEY", None)   # folosim abonamentul Pro, nu API-ul
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_IDS = {int(x) for x in os.environ["TELEGRAM_ALLOWED_CHAT_ID"].split(",")}
FOLDER = Path(__file__).parent

SHOP = os.environ["SHOPIFY_STORE"]                       # b77w9x-rx.myshopify.com
SHOP_CLIENT_ID = os.environ["SHOPIFY_CLIENT_ID"]
SHOP_CLIENT_SECRET = os.environ["SHOPIFY_CLIENT_SECRET"]
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-10")

MODEL_RESEARCH = os.getenv("MODEL_RESEARCH", "sonnet")
EFFORT_RESEARCH = os.getenv("EFFORT_RESEARCH", "medium")
MODEL_COPY = os.getenv("MODEL_COPY", "claude-opus-5-5")
EFFORT_COPY = os.getenv("EFFORT_COPY", "medium")


# 3. NOU: tokenul Shopify (expiră la 24h, îl cerem din nou singuri)
token_shopify = {"valoare": "", "expira": 0.0}


async def ia_token_shopify():
    if token_shopify["valoare"] and time.time() < token_shopify["expira"]:
        return token_shopify["valoare"]
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(
            f"https://{SHOP}/admin/oauth/access_token",
            data={
                "grant_type": "client_credentials",
                "client_id": SHOP_CLIENT_ID,
                "client_secret": SHOP_CLIENT_SECRET,
            },
        )
    r.raise_for_status()
    date = r.json()
    token_shopify["valoare"] = date["access_token"]
    token_shopify["expira"] = time.time() + date.get("expires_in", 86399) - 600
    return token_shopify["valoare"]


# 4. NOU: unealta Shopify pe care o poate folosi Claude
@tool(
    "shopify_graphql",
    "Rulează o interogare sau o mutație GraphQL pe Shopify Admin API al magazinului SuperPractic. "
    "Primește 'query' (text GraphQL) și opțional 'variables' (obiect JSON). Întoarce răspunsul JSON.",
    {
        "type": "object",
        "properties": {"query": {"type": "string"}, "variables": {"type": "object"}},
        "required": ["query"],
    },
)
async def shopify_graphql(args):
    token = await ia_token_shopify()
    async with httpx.AsyncClient(timeout=90) as http:
        r = await http.post(
            f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json",
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            json={"query": args["query"], "variables": args.get("variables") or {}},
        )
    return {"content": [{"type": "text", "text": f"HTTP {r.status_code}\n{r.text[:120000]}"}]}


server_shopify = create_sdk_mcp_server(name="shopify", version="1.0.0", tools=[shopify_graphql])
UNEALTA_SHOPIFY = "mcp__shopify__shopify_graphql"

# 5. Al doilea agent: copywriter-ul (Opus) – acum are și Shopify
copywriter = AgentDefinition(
    description="Scrie copy-ul paginii de produs și creează pagina în Shopify, după CLAUDE.md. "
                "Folosește-l pentru Pasul 2 și Pasul 3.",
    prompt="Ești copywriter-ul SuperPractic. Scrii copy-ul în formatul fix din CLAUDE.md, "
           "pentru clientul român, fără să inventezi nimic din ce nu e în research-ul primit. "
           "La Pasul 3 creezi produsul și pagina cu unealta Shopify, exact după CLAUDE.md. "
           "Răspunzi în română.",
    tools=["Read", UNEALTA_SHOPIFY],
    model=MODEL_COPY,
    effort=EFFORT_COPY,
)

# 6. Setările agentului principal
optiuni = ClaudeAgentOptions(
    cwd=str(FOLDER),
    setting_sources=["project"],
    mcp_servers={"shopify": server_shopify},
    allowed_tools=["WebSearch", "WebFetch", "Agent", "Read", UNEALTA_SHOPIFY],
    disallowed_tools=["Bash", "Edit", "Write", "NotebookEdit", "TodoWrite"],
    agents={"copywriter": copywriter},
    max_turns=40,
    model=MODEL_RESEARCH,
    effort=EFFORT_RESEARCH,
)

# 7. NOU: câte o conversație pentru fiecare om din echipă
conversatii = {}   # chat_id -> ClaudeSDKClient
lacate = {}        # chat_id -> asyncio.Lock (un singur mesaj procesat odată)
ultima_limita = {}


async def ia_conversatia(chat_id):
    if chat_id not in conversatii:
        client = ClaudeSDKClient(options=optiuni)
        await client.connect()
        conversatii[chat_id] = client
    return conversatii[chat_id]


async def inchide_conversatia(chat_id):
    client = conversatii.pop(chat_id, None)
    if client:
        await client.disconnect()


# 8. Textul cu limita (pentru /showusage)
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
    return rand


# 9. Trimite mesajul în conversație și adună răspunsul
async def intreaba_claude(chat_id, text):
    client = await ia_conversatia(chat_id)
    await client.query(text)
    raspuns = ""
    async for mesaj in client.receive_response():
        if isinstance(mesaj, AssistantMessage) and not mesaj.parent_tool_use_id:
            for bucata in mesaj.content:
                if isinstance(bucata, TextBlock):
                    raspuns += bucata.text + "\n"
        if isinstance(mesaj, RateLimitEvent):
            ultima_limita["info"] = mesaj.rate_limit_info
    return raspuns.strip() or "(n-am primit răspuns)"


# 10. Arată „scrie…” cât lucrează
async def arata_scrie(chat, stop):
    while not stop.is_set():
        await chat.send_action(ChatAction.TYPING)
        try:
            await asyncio.wait_for(stop.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass


# 11. Ce face botul la fiecare mesaj
async def raspunde(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in ALLOWED_IDS:
        return
    lacat = lacate.setdefault(chat_id, asyncio.Lock())
    async with lacat:
        stop = asyncio.Event()
        scrie = asyncio.create_task(arata_scrie(update.effective_chat, stop))
        try:
            raspuns = await intreaba_claude(chat_id, update.message.text)
        except Exception as eroare:
            await inchide_conversatia(chat_id)
            raspuns = f"A apărut o eroare: {eroare}\nAm pornit o conversație nouă, trimite din nou mesajul."
        finally:
            stop.set()
            await scrie
    for i in range(0, len(raspuns), 4000):
        await update.message.reply_text(raspuns[i:i + 4000])


# 12. Comenzi
async def nou(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ALLOWED_IDS:
        return
    await inchide_conversatia(update.effective_chat.id)
    await update.message.reply_text("Gata, conversație nouă. Trimite produsul sau ce vrei să modific.")


async def showusage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ALLOWED_IDS:
        return
    text = text_limita() or ("Încă n-am primit de la Claude informații despre limită în această pornire a botului.\n"
                             "Le vezi oricând pe claude.ai → Settings → Usage.")
    await update.message.reply_text(text)


# 13. Pornim botul
app = ApplicationBuilder().token(TOKEN).concurrent_updates(True).build()
app.add_handler(CommandHandler("nou", nou))
app.add_handler(CommandHandler("showusage", showusage))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, raspunde))
print("Botul a pornit. Control+C ca să-l oprești.")
app.run_polling()