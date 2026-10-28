# Agentul SuperPractic pe Telegram
# Research-ul îl face agentul principal (Sonnet); copy-ul și pagina le face agentul „copywriter” (Opus).
# Are unealta Shopify, ține minte conversația până dai /nou, vede pozele și arată ce face pe parcurs
# (pasul curent + cât timp lucrează), iar în terminal scrie fiecare pas și erorile complete.
# Poate încărca în Shopify pozele primite pe Telegram (la produs sau în Content → Files).

# 1. Importuri
import asyncio
import logging
import mimetypes
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
    ClaudeSDKClient,      # conversație care ține minte mesajele
    ClaudeAgentOptions,
    AgentDefinition,
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,      # NOU: rezultatul unei unelte (aici: copywriter-ul a terminat)
    UserMessage,        # NOU: Claude gândește
    ToolUseBlock,         # aflăm ce unealtă folosește Claude
    RateLimitEvent,
    tool,                 # ca să construim unealta Shopify
    create_sdk_mcp_server,
)

# Jurnalul din terminal: oră, nivel, mesaj
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S")
logging.getLogger("httpx").setLevel(logging.WARNING)   # fără zgomot de la cererile Telegram
log = logging.getLogger("onyx")

# 2. Cheile din .env
load_dotenv()
os.environ.pop("ANTHROPIC_API_KEY", None)   # folosim abonamentul Pro, nu API-ul
# NOU: oprim complet „fundalul” – subagenții (copywriter-ul) rulează mereu până la capăt, în același răspuns
os.environ["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_IDS = {int(x) for x in os.environ["TELEGRAM_ALLOWED_CHAT_ID"].split(",")}
FOLDER = Path(__file__).parent
POZE = FOLDER / "uploads"
POZE.mkdir(exist_ok=True)

SHOP = os.environ["SHOPIFY_STORE"]                       # b77w9x-rx.myshopify.com
SHOP_CLIENT_ID = os.environ["SHOPIFY_CLIENT_ID"]
SHOP_CLIENT_SECRET = os.environ["SHOPIFY_CLIENT_SECRET"]
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-10")

MODEL_RESEARCH = os.getenv("MODEL_RESEARCH", "sonnet")
EFFORT_RESEARCH = os.getenv("EFFORT_RESEARCH", "medium")
MODEL_COPY = os.getenv("MODEL_COPY", "claude-opus-5-5")
EFFORT_COPY = os.getenv("EFFORT_COPY", "medium")


# 3. Tokenul Shopify (expiră la 24h, îl cerem din nou singuri)
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


# 4. Unealta Shopify pe care o poate folosi Claude
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


# 4b. NOU: încarcă o poză din folderul uploads în Shopify
async def graphql(query, variables=None):
    token = await ia_token_shopify()
    async with httpx.AsyncClient(timeout=90) as http:
        r = await http.post(
            f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json",
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            json={"query": query, "variables": variables or {}},
        )
    r.raise_for_status()
    return r.json()


def raspuns_unealta(text):
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "incarca_poza",
    "Încarcă în Shopify o poză primită pe Telegram (din folderul uploads). "
    "'cale' = calea fișierului. 'unde' = 'produs' (o adaugă la produsul 'product_id', ex. gid://shopify/Product/123) "
    "sau 'fisiere' (o pune în Content → Files și întoarce referința shopify://shop_images/... pentru secțiunile din pagină). "
    "'alt' = text alternativ, opțional. Nu șterge pozele vechi; pentru asta folosește shopify_graphql (productDeleteMedia).",
    {
        "type": "object",
        "properties": {
            "cale": {"type": "string"},
            "unde": {"type": "string", "enum": ["produs", "fisiere"]},
            "product_id": {"type": "string"},
            "alt": {"type": "string"},
        },
        "required": ["cale", "unde"],
    },
)
async def incarca_poza(args):
    cale = Path(args["cale"]).resolve()
    # siguranță: doar poze din folderul uploads al botului
    if POZE.resolve() not in cale.parents or not cale.is_file():
        return raspuns_unealta(f"Fișierul {cale} nu există sau nu e în folderul uploads.")
    tip = mimetypes.guess_type(cale.name)[0] or "image/jpeg"
    continut = cale.read_bytes()

    # Pasul 1: Shopify ne dă un loc unde să urcăm fișierul
    r = await graphql(
        """mutation($input: [StagedUploadInput!]!) {
             stagedUploadsCreate(input: $input) {
               stagedTargets { url resourceUrl parameters { name value } }
               userErrors { field message }
             }
           }""",
        {"input": [{"resource": "IMAGE", "filename": cale.name, "mimeType": tip,
                    "httpMethod": "POST", "fileSize": str(len(continut))}]},
    )
    date = (r.get("data") or {}).get("stagedUploadsCreate") or {}
    if not date.get("stagedTargets"):
        return raspuns_unealta(f"Shopify n-a dat loc de încărcare: {r}")
    tinta = date["stagedTargets"][0]

    # Pasul 2: urcăm fișierul acolo (parametrii întâi, fișierul la final)
    campuri = {p["name"]: p["value"] for p in tinta["parameters"]}
    async with httpx.AsyncClient(timeout=120) as http:
        up = await http.post(tinta["url"], data=campuri, files={"file": (cale.name, continut, tip)})
    if up.status_code >= 300:
        return raspuns_unealta(f"Încărcarea a eșuat: HTTP {up.status_code} {up.text[:500]}")

    # Pasul 3a: o atașăm la produs
    if args["unde"] == "produs":
        if not args.get("product_id"):
            return raspuns_unealta("Lipsește product_id.")
        r = await graphql(
            """mutation($product: ProductUpdateInput!, $media: [CreateMediaInput!]) {
                 productUpdate(product: $product, media: $media) {
                   product { id media(first: 20) { nodes { id alt mediaContentType status } } }
                   userErrors { field message }
                 }
               }""",
            {"product": {"id": args["product_id"]},
             "media": [{"originalSource": tinta["resourceUrl"], "mediaContentType": "IMAGE",
                        "alt": args.get("alt", "")}]},
        )
        return raspuns_unealta(f"Poza a fost adăugată la produs. Răspuns Shopify: {r}")

    # Pasul 3b: o punem în Content → Files
    r = await graphql(
        """mutation($files: [FileCreateInput!]!) {
             fileCreate(files: $files) {
               files { id fileStatus alt ... on MediaImage { image { url } } }
               userErrors { field message }
             }
           }""",
        {"files": [{"originalSource": tinta["resourceUrl"], "contentType": "IMAGE",
                    "filename": cale.name, "alt": args.get("alt", "")}]},
    )
    return raspuns_unealta(
        f"Poza a fost pusă în Content → Files. În secțiunile paginii o folosești ca "
        f"shopify://shop_images/{cale.name} . Răspuns Shopify: {r}"
    )


