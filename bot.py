"""
Refresh by Coco — WhatsApp Chat Summary Bot for Telegram
Paste WhatsApp chat messages OR send a zip file → get structured summary in 6 categories.
"""

import os
import io
import zipfile
import tempfile
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

# --- Config ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = "claude-sonnet-4-20250514"

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


def extract_text_from_zip(zip_bytes: bytes) -> str:
    """Extract .txt file contents from a WhatsApp export zip."""
    text_parts = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        for name in sorted(zf.namelist()):
            if name.endswith(".txt"):
                text_parts.append(zf.read(name).decode("utf-8", errors="replace"))
    return "\n".join(text_parts)


async def call_claude(chat_text: str) -> str:
    """Send chat text to Claude and return the summary."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Tolong ringkas chat WhatsApp berikut:\n\n{chat_text}",
            }
        ],
    )
    return response.content[0].text


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
