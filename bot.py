"""
Refresh by Coco — WhatsApp Chat Summary Bot for Telegram
Paste WhatsApp chat messages OR send a zip file → get structured summary in 6 categories.
Supports date filtering, chunking for long chats, and /cancel to stop processing.
"""

import os
import io
import re
import asyncio
import zipfile
import logging
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
import anthropic

# --- Config ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = "claude-sonnet-4-20250514"
MAX_CHARS_PER_CHUNK = 50000

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Conversation states ---
WAITING_FOR_DATE = 1

# --- Month name mapping (Indonesian) ---
MONTH_MAP = {
    "januari": 1, "jan": 1,
    "februari": 2, "feb": 2,
    "maret": 3, "mar": 3,
    "april": 4, "apr": 4,
    "mei": 5,
    "juni": 6, "jun": 6,
    "juli": 7, "jul": 7,
    "agustus": 8, "agu": 8, "ags": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10,
    "november": 11, "nov": 11,
    "desember": 12, "des": 12,
}

# --- System Prompt ---
SYSTEM_PROMPT = """Kamu adalah asisten operasional untuk bisnis "Refresh by Coco" — air kelapa murni segar yang di-repackage ke botol 330ml.

KONTEKS BISNIS:
- Shelf life sangat pendek: 5–6 hari
- Model bisnis: konsinyasi — produk dititip di kulkas merchant
- Rata-rata 5–10 botol per merchant, total ~30 merchant aktif
- Tipe merchant: minimarket, restoran, lapangan badminton (GOR)
- Harga jual: ~Rp 10.000/botol
- Kompetitor: Hydro Coco (Rp 10.000), CocoNico (Rp 15.000)
- Program insentif kasir: jual 5 = Rp 5.000 bonus, jual 10 = Rp 10.000

TIM YANG ADA DI CHAT:
- Dionisius Radita (Radit) — Co-owner, ops lead
- Benedict Anthony (Ben) — Co-owner, business partner
- Mas Risky (nama WhatsApp: "qiw qiw") — Kurir lapangan / sales
- Mas Heri — Kurir / field sales lainnya

TUGAS:
Ketika user mengirimkan copy-paste chat WhatsApp, ringkas ke dalam 6 kategori berikut menggunakan format tabel/poin. Output HARUS dalam Bahasa Indonesia.

6 KATEGORI OUTPUT:

1. RESTOCK MERCHANT
   - Merchant mana yang di-restock, berapa botol, kapan
   - Perhatikan angka botol yang disebutkan
   - "Titip" / "isi" / "taruh" = restock

2. POSM (Poster / Akrilik)
   - Merchant mana yang sudah punya poster dan/atau akrilik standing
   - Merchant mana yang belum / menolak pasang
   - "Tempel" / "pasang" / "akrilik" / "poster" = POSM

3. RETUR / PRODUK EXPIRED
   - Produk yang dikembalikan karena expired atau tidak laku
   - Karena shelf life pendek, retur itu NORMAL dan sering terjadi
   - "Ambil balik" / "tarik" / "expired" / "basi" / "exp" = retur

4. MERCHANT CHURN
   - Merchant yang berhenti atau minta stop bawa produk
   - Sertakan alasan kalau disebutkan
   - "Gamau lagi" / "stop" / "tarik semua" = churn

5. MERCHANT LIBUR / TUTUP SEMENTARA
   - Merchant yang tutup sementara (libur, pulkam, renovasi, dll)
   - Sertakan tanggal buka kembali kalau ada
   - "Libur" / "pulkam" / "tutup dulu" = libur sementara

6. TOPIK / ISU LAINNYA
   - Operasional & logistik (macet, motor rusak, stiker habis, dll)
   - Keuangan (gaji, pembayaran merchant, insentif)
   - Info kompetitor dari lapangan
   - Masalah internal tim
   - Hal lain yang relevan

ATURAN FORMAT:
- Output dalam Bahasa Indonesia
- Gunakan format tabel untuk Restock, POSM, Retur, dan Merchant Libur
- Gunakan poin untuk Churn dan Isu Lainnya
- Kalau suatu kategori tidak ada datanya, tulis "Tidak ada data untuk periode ini"
- Chat yang masuk biasanya sangat kasual dan informal (bahasa gaul Indonesia)
- Perhatikan konteks — "enci" = pemilik toko (Tionghoa), "GOR" = lapangan badminton
- Satuan selalu BOTOL, bukan buah/kg
"""

