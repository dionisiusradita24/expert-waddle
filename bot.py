"""
Refresh by Coco — WhatsApp Chat Summary Bot for Telegram
Supports two modes: RESTOCK (daily ops) and BD (business development).
Includes date filtering, chunking, /cancel support.
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
WAITING_FOR_CONTEXT = 1
WAITING_FOR_DATE = 2

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

# =====================================================================
# SYSTEM PROMPTS
# =====================================================================

RESTOCK_PROMPT = """Kamu adalah asisten operasional untuk bisnis "Refresh by Coco" — air kelapa murni segar yang di-repackage ke botol 330ml.

KONTEKS BISNIS:
- Shelf life sangat pendek: 5–6 hari
- Model bisnis: konsinyasi — produk dititip di kulkas merchant
- Rata-rata 5–10 botol per merchant, total ~30 merchant aktif
- Tipe merchant: minimarket, restoran, lapangan badminton (GOR)
- Harga jual: ~Rp 10.000/botol
- Kompetitor: Hydro Coco (Rp 10.000), CocoNico (Rp 15.000)
- Program insentif kasir: jual 5 = Rp 5.000 bonus, jual 10 = Rp 10.000
- Beberapa toko bayar langsung (cash saat restock), beberapa bayar bulanan

TIM YANG ADA DI CHAT:
- Dionisius Radita (Radit) — Co-owner, ops lead
- Benedict Anthony (Ben) — Co-owner, business partner
- Mas Risky (nama WhatsApp: "qiw qiw") — Kurir lapangan / sales
- Mas Heri — Kurir / field sales lainnya
- Nama lain yang muncul = kemungkinan karyawan baru, catat saja

TUGAS:
Ringkas chat WhatsApp ke dalam 6 kategori berikut. Output HARUS dalam Bahasa Indonesia.
Karena Telegram tidak support tabel HTML, gunakan format monospace block (```) untuk tabel supaya kolom sejajar.

6 KATEGORI OUTPUT:

1. RESTOCK MERCHANT
   Format kolom: Nama Toko | Tgl | Jumlah | Status Bayar | Keterangan
   - Merchant mana yang di-restock, berapa botol, kapan
   - "Titip" / "isi" / "taruh" = restock
   - Status bayar: "Lunas [jumlah]" / "Bayar bulanan" / "Belum bayar" / "Tidak disebutkan"
   - "Bayar" / "udah bayar" / "cash" / "transfer" = lunas. Catat jumlahnya kalau disebutkan.
   - "Nanti aja" / "akhir bulan" / "bulanan" = bayar bulanan
   - Satuan selalu BOTOL

2. POSM (Poster / Akrilik)
   Format kolom: Nama Toko | Poster | Akrilik | Info dari Karyawan | Alasan Tidak Pasang
   - Poster: "Ada" / "Tidak ada" / "Tidak disebutkan"
   - Akrilik: "Ada" / "Tidak ada" / "Tidak disebutkan"
   - Info dari karyawan: "Ya" / "Tidak" — apakah karyawan/kurir melaporkan status POSM
   - Alasan tidak pasang: isi alasannya kalau disebutkan (misal "gamau", "tidak ada tempat", "belum sempat"), atau "-" kalau sudah ada atau tidak disebutkan
   - "Tempel" / "pasang" / "poster" / "akrilik" = POSM

3. RETUR / PRODUK EXPIRED
   Format kolom: Nama Toko | Tgl | Jumlah | Keterangan
   - Karena shelf life pendek, retur itu NORMAL dan sering terjadi
   - "Ambil balik" / "tarik" / "expired" / "basi" / "exp" = retur

4. MERCHANT CHURN
   - Merchant yang berhenti atau minta stop bawa produk
   - Sertakan alasan kalau disebutkan
   - "Gamau lagi" / "stop" / "tarik semua" = churn

5. MERCHANT LIBUR / TUTUP SEMENTARA
   Format kolom: Nama Toko | Mulai Libur | Buka Kembali | Keterangan
   - "Libur" / "pulkam" / "tutup dulu" = libur sementara

6. TOPIK / ISU LAINNYA
   - Operasional & logistik (macet, motor rusak, stiker habis, dll)
   - Keuangan (gaji, pembayaran merchant, insentif)
   - Info kompetitor dari lapangan
   - Masalah internal tim
   - Hal lain yang relevan

ATURAN FORMAT:
- Output dalam Bahasa Indonesia
- Gunakan monospace block (```) untuk semua tabel supaya kolom sejajar di Telegram
- Gunakan poin (-) untuk Churn dan Isu Lainnya
- Kalau suatu kategori tidak ada datanya, tulis "Tidak ada data untuk periode ini"
- Chat sangat kasual dan informal (bahasa gaul Indonesia)
- "enci" = pemilik toko (Tionghoa), "GOR" = lapangan badminton
"""

BD_PROMPT = """Kamu adalah asisten business development untuk bisnis "Refresh by Coco" — air kelapa murni segar yang di-repackage ke botol 330ml.