server_shopify = create_sdk_mcp_server(name="shopify", version="1.0.0", tools=[shopify_graphql, incarca_poza])
UNEALTA_SHOPIFY = "mcp__shopify__shopify_graphql"
UNEALTA_POZA = "mcp__shopify__incarca_poza"

# 5. Al doilea agent: copywriter-ul (Opus) – are și Shopify
copywriter = AgentDefinition(
    description="Scrie copy-ul paginii de produs și creează pagina în Shopify, după CLAUDE.md. "
                "Folosește-l pentru Pasul 2 și Pasul 3.",
    prompt="Ești copywriter-ul SuperPractic. Scrii copy-ul în formatul fix din CLAUDE.md, "
           "pentru clientul român, fără să inventezi nimic din ce nu e în research-ul primit. "
           "La Pasul 3 creezi produsul și pagina cu unealta Shopify, exact după CLAUDE.md. "
           "Răspunzi în română.",
    tools=["Read", UNEALTA_SHOPIFY, UNEALTA_POZA],
    model=MODEL_COPY,
    effort=EFFORT_COPY,
    background=False,     # NOU: agentul principal îl așteaptă să termine (nu-l lasă în fundal)
)

# 6. Setările agentului principal
optiuni = ClaudeAgentOptions(
    cwd=str(FOLDER),
    setting_sources=["project"],
    mcp_servers={"shopify": server_shopify},
    allowed_tools=["WebSearch", "WebFetch", "Agent", "Read", UNEALTA_SHOPIFY, UNEALTA_POZA],
    disallowed_tools=["Bash", "Edit", "Write", "NotebookEdit", "TodoWrite"],
    agents={"copywriter": copywriter},
    max_turns=40,
    model=MODEL_RESEARCH,
    effort=EFFORT_RESEARCH,
    env={"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"},   # NOU: același lucru, trimis direct lui Claude
)