MERGE_PROMPT = """Kamu menerima beberapa ringkasan parsial dari chat WhatsApp yang sangat panjang (dipecah jadi beberapa bagian).

Tugasmu: GABUNGKAN semua ringkasan parsial menjadi SATU ringkasan final yang lengkap dan rapi.

Aturan:
- Kalau merchant yang sama muncul di beberapa bagian, gabungkan datanya (jangan duplikat)
- Untuk restock, jumlahkan atau list semua tanggal restock
- Untuk POSM, ambil status terbaru
- Untuk retur, gabungkan semua kejadian
- Untuk churn dan libur, pastikan tidak ada duplikat
- Untuk isu lainnya, gabungkan semua poin unik
- Output tetap dalam format 6 kategori yang sama
- Output dalam Bahasa Indonesia
"""


# --- Date parsing ---

def parse_date_range(text: str) -> tuple[datetime | None, datetime | None]:
    """Parse Indonesian date range from user input."""
    text = text.strip().lower()
    now = datetime.now()
    year = now.year

    if text in ("semua", "all", "semuanya"):
        return None, None

    if text in ("minggu ini", "this week"):
        start = now - timedelta(days=now.weekday())
        end = now
        return start.replace(hour=0, minute=0, second=0), end.replace(hour=23, minute=59, second=59)

    if text in ("minggu lalu", "last week"):
        start = now - timedelta(days=now.weekday() + 7)
        end = start + timedelta(days=6)
        return start.replace(hour=0, minute=0, second=0), end.replace(hour=23, minute=59, second=59)

    if text in ("bulan ini", "this month"):
        start = now.replace(day=1, hour=0, minute=0, second=0)
        end = now.replace(hour=23, minute=59, second=59)
        return start, end

    if text in ("bulan lalu", "last month"):
        first_this_month = now.replace(day=1)
        end = first_this_month - timedelta(days=1)
        start = end.replace(day=1, hour=0, minute=0, second=0)
        return start, end.replace(hour=23, minute=59, second=59)

    # "D-D month" (e.g., "1-15 maret")
    m = re.match(r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s+(\w+)(?:\s+(\d{4}))?", text)
    if m:
        day1, day2, month_str, year_str = m.groups()
        month = MONTH_MAP.get(month_str)
        if month:
            y = int(year_str) if year_str else year
            try:
                start = datetime(y, month, int(day1), 0, 0, 0)
                end = datetime(y, month, int(day2), 23, 59, 59)
                return start, end
            except ValueError:
                pass

    # "D month - D month"
    m = re.match(
        r"(\d{1,2})\s+(\w+)\s*[-–]|sampai|sampe|s/d\s*(\d{1,2})\s+(\w+)(?:\s+(\d{4}))?",
        text,
    )
    if m:
        groups = m.groups()
        day1, month1_str, day2, month2_str = groups[0], groups[1], groups[2], groups[3]
        year_str = groups[4] if len(groups) > 4 else None
        m1 = MONTH_MAP.get(month1_str)
        m2 = MONTH_MAP.get(month2_str)
        if m1 and m2 and day1 and day2:
            y = int(year_str) if year_str else year
            try:
                start = datetime(y, m1, int(day1), 0, 0, 0)
                end = datetime(y, m2, int(day2), 23, 59, 59)
                return start, end
            except ValueError:
                pass

    return None, None


def filter_chat_by_date(chat_text: str, start_date: datetime, end_date: datetime) -> str:
    """Filter WhatsApp chat lines to only include messages within the date range."""
    lines = chat_text.split("\n")
    filtered = []
    include_line = False

    date_patterns = [
        re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4}),?\s"),
        re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}),?\s"),
        re.compile(r"^\[(\d{1,2})/(\d{1,2})/(\d{4})"),
        re.compile(r"^\[(\d{1,2})/(\d{1,2})/(\d{2})"),
    ]

    for line in lines:
        matched = False
        for pattern in date_patterns:
            m = pattern.match(line)
            if m:
                day, month, year_str = int(m.group(1)), int(m.group(2)), m.group(3)
                yr = int(year_str)
                if yr < 100:
                    yr += 2000
                try:
                    line_date = datetime(yr, month, day)
                    include_line = start_date <= line_date <= end_date
                except ValueError:
                    include_line = False
                matched = True
                break

        if not matched:
            pass

        if include_line:
            filtered.append(line)

    return "\n".join(filtered)


# --- Text chunking ---

def split_chat_into_chunks(text: str, max_chars: int = MAX_CHARS_PER_CHUNK) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    chunks = []
    lines = text.split("\n")
    current_chunk = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len > max_chars and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = []
            current_len = 0
        current_chunk.append(line)
        current_len += line_len

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks


# --- Claude API (with cancellation support) ---

class CancelledError(Exception):
    """Raised when user cancels processing."""
    pass


