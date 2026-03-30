"""
Refresh by Coco — WhatsApp Chat Summary Bot for Telegram
Paste WhatsApp chat messages OR send a zip file → get structured summary in 6 categories.
Supports chunking for very long chats (>150k chars) to stay within Claude's token limit.
"""

import os
import io
import time
import zipfile
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

# --- Config ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = "claude-sonnet-4-20250514"
MAX_CHARS_PER_CHUNK = 50000  # ~50k chars ≈ ~15k tokens, safe under 30k token/min rate limit

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- System Prompt (all business context baked in) ---
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


def extract_text_from_zip(zip_bytes: bytes) -> str:
    """Extract .txt file contents from a WhatsApp export zip."""
    text_parts = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        for name in sorted(zf.namelist()):
            if name.endswith(".txt"):
                text_parts.append(zf.read(name).decode("utf-8", errors="replace"))
    return "\n".join(text_parts)


def split_chat_into_chunks(text: str, max_chars: int = MAX_CHARS_PER_CHUNK) -> list[str]:
    """Split long chat text into chunks, breaking at newlines to avoid cutting mid-message."""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    lines = text.split("\n")
    current_chunk = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1  # +1 for newline
        if current_len + line_len > max_chars and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = []
            current_len = 0
        current_chunk.append(line)
        current_len += line_len

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks


async def call_claude(chat_text: str) -> str:
    """Send chat text to Claude and return the summary. Handles chunking for long chats."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    chunks = split_chat_into_chunks(chat_text)

    if len(chunks) == 1:
        # Short enough — single call
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

    # Long chat — summarize each chunk, then merge
    logger.info(f"Chat too long ({len(chat_text)} chars), splitting into {len(chunks)} chunks")
    partial_summaries = []

    for i, chunk in enumerate(chunks):
        # Wait between chunks to avoid rate limit (30k tokens/min on free tier)
        if i > 0:
            logger.info(f"Waiting 65 seconds to avoid rate limit...")
            time.sleep(65)
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

    # Merge all partial summaries into one — wait for rate limit first
    all_summaries = "\n\n".join(partial_summaries)
    logger.info(f"Waiting 65 seconds before merge step...")
    time.sleep(65)
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
    """Send a message, splitting into chunks if over Telegram's 4096 char limit."""
    if len(text) <= 4096:
        await update.message.reply_text(text)
    else:
        chunks = [text[i : i + 4096] for i in range(0, len(text), 4096)]
        for chunk in chunks:
            await update.message.reply_text(chunk)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Halo! Aku bot ringkasan WhatsApp untuk Refresh by Coco 🥥\n\n"
        "Cara pakai:\n"
        "1. Copy-paste chat WhatsApp langsung ke sini, ATAU\n"
        "2. Kirim file zip dari WhatsApp export\n"
        "3. Tunggu sebentar, ringkasan akan muncul\n\n"
        "Format chat yang didukung:\n"
        "- [09:15] Nama: pesan...\n"
        "- 09:15 - Nama: pesan...\n"
        "- Atau format export WhatsApp lainnya\n\n"
        "Kirim /help untuk bantuan."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 Perintah yang tersedia:\n"
        "/start — Mulai bot\n"
        "/help — Tampilkan bantuan ini\n\n"
        "Kirim chat WhatsApp (paste text atau zip file), dan aku akan meringkasnya ke 6 kategori:\n"
        "1. Restock Merchant\n"
        "2. POSM (Poster/Akrilik)\n"
        "3. Retur / Produk Expired\n"
        "4. Merchant Churn\n"
        "5. Merchant Libur\n"
        "6. Topik / Isu Lainnya"
    )


async def summarize_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain text messages."""
    chat_text = update.message.text

    if len(chat_text) < 50:
        await update.message.reply_text(
            "Pesannya terlalu pendek — paste chat WhatsApp yang lebih panjang ya!"
        )
        return

    await update.message.chat.send_action("typing")

    try:
        summary = await call_claude(chat_text)
        await send_long_message(update, summary)
    except Exception as e:
        logger.error(f"Error calling Claude API: {e}")
        await update.message.reply_text(
            "Maaf, ada error saat memproses. Coba lagi ya!\n"
            f"Error: {str(e)[:200]}"
        )


async def summarize_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle uploaded files (zip or txt)."""
    doc = update.message.document
    file_name = doc.file_name or ""

    # Only accept zip and txt files
    if not (file_name.endswith(".zip") or file_name.endswith(".txt")):
        await update.message.reply_text(
            "Format file tidak didukung. Kirim file .zip (WhatsApp export) atau .txt ya!"
        )
        return

    await update.message.chat.send_action("typing")
    await update.message.reply_text("📂 Memproses file... tunggu sebentar ya.")

    try:
        # Download the file
        tg_file = await doc.get_file()
        file_bytes = await tg_file.download_as_bytearray()

        # Extract text
        if file_name.endswith(".zip"):
            chat_text = extract_text_from_zip(bytes(file_bytes))
        else:
            chat_text = bytes(file_bytes).decode("utf-8", errors="replace")

        if len(chat_text.strip()) < 50:
            await update.message.reply_text(
                "File-nya kosong atau terlalu pendek — pastikan ini file export WhatsApp yang benar."
            )
            return

        num_chunks = len(split_chat_into_chunks(chat_text))
        if num_chunks > 1:
            est_minutes = num_chunks * 1.5  # ~1.5 min per chunk (65s wait + processing)
            await update.message.reply_text(
                f"💬 Chat sangat panjang ({len(chat_text):,} karakter), dipecah jadi {num_chunks} bagian. "
                f"Estimasi waktu: ~{int(est_minutes)} menit. Sabar ya!"
            )

        logger.info(f"Extracted {len(chat_text)} chars from {file_name}")

        # Summarize
        await update.message.chat.send_action("typing")
        summary = await call_claude(chat_text)
        await send_long_message(update, summary)

    except zipfile.BadZipFile:
        await update.message.reply_text(
            "File zip-nya rusak atau bukan format yang valid. Coba export ulang dari WhatsApp."
        )
    except Exception as e:
        logger.error(f"Error processing file: {e}")
        await update.message.reply_text(
            "Maaf, ada error saat memproses file. Coba lagi ya!\n"
            f"Error: {str(e)[:200]}"
        )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, summarize_text))
    app.add_handler(MessageHandler(filters.Document.ALL, summarize_document))

    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