KONTEKS BISNIS:
- Air kelapa murni 330ml, shelf life 5–6 hari
- Model konsinyasi — produk dititip di kulkas merchant
- Harga jual: ~Rp 10.000/botol
- Target merchant baru: minimarket, restoran, lapangan badminton (GOR), kafe, dll

TIM YANG ADA DI CHAT:
- Dionisius Radita (Radit) — Co-owner, ops lead
- Benedict Anthony (Ben) — Co-owner, business partner
- BD Sales — karyawan BD (bisa lebih dari satu, catat nama yang muncul)
- Nama lain yang muncul = kemungkinan karyawan baru atau kontak toko, catat saja

TUGAS:
Ringkas chat WhatsApp dari grup BD ke dalam kategori berikut. Output HARUS dalam Bahasa Indonesia.
Karena Telegram tidak support tabel HTML, gunakan format monospace block (```) untuk tabel.

KATEGORI OUTPUT:

1. TOKO YANG DIKUNJUNGI (prospek)
   Format kolom: Nama Toko | Tgl | Sampel | Status | Next Step | Stok Masuk | No HP | Keterangan
   - Sampel: berapa botol sampel yang diberikan ("kasih sampel" / "coba" / "tester")
   - Status: "OK jadi merchant" / "Pending" / "Reject" / "Follow up"
   - Next step (kalau reject/pending): apa yang perlu dilakukan selanjutnya
   - Stok masuk (kalau OK): berapa botol pertama yang dititip
   - No HP: nomor telepon toko/pemilik kalau disebutkan
   - Keterangan: info tambahan (lokasi, tipe toko, nama pemilik, alasan reject, dll)

2. TOKO REJECT
   - Toko yang menolak jadi merchant
   - Sertakan alasan reject kalau disebutkan
   - "Gamau" / "ga tertarik" / "udah ada supplier" / "reject" = reject

3. FOLLOW UP
   - Toko yang perlu di-follow up / dikunjungi lagi
   - Tanggal follow up kalau disebutkan
   - Status terakhir (misal: "sudah kasih sampel, tunggu feedback")

4. TOPIK / ISU LAINNYA
   - Masalah di lapangan (area susah dijangkau, parkir, dll)
   - Insight pasar (kompetitor, harga pasaran, permintaan)
   - Strategi atau arahan dari owner
   - Hal lain yang relevan

ATURAN FORMAT:
- Output dalam Bahasa Indonesia
- Gunakan monospace block (```) untuk tabel
- Gunakan poin (-) untuk Reject, Follow Up, dan Isu Lainnya
- Kalau suatu kategori tidak ada datanya, tulis "Tidak ada data untuk periode ini"
- Chat sangat kasual dan informal (bahasa gaul Indonesia)
- "enci" = pemilik toko (Tionghoa), "GOR" = lapangan badminton
"""

MERGE_RESTOCK_PROMPT = """Kamu menerima beberapa ringkasan parsial dari chat WhatsApp operasional (restock) yang dipecah jadi beberapa bagian.

Tugasmu: GABUNGKAN semua ringkasan parsial menjadi SATU ringkasan final yang lengkap dan rapi.