async def call_claude(chat_text: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Send chat text to Claude. Checks context.user_data['cancel'] between chunks."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    chunks = split_chat_into_chunks(chat_text)

    def _check_cancelled():
        if context.user_data.get("cancel"):
            raise CancelledError("Proses dibatalkan oleh user.")

    if len(chunks) == 1:
        _check_cancelled()
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Tolong ringkas chat WhatsApp berikut:\n\n{chunks[0]}",
                }
            ],
        )
        return response.content[0].text

    logger.info(f"Chat too long ({len(chat_text)} chars), splitting into {len(chunks)} chunks")
    partial_summaries = []

    for i, chunk in enumerate(chunks):
        _check_cancelled()
        if i > 0:
            logger.info(f"Waiting 65 seconds to avoid rate limit...")
            # Use asyncio.sleep so the bot can still receive /cancel during the wait
            for _ in range(65):
                _check_cancelled()
                await asyncio.sleep(1)
        logger.info(f"Processing chunk {i+1}/{len(chunks)} ({len(chunk)} chars)")
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Tolong ringkas chat WhatsApp berikut (bagian {i+1} dari {len(chunks)}):\n\n{chunk}",
                }
            ],
        )
        partial_summaries.append(f"=== RINGKASAN BAGIAN {i+1} ===\n{response.content[0].text}")

    # Merge
    _check_cancelled()
    all_summaries = "\n\n".join(partial_summaries)
    logger.info(f"Waiting 65 seconds before merge step...")
    for _ in range(65):
        _check_cancelled()
        await asyncio.sleep(1)
    logger.info(f"Merging {len(chunks)} partial summaries")

    merge_response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8192,
        system=MERGE_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Gabungkan ringkasan-ringkasan parsial berikut menjadi satu ringkasan final:\n\n{all_summaries}",
            }
        ],
    )
    return merge_response.content[0].text


async def send_long_message(update: Update, text: str):
    if len(text) <= 4096:
        await update.message.reply_text(text)
    else:
        chunks = [text[i : i + 4096] for i in range(0, len(text), 4096)]
        for chunk in chunks:
            await update.message.reply_text(chunk)


# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Halo! Aku bot ringkasan WhatsApp untuk Refresh by Coco 🥥\n\n"
        "Cara pakai:\n"
        "1. Kirim file zip / txt dari WhatsApp export\n"
        "2. Aku akan tanya range tanggal yang mau diringkas\n"
        "3. Ketik tanggalnya (misal: 1-15 maret)\n"
        "4. Tunggu sebentar, ringkasan muncul!\n\n"
        "Atau paste chat langsung sebagai text.\n\n"
        "Kirim /cancel untuk membatalkan proses.\n"
        "Kirim /help untuk bantuan."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 Perintah:\n"
        "/start — Mulai bot\n"
        "/help — Bantuan\n"
        "/cancel — Batalkan proses yang sedang berjalan\n\n"
        "📎 Kirim file zip/txt → bot tanya tanggal → ringkasan\n"
        "📝 Atau paste chat langsung sebagai text\n\n"
        "Format tanggal yang didukung:\n"
        "• 1-15 maret\n"
        "• 1 maret - 15 maret\n"
        "• minggu ini\n"
        "• minggu lalu\n"
        "• bulan ini\n"
        "• bulan lalu\n"
        "• semua (tanpa filter)\n\n"
        "6 kategori ringkasan:\n"
        "1. Restock Merchant\n"
        "2. POSM (Poster/Akrilik)\n"
        "3. Retur / Produk Expired\n"
        "4. Merchant Churn\n"
        "5. Merchant Libur\n"
        "6. Topik / Isu Lainnya"
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Global /cancel — works anytime, sets cancel flag for running processes."""
    context.user_data["cancel"] = True
    context.user_data.pop("chat_text", None)
    await update.message.reply_text("⛔ Proses dibatalkan. Kirim file baru kapanpun.")
    return ConversationHandler.END


async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: Receive file, store it, ask for date range."""
    doc = update.message.document
    file_name = doc.file_name or ""

    if not (file_name.endswith(".zip") or file_name.endswith(".txt")):
        await update.message.reply_text(
            "Format file tidak didukung. Kirim file .zip atau .txt dari WhatsApp export ya!"
        )
        return ConversationHandler.END

    # Reset cancel flag for new process
    context.user_data["cancel"] = False

    await update.message.chat.send_action("typing")

    try:
        tg_file = await doc.get_file()
        file_bytes = await tg_file.download_as_bytearray()

        if file_name.endswith(".zip"):
            chat_text = extract_text_from_zip(bytes(file_bytes))
        else:
            chat_text = bytes(file_bytes).decode("utf-8", errors="replace")

        if len(chat_text.strip()) < 50:
            await update.message.reply_text("File-nya kosong atau terlalu pendek.")
            return ConversationHandler.END

        context.user_data["chat_text"] = chat_text
        logger.info(f"Stored {len(chat_text)} chars from {file_name}")

        await update.message.reply_text(
            f"📂 File diterima! ({len(chat_text):,} karakter)\n\n"
            "Mau ringkasan untuk tanggal berapa?\n\n"
            "Contoh:\n"
            "• 1-15 maret\n"
            "• minggu ini\n"
            "• bulan lalu\n"
            "• semua (tanpa filter tanggal)\n\n"
            "Ketik /cancel untuk membatalkan."
        )
        return WAITING_FOR_DATE

    except zipfile.BadZipFile:
        await update.message.reply_text("File zip-nya rusak. Coba export ulang dari WhatsApp.")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error reading file: {e}")
        await update.message.reply_text(f"Error membaca file: {str(e)[:200]}")
        return ConversationHandler.END