# 7. Câte o conversație pentru fiecare om din echipă
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


# 9. Transformă unealta folosită într-un text scurt pentru mesajul de stare
def descrie_pasul(bucata):
    nume, date = bucata.name, bucata.input or {}
    if nume == "WebSearch":
        return f"🔍 Caut: {date.get('query', '')}"
    if nume == "WebFetch":
        url = date.get("url", "")
        adresa = url.split("/")[2] if "//" in url else url
        return f"📄 Citesc: {adresa}"
    if nume in ("Agent", "Task"):
        return "✍️ Copywriter-ul scrie…"
    if nume == "Read":
        return "👀 Mă uit la poză…"
    if nume.endswith("incarca_poza"):
        return "🖼 Încarc poza în Shopify…"
    if nume.endswith("shopify_graphql"):
        return "🛒 Lucrez în Shopify…"
    return f"⚙️ {nume}"


# 10. Trimite mesajul în conversație și adună răspunsul
def text_din_rezultat(continut):
    # rezultatul copywriter-ului poate veni ca text sau ca listă de bucăți {"type": "text", "text": ...}
    if isinstance(continut, str):
        return continut
    if isinstance(continut, list):
        return "\n".join(b.get("text", "") for b in continut if isinstance(b, dict))
    return ""


async def intreaba_claude(chat_id, text, progres):
    client = await ia_conversatia(chat_id)
    await client.query(text + "\n\n(Dacă folosești copywriter-ul, așteaptă-i rezultatul în acest răspuns; nu-l lăsa în fundal.)")
    raspuns = await citeste_raspuns(client, chat_id, progres)

    # NOU: plasă de siguranță – dacă răspunsul s-a încheiat, dar copywriter-ul n-a terminat,
    # îl întrebăm singuri pe agent (de max. 3 ori), ca să nu mai fie nevoie să întrebi tu
    incercari = 0
    while progres.get("copy") and not progres["copy"]["gata"] and incercari < 3:
        incercari += 1
        log.info("chat %s · copywriter-ul n-a terminat încă, întreb automat (%s/3)", chat_id, incercari)
        progres["pas"] = "⏳ Aștept copywriter-ul…"
        await asyncio.sleep(20)
        await client.query("Așteaptă până termină copywriter-ul și apoi dă-mi rezultatul lui complet, "
                           "exact cum l-a scris. Nu-l lăsa în fundal.")
        raspuns_nou = await citeste_raspuns(client, chat_id, progres)
        if raspuns_nou:
            raspuns = raspuns_nou

    # dacă agentul principal n-a scris nimic, dar avem rezultatul copywriter-ului, îl trimitem pe acela
    copy = progres.get("copy")
    if not raspuns and copy and copy.get("rezultat"):
        raspuns = copy["rezultat"]
    return raspuns or "(n-am primit răspuns)"