Aturan:
- Kalau merchant yang sama muncul di beberapa bagian, gabungkan datanya (jangan duplikat)
- Untuk restock, gabungkan semua tanggal dan jumlah. Status bayar ambil yang terbaru.
- Untuk POSM, ambil status terbaru
- Untuk retur, gabungkan semua kejadian
- Untuk churn dan libur, pastikan tidak ada duplikat
- Untuk isu lainnya, gabungkan semua poin unik
- Output tetap dalam format 6 kategori yang sama
- Gunakan monospace block (```) untuk tabel
- Output dalam Bahasa Indonesia
"""

MERGE_BD_PROMPT = """Kamu menerima beberapa ringkasan parsial dari chat WhatsApp BD (business development) yang dipecah jadi beberapa bagian.

Tugasmu: GABUNGKAN semua ringkasan parsial menjadi SATU ringkasan final yang lengkap dan rapi.

Aturan:
- Kalau toko yang sama muncul di beberapa bagian, gabungkan datanya (jangan duplikat)
- Ambil status terbaru untuk setiap toko
- Untuk reject dan follow up, pastikan tidak ada duplikat
- Untuk isu lainnya, gabungkan semua poin unik
- Output tetap dalam format kategori BD yang sama
- Gunakan monospace block (```) untuk tabel
- Output dalam Bahasa Indonesia
"""


# =====================================================================
# DATE PARSING & FILTERING
# =====================================================================

def parse_date_range(text: str) -> tuple[datetime | None, datetime | None]:
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

    m = re.match(r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s+(\w+)(?:\s+(\d{4}))?", text)
    if m:
        day1, day2, month_str, year_str = m.groups()
        month = MONTH_MAP.get(month_str)
        if month:
            y = int(year_str) if year_str else year
            try:
                return datetime(y, month, int(day1), 0, 0, 0), datetime(y, month, int(day2), 23, 59, 59)
            except ValueError:
                pass

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
                return datetime(y, m1, int(day1), 0, 0, 0), datetime(y, m2, int(day2), 23, 59, 59)
            except ValueError:
                pass

    return None, None


def filter_chat_by_date(chat_text: str, start_date: datetime, end_date: datetime) -> str:
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

        if include_line:
            filtered.append(line)

    return "\n".join(filtered)


# =====================================================================
# TEXT CHUNKING & CLAUDE API
# =====================================================================

def extract_text_from_zip(zip_bytes: bytes) -> str:
    text_parts = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        for name in sorted(zf.namelist()):
            if name.endswith(".txt"):
                text_parts.append(zf.read(name).decode("utf-8", errors="replace"))
    return "\n".join(text_parts)


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


class CancelledError(Exception):
    pass


async def call_claude(chat_text: str, context: ContextTypes.DEFAULT_TYPE, mode: str = "restock") -> str:
    """Send chat text to Claude with the appropriate prompt based on mode."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    chunks = split_chat_into_chunks(chat_text)

    system_prompt = RESTOCK_PROMPT if mode == "restock" else BD_PROMPT
    merge_prompt = MERGE_RESTOCK_PROMPT if mode == "restock" else MERGE_BD_PROMPT

    def _check_cancelled():
        if context.user_data.get("cancel"):
            raise CancelledError("Proses dibatalkan oleh user.")

    if len(chunks) == 1:
        _check_cancelled()
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=system_prompt,
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
            for _ in range(65):
                _check_cancelled()
                await asyncio.sleep(1)
        logger.info(f"Processing chunk {i+1}/{len(chunks)} ({len(chunk)} chars)")
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": f"Tolong ringkas chat WhatsApp berikut (bagian {i+1} dari {len(chunks)}):\n\n{chunk}",
                }
            ],
        )
        partial_summaries.append(f"=== RINGKASAN BAGIAN {i+1} ===\n{response.content[0].text}")

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
        system=merge_prompt,
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


# =====================================================================
# HANDLERS
# =====================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Halo! Aku bot ringkasan WhatsApp untuk Refresh by Coco 🥥\n\n"
        "Cara pakai:\n"
        "1. Kirim file zip / txt dari WhatsApp export\n"
        "2. Pilih konteks: Restock atau BD\n"
        "3. Masukkan range tanggal\n"
        "4. Tunggu sebentar, ringkasan muncul!\n\n"
        "Atau paste chat langsung sebagai text.\n\n"
        "/cancel — Batalkan proses\n"
        "/help — Bantuan lengkap"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 Perintah:\n"
        "/start — Mulai bot\n"
        "/help — Bantuan\n"
        "/cancel — Batalkan proses\n\n"
        "📎 Kirim file zip/txt → pilih konteks → tanggal → ringkasan\n"
        "📝 Atau paste chat langsung (default: mode Restock)\n\n"
        "2 mode tersedia:\n"
        "• Restock — ringkasan operasional harian (6 kategori)\n"
        "• BD — ringkasan business development (4 kategori)\n\n"
        "Format tanggal:\n"
        "• 1-15 maret\n"
        "• minggu ini / minggu lalu\n"
        "• bulan ini / bulan lalu\n"
        "• semua (tanpa filter)"
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cancel"] = True
    context.user_data.pop("chat_text", None)
    context.user_data.pop("mode", None)
    await update.message.reply_text("⛔ Proses dibatalkan. Kirim file baru kapanpun.")
    return ConversationHandler.END