def extract_text_from_zip(zip_bytes: bytes) -> str:
    text_parts = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        for name in sorted(zf.namelist()):
            if name.endswith(".txt"):
                text_parts.append(zf.read(name).decode("utf-8", errors="replace"))
    return "\n".join(text_parts)


async def receive_date_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: Receive date range, filter chat, summarize."""
    date_input = update.message.text
    chat_text = context.user_data.get("chat_text", "")

    if not chat_text:
        await update.message.reply_text("Tidak ada file yang tersimpan. Kirim ulang file-nya ya.")
        return ConversationHandler.END

    # Reset cancel flag
    context.user_data["cancel"] = False

    start_date, end_date = parse_date_range(date_input)

    if start_date and end_date:
        filtered_text = filter_chat_by_date(chat_text, start_date, end_date)
        date_label = f"{start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}"

        if len(filtered_text.strip()) < 50:
            await update.message.reply_text(
                f"Tidak ada pesan ditemukan untuk periode {date_label}.\n"
                "Coba range tanggal lain, atau ketik 'semua' untuk proses tanpa filter."
            )
            return WAITING_FOR_DATE

        await update.message.reply_text(
            f"📅 Filter: {date_label}\n"
            f"📊 {len(filtered_text):,} karakter (dari {len(chat_text):,} total)\n"
            "⏳ Memproses ringkasan... (kirim /cancel untuk membatalkan)"
        )
        process_text = filtered_text
    else:
        await update.message.reply_text(
            f"📊 Memproses semua chat ({len(chat_text):,} karakter)...\n"
            "⏳ Ini mungkin memakan waktu lebih lama. (kirim /cancel untuk membatalkan)"
        )
        process_text = chat_text

    num_chunks = len(split_chat_into_chunks(process_text))
    if num_chunks > 1:
        est_minutes = int(num_chunks * 1.5)
        await update.message.reply_text(
            f"💬 Dipecah jadi {num_chunks} bagian. Estimasi: ~{est_minutes} menit."
        )

    await update.message.chat.send_action("typing")

    try:
        summary = await call_claude(process_text, context)
        await send_long_message(update, summary)
    except CancelledError:
        await update.message.reply_text("⛔ Proses dibatalkan. Kirim file baru kapanpun.")
    except Exception as e:
        logger.error(f"Error calling Claude: {e}")
        await update.message.reply_text(f"Maaf, ada error: {str(e)[:200]}")

    context.user_data.pop("chat_text", None)
    context.user_data["cancel"] = False
    return ConversationHandler.END


async def summarize_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain text paste (no file, no date filter needed)."""
    chat_text = update.message.text

    if len(chat_text) < 50:
        await update.message.reply_text(
            "Pesannya terlalu pendek — paste chat WhatsApp yang lebih panjang ya!"
        )
        return

    context.user_data["cancel"] = False
    await update.message.chat.send_action("typing")

    try:
        summary = await call_claude(chat_text, context)
        await send_long_message(update, summary)
    except CancelledError:
        await update.message.reply_text("⛔ Proses dibatalkan.")
    except Exception as e:
        logger.error(f"Error calling Claude API: {e}")
        await update.message.reply_text(f"Maaf, ada error: {str(e)[:200]}")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Global /cancel handler — highest priority
    app.add_handler(CommandHandler("cancel", cancel_command), group=-1)

    # Conversation handler for file + date filter flow
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Document.ALL, receive_document)],
        states={
            WAITING_FOR_DATE: [
                CommandHandler("cancel", cancel_command),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_date_range),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, summarize_text))

    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