async def citeste_raspuns(client, chat_id, progres):
    raspuns = ""
    async for mesaj in client.receive_response():
        din_copywriter = bool(getattr(mesaj, "parent_tool_use_id", None))

        if isinstance(mesaj, AssistantMessage):
            for bucata in mesaj.content:
                if din_copywriter:
                    # ce face copywriter-ul -> al doilea mesaj de stare
                    copy = progres.get("copy")
                    if copy and not copy["gata"]:
                        if isinstance(bucata, ThinkingBlock):
                            copy["pas"] = "🧠 Gândește…"
                        elif isinstance(bucata, ToolUseBlock):
                            copy["pas"] = descrie_pasul(bucata)
                            log.info("chat %s · copywriter · %s", chat_id, copy["pas"])
                        elif isinstance(bucata, TextBlock):
                            copy["pas"] = "📝 Scrie…"
                    continue
                # ce face agentul principal -> primul mesaj de stare
                if isinstance(bucata, ThinkingBlock):
                    progres["pas"] = "🧠 Gândește…"
                elif isinstance(bucata, ToolUseBlock):
                    progres["pas"] = descrie_pasul(bucata)
                    log.info("chat %s · %s", chat_id, progres["pas"])
                    if bucata.name in ("Agent", "Task"):
                        progres["copy"] = {"id": bucata.id, "start": time.time(),
                                           "pas": "⏳ Pornește…", "gata": False}
                elif isinstance(bucata, TextBlock):
                    progres["pas"] = "📝 Scrie răspunsul…"
                    raspuns += bucata.text + "\n"

        # copywriter-ul a terminat când vine rezultatul uneltei Agent
        if isinstance(mesaj, UserMessage) and isinstance(mesaj.content, list):
            copy = progres.get("copy")
            for bucata in mesaj.content:
                if copy and isinstance(bucata, ToolResultBlock) and bucata.tool_use_id == copy["id"]:
                    if "async_launched" not in str(bucata.content):
                        copy["gata"] = True
                        copy["durata"] = int(time.time() - copy["start"])
                        copy["rezultat"] = text_din_rezultat(bucata.content).strip()
                        log.info("chat %s · copywriter gata în %ss", chat_id, copy["durata"])
                    else:
                        log.warning("chat %s · copywriter-ul a pornit în fundal", chat_id)

        if isinstance(mesaj, RateLimitEvent):
            ultima_limita["info"] = mesaj.rate_limit_info
    return raspuns.strip()


# 11. Cât lucrează: „scrie…” sus + mesajele de stare (agentul principal și copywriter-ul)
def ceas(start):
    secunde = int(time.time() - start)
    return f"{secunde // 60}m {secunde % 60:02d}s"


def text_stare(progres):
    return f"{progres['pas']}\n⏱ {ceas(progres['start'])} · botul merge, nicio eroare"


def text_copywriter(copy):
    if copy["gata"]:
        d = copy["durata"]
        return f"✅ Copywriter-ul (Opus) a terminat în {d // 60}m {d % 60:02d}s"
    return f"✍️ Copywriter-ul (Opus) lucrează\n{copy['pas']}\n⏱ {ceas(copy['start'])} · merge, nu s-a oprit"


async def arata_progres(chat, stare, progres, stop):
    ultimul_text, ultimul_copy = "", ""
    while not stop.is_set():
        try:
            await chat.send_action(ChatAction.TYPING)
            text = text_stare(progres)
            if text != ultimul_text:
                await stare.edit_text(text)
                ultimul_text = text
            copy = progres.get("copy")
            if copy:
                text_c = text_copywriter(copy)
                if "mesaj" not in copy:
                    copy["mesaj"] = await chat.send_message(text_c)
                    ultimul_copy = text_c
                elif text_c != ultimul_copy:
                    await copy["mesaj"].edit_text(text_c)
                    ultimul_copy = text_c
        except Exception:
            pass   # dacă Telegram refuză o editare, încercăm din nou la următorul pas
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
    # la final: mesajul copywriter-ului rămâne cu ✅ (sau îl actualizăm o ultimă dată)
    copy = progres.get("copy")
    if copy and "mesaj" in copy:
        try:
            await copy["mesaj"].edit_text(text_copywriter(copy) if copy["gata"]
                                          else "⚠️ Copywriter-ul nu a trimis rezultatul în acest răspuns.")
        except Exception:
            pass