async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: Receive file, ask for context (Restock or BD)."""
    doc = update.message.document
    file_name = doc.file_name or ""

    if not (file_name.endswith(".zip") or file_name.endswith(".txt")):
        await update.message.reply_text(
            "Format file tidak didukung. Kirim file .zip atau .txt ya!"
        )
        return ConversationHandler.END

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
            "Ini chat untuk konteks apa?\n\n"
            "1️⃣ Ketik restock — Ringkasan operasional (restock, POSM, retur, dll)\n"
            "2️⃣ Ketik bd — Ringkasan business development (prospek toko baru, reject, dll)\n\n"
            "/cancel untuk membatalkan"
        )
        return WAITING_FOR_CONTEXT

    except zipfile.BadZipFile:
        await update.message.reply_text("File zip-nya rusak. Coba export ulang dari WhatsApp.")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error reading file: {e}")
        await update.message.reply_text(f"Error membaca file: {str(e)[:200]}")
        return ConversationHandler.END


async def receive_context(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: Receive context selection (restock or bd), ask for date range."""
    text = update.message.text.strip().lower()

    if text in ("restock", "1", "restok"):
        context.user_data["mode"] = "restock"
        mode_label = "Restock (operasional)"
    elif text in ("bd", "2", "bisdev", "business development"):
        context.user_data["mode"] = "bd"
        mode_label = "BD (business development)"
    else:
        await update.message.reply_text(
            "Tidak dikenali. Ketik restock atau bd ya.\n\n"
            "1️⃣ restock — Ringkasan operasional\n"
            "2️⃣ bd — Ringkasan business development"
        )
        return WAITING_FOR_CONTEXT

    await update.message.reply_text(
        f"✅ Mode: {mode_label}\n\n"
        "Mau ringkasan untuk tanggal berapa?\n\n"
        "Contoh:\n"
        "• 1-15 maret\n"
        "• minggu ini\n"
        "• bulan lalu\n"
        "• semua (tanpa filter tanggal)"
    )
    return WAITING_FOR_DATE


async def receive_date_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 3: Receive date range, filter chat, summarize."""
    date_input = update.message.text
    chat_text = context.user_data.get("chat_text", "")
    mode = context.user_data.get("mode", "restock")

    if not chat_text:
        await update.message.reply_text("Tidak ada file yang tersimpan. Kirim ulang file-nya ya.")
        return ConversationHandler.END

    context.user_data["cancel"] = False
    start_date, end_date = parse_date_range(date_input)

    if start_date and end_date:
        filtered_text = filter_chat_by_date(chat_text, start_date, end_date)
        date_label = f"{start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}"

        if len(filtered_text.strip()) < 50:
            await update.message.reply_text(
                f"Tidak ada pesan ditemukan untuk periode {date_label}.\n"
                "Coba range tanggal lain, atau ketik 'semua'."
            )
            return WAITING_FOR_DATE

        await update.message.reply_text(
            f"📅 Filter: {date_label}\n"
            f"📊 {len(filtered_text):,} karakter (dari {len(chat_text):,} total)\n"
            f"🔧 Mode: {'Restock' if mode == 'restock' else 'BD'}\n"
            "⏳ Memproses... (kirim /cancel untuk membatalkan)"
        )
        process_text = filtered_text
    else:
        await update.message.reply_text(
            f"📊 Memproses semua chat ({len(chat_text):,} karakter)...\n"
            f"🔧 Mode: {'Restock' if mode == 'restock' else 'BD'}\n"
            "⏳ Mungkin memakan waktu lebih lama. (kirim /cancel untuk membatalkan)"
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
        summary = await call_claude(process_text, context, mode)
        await send_long_message(update, summary)
    except CancelledError:
        await update.message.reply_text("⛔ Proses dibatalkan. Kirim file baru kapanpun.")
    except Exception as e:
        logger.error(f"Error calling Claude: {e}")
        await update.message.reply_text(f"Maaf, ada error: {str(e)[:200]}")

    context.user_data.pop("chat_text", None)
    context.user_data.pop("mode", None)
    context.user_data["cancel"] = False
    return ConversationHandler.END


async def summarize_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain text paste — defaults to restock mode."""
    chat_text = update.message.text

    if len(chat_text) < 50:
        await update.message.reply_text(
            "Pesannya terlalu pendek — paste chat WhatsApp yang lebih panjang ya!"
        )
        return

    context.user_data["cancel"] = False
    await update.message.chat.send_action("typing")

    try:
        summary = await call_claude(chat_text, context, "restock")
        await send_long_message(update, summary)
    except CancelledError:
        await update.message.reply_text("⛔ Proses dibatalkan.")
    except Exception as e:
        logger.error(f"Error calling Claude API: {e}")
        await update.message.reply_text(f"Maaf, ada error: {str(e)[:200]}")


# =====================================================================
# MAIN
# =====================================================================

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("cancel", cancel_command), group=-1)

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Document.ALL, receive_document)],
        states={
            WAITING_FOR_CONTEXT: [
                CommandHandler("cancel", cancel_command),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_context),
            ],
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