# 12. Partea comună: trimite un text la Claude și răspunde în Telegram
async def proceseaza(update: Update, text):
    chat_id = update.effective_chat.id
    lacat = lacate.setdefault(chat_id, asyncio.Lock())
    async with lacat:
        progres = {"pas": "⏳ Pornesc…", "start": time.time()}
        stare = await update.message.reply_text(text_stare(progres))
        stop = asyncio.Event()
        urmarire = asyncio.create_task(arata_progres(update.effective_chat, stare, progres, stop))
        log.info("chat %s · mesaj nou: %s", chat_id, text[:80].replace("\n", " "))
        try:
            raspuns = await intreaba_claude(chat_id, text, progres)
            durata = int(time.time() - progres["start"])
            log.info("chat %s · gata în %ss", chat_id, durata)
            eroare_aparuta = False
        except Exception as eroare:
            log.exception("chat %s · EROARE", chat_id)   # scrie în terminal eroarea completă
            await inchide_conversatia(chat_id)
            raspuns = (f"❌ A apărut o eroare: {eroare}\n"
                       "Am pornit o conversație nouă, trimite din nou mesajul. "
                       "Detaliile complete sunt în terminal.")
            eroare_aparuta = True
        finally:
            stop.set()
            await urmarire
        try:
            if eroare_aparuta:
                await stare.edit_text("❌ S-a oprit cu o eroare (detalii mai jos).")
            else:
                await stare.delete()
        except Exception:
            pass
    # NOU: anunț clar, ca mesaj nou (cu notificare), că a terminat copywriter-ul
    copy = progres.get("copy")
    if copy and not eroare_aparuta:
        if copy["gata"]:
            d = copy["durata"]
            await update.message.reply_text(
                f"✅ Copywriter-ul a terminat (în {d // 60}m {d % 60:02d}s). Rezultatul e mai jos 👇")
        else:
            await update.message.reply_text(
                "⚠️ Copywriter-ul nu a terminat nici după 3 verificări. Scrie „gata?” și îl întreb din nou.")
    for i in range(0, len(raspuns), 4000):
        await update.message.reply_text(raspuns[i:i + 4000])


# 13. Mesaj text
async def raspunde(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ALLOWED_IDS:
        return
    await proceseaza(update, update.message.text)


# 14. Poză (trimisă ca poză sau ca fișier)
async def primeste_poza(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ALLOWED_IDS:
        return
    if update.message.photo:
        fisier_tg = update.message.photo[-1]          # cea mai mare rezoluție
        extensie = ".jpg"
    else:
        fisier_tg = update.message.document           # poză trimisă ca fișier
        extensie = Path(fisier_tg.file_name or "poza.jpg").suffix or ".jpg"
    fisier = await fisier_tg.get_file()
    cale = POZE / f"{fisier_tg.file_unique_id}{extensie}"
    await fisier.download_to_drive(cale)
    text_insotitor = update.message.caption or "Uită-te la poză și spune-mi ce produs e."
    prompt = (
        f"Utilizatorul a trimis o poză, salvată la: {cale}\n"
        f"Deschide-o cu unealta Read ca s-o vezi.\n"
        f"Mesajul lui: {text_insotitor}"
    )
    await proceseaza(update, prompt)


# 15. Comenzi
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


# 16. Pornim botul
app = ApplicationBuilder().token(TOKEN).concurrent_updates(True).build()
app.add_handler(CommandHandler("nou", nou))
app.add_handler(CommandHandler("showusage", showusage))
app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, primeste_poza))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, raspunde))
log.info("Botul a pornit. Control+C ca să-l oprești.")
app.run_polling()