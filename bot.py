"""
Refresh by Coco — WhatsApp Chat Summary Bot for Telegram
Supports two modes: RESTOCK (daily ops) and BD (business development).
Includes date filtering, chunking, /cancel support.
In groups: only responds when mentioned. In private chat: responds to everything.
"""

import os
import io
import re
import json
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
import matplotlib
matplotlib.use("Agg")  # Non-interactive backend for server
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

# --- Config ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = "claude-sonnet-4-20250514"
MAX_CHARS_PER_CHUNK = 50000

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Bot username — set on startup
BOT_USERNAME = ""

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
Format: bullet point per merchant, BUKAN tabel. Mudah dibaca di HP.

6 KATEGORI OUTPUT:

1. RESTOCK MERCHANT
   Tulis per merchant dengan format:
   • [Nama Toko] — [tanggal]
     - Isi: [jumlah] botol
     - Bayar: [status bayar + jumlah kalau ada]
     - Ket: [keterangan tambahan kalau ada]

   Panduan status bayar:
   - "Bayar" / "udah bayar" / "cash" / "transfer" = Lunas. Catat jumlahnya.
   - "Nanti aja" / "akhir bulan" / "bulanan" = Bayar bulanan
   - Kalau tidak disebutkan = tulis "Tidak disebutkan"
   - "Titip" / "isi" / "taruh" = restock
   - Satuan selalu BOTOL

2. POSM (Poster / Akrilik)
   Tulis per merchant dengan format:
   • [Nama Toko]
     - Poster: [Ada / Tidak ada / Tidak disebutkan]
     - Akrilik: [Ada / Tidak ada / Tidak disebutkan]
     - Dilaporkan karyawan: [Ya / Tidak]
     - Alasan tidak pasang: [alasan / -]

   - "Tempel" / "pasang" / "poster" / "akrilik" = POSM

3. RETUR / PRODUK EXPIRED
   Tulis per merchant dengan format:
   • [Nama Toko] — [tanggal]
     - Jumlah: [jumlah] botol
     - Ket: [keterangan, misal "exp besok", "sudah basi"]

   - Karena shelf life pendek, retur itu NORMAL dan sering terjadi
   - "Ambil balik" / "tarik" / "expired" / "basi" / "exp" = retur

4. MERCHANT CHURN
   Tulis per merchant dengan format:
   • [Nama Toko] — [alasan kalau disebutkan]

   - "Gamau lagi" / "stop" / "tarik semua" = churn

5. MERCHANT LIBUR / TUTUP SEMENTARA
   Tulis per merchant dengan format:
   • [Nama Toko]
     - Libur mulai: [tanggal]
     - Buka kembali: [tanggal / belum ada info]
     - Ket: [keterangan, misal "pulkam", "renovasi"]

   - "Libur" / "pulkam" / "tutup dulu" = libur sementara

6. TOPIK / ISU LAINNYA
   Tulis per topik dengan bullet point:
   • [topik]: [detail]

   Meliputi:
   - Operasional & logistik (macet, motor rusak, stiker habis, dll)
   - Keuangan (gaji, pembayaran merchant, insentif)
   - Info kompetitor dari lapangan
   - Masalah internal tim
   - Hal lain yang relevan

ATURAN FORMAT:
- Output dalam Bahasa Indonesia
- JANGAN gunakan tabel atau monospace block — gunakan bullet point saja
- Kalau suatu kategori tidak ada datanya, tulis "Tidak ada data untuk periode ini"
- Chat sangat kasual dan informal (bahasa gaul Indonesia)
- "enci" = pemilik toko (Tionghoa), "GOR" = lapangan badminton

DATA TABEL (WAJIB):
Setelah ringkasan bullet point, WAJIB tambahkan blok JSON untuk pembuatan gambar tabel.
Tulis persis dalam format ini di akhir output:

---TABLE_JSON_START---
{
  "restock": [
    {"toko": "Nama Toko", "tgl": "9 Mar", "jumlah": "8 botol", "bayar": "Lunas Rp 80rb", "ket": "-"}
  ],
  "posm": [
    {"toko": "Nama Toko", "poster": "Ada", "akrilik": "Ada", "laporan": "Ya", "alasan": "-"}
  ],
  "retur": [
    {"toko": "Nama Toko", "tgl": "9 Mar", "jumlah": "4 botol", "ket": "Exp besok"}
  ]
}
---TABLE_JSON_END---

Aturan JSON:
- Array kosong [] jika tidak ada data untuk kategori tersebut
- Semua value harus string
- JANGAN masukkan kategori churn, libur, atau isu lainnya ke JSON — hanya restock, posm, retur
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
Format: bullet point per toko, BUKAN tabel. Mudah dibaca di HP.

KATEGORI OUTPUT:

1. TOKO YANG DIKUNJUNGI (prospek)
   Tulis per toko dengan format:
   • [Nama Toko] — [tanggal]
     - Sampel: [jumlah botol yang dikasih / tidak ada]
     - Status: [OK jadi merchant / Pending / Reject / Follow up]
     - Next step: [apa yang perlu dilakukan / -]
     - Stok masuk: [jumlah botol pertama yang dititip / belum]
     - No HP: [nomor kalau disebutkan / -]
     - Ket: [lokasi, tipe toko, nama pemilik, dll]

   - "Kasih sampel" / "coba" / "tester" = sampel
   - "Gamau" / "ga tertarik" / "udah ada supplier" = reject
   - "Nanti aja" / "pikir dulu" / "hubungi lagi" = follow up

2. TOKO REJECT
   Tulis per toko dengan format:
   • [Nama Toko] — [alasan reject]

3. FOLLOW UP
   Tulis per toko dengan format:
   • [Nama Toko]
     - Tanggal follow up: [tanggal / belum ditentukan]
     - Status terakhir: [misal "sudah kasih sampel, tunggu feedback"]

4. TOPIK / ISU LAINNYA
   Tulis per topik dengan bullet point:
   • [topik]: [detail]

   Meliputi:
   - Masalah di lapangan (area susah dijangkau, parkir, dll)
   - Insight pasar (kompetitor, harga pasaran, permintaan)
   - Strategi atau arahan dari owner
   - Hal lain yang relevan

ATURAN FORMAT:
- Output dalam Bahasa Indonesia
- JANGAN gunakan tabel atau monospace block — gunakan bullet point saja
- Kalau suatu kategori tidak ada datanya, tulis "Tidak ada data untuk periode ini"
- Chat sangat kasual dan informal (bahasa gaul Indonesia)
- "enci" = pemilik toko (Tionghoa), "GOR" = lapangan badminton

DATA TABEL (WAJIB):
Setelah ringkasan bullet point, WAJIB tambahkan blok JSON untuk pembuatan gambar tabel.
Tulis persis dalam format ini di akhir output:

---TABLE_JSON_START---
{
  "prospek": [
    {"toko": "Nama Toko", "tgl": "9 Mar", "sampel": "3 botol", "status": "Pending", "next_step": "Follow up minggu depan"}
  ],
  "reject": [
    {"toko": "Nama Toko", "alasan": "Sudah ada supplier"}
  ],
  "follow_up": [
    {"toko": "Nama Toko", "tgl_fu": "15 Mar", "status_terakhir": "Sudah kasih sampel"}
  ]
}
---TABLE_JSON_END---

Aturan JSON:
- Array kosong [] jika tidak ada data untuk kategori tersebut
- Semua value harus string
- JANGAN masukkan kategori isu lainnya ke JSON — hanya prospek, reject, follow_up
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
- JANGAN gunakan tabel — gunakan bullet point per merchant
- Output dalam Bahasa Indonesia

DATA TABEL (WAJIB):
Di akhir ringkasan gabungan, WAJIB sertakan blok JSON gabungan dengan format:
---TABLE_JSON_START---
{
  "restock": [...],
  "posm": [...],
  "retur": [...]
}
---TABLE_JSON_END---
Gabungkan data dari semua ringkasan parsial, hapus duplikat merchant.
"""

MERGE_BD_PROMPT = """Kamu menerima beberapa ringkasan parsial dari chat WhatsApp BD (business development) yang dipecah jadi beberapa bagian.

Tugasmu: GABUNGKAN semua ringkasan parsial menjadi SATU ringkasan final yang lengkap dan rapi.

Aturan:
- Kalau toko yang sama muncul di beberapa bagian, gabungkan datanya (jangan duplikat)
- Ambil status terbaru untuk setiap toko
- Untuk reject dan follow up, pastikan tidak ada duplikat
- Untuk isu lainnya, gabungkan semua poin unik
- Output tetap dalam format kategori BD yang sama
- JANGAN gunakan tabel — gunakan bullet point per toko
- Output dalam Bahasa Indonesia

DATA TABEL (WAJIB):
Di akhir ringkasan gabungan, WAJIB sertakan blok JSON gabungan dengan format:
---TABLE_JSON_START---
{
  "prospek": [...],
  "reject": [...],
  "follow_up": [...]
}
---TABLE_JSON_END---
Gabungkan data dari semua ringkasan parsial, hapus duplikat toko.
"""


# =====================================================================
# HELPER: Check if bot is addressed in group
# =====================================================================

def is_bot_addressed(update: Update) -> bool:
    """Check if the bot should respond to this message.
    - Private chat: always respond
    - Group chat: only if mentioned or replying to the bot
    """
    if update.message.chat.type == "private":
        return True

    # Check if bot is mentioned in text
    if update.message.text and f"@{BOT_USERNAME}" in update.message.text:
        return True

    # Check if message is a reply to the bot
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        if update.message.reply_to_message.from_user.username == BOT_USERNAME:
            return True

    # Check if bot is mentioned in caption (for files sent with caption)
    if update.message.caption and f"@{BOT_USERNAME}" in update.message.caption:
        return True

    # Check entities for bot mention
    entities = update.message.entities or update.message.caption_entities or []
    for entity in entities:
        if entity.type == "mention":
            text = update.message.text or update.message.caption or ""
            mention = text[entity.offset : entity.offset + entity.length]
            if mention.lower() == f"@{BOT_USERNAME.lower()}":
                return True

    return False


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


# =====================================================================
# TABLE IMAGE GENERATION
# =====================================================================

HEADER_COLOR = "#1B4332"
HEADER_TEXT_COLOR = "white"
ROW_COLOR_1 = "#F0F7F4"
ROW_COLOR_2 = "white"


def extract_table_json(text: str) -> tuple[str, dict | None]:
    """Extract JSON block from Claude response. Returns (clean_text, json_data)."""
    pattern = r"---TABLE_JSON_START---\s*(\{.*?\})\s*---TABLE_JSON_END---"
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return text, None
    try:
        data = json.loads(match.group(1))
        clean_text = text[: match.start()].rstrip()
        return clean_text, data
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse table JSON: {e}")
        clean_text = text[: match.start()].rstrip()
        return clean_text, None


def create_combined_table_image(table_data: dict, mode: str, period: str = "") -> io.BytesIO | None:
    """Generate a combined table image from structured data. Returns BytesIO or None."""
    if mode == "restock":
        sections = _build_restock_sections(table_data)
    else:
        sections = _build_bd_sections(table_data)

    # Filter out empty sections
    sections = [(title, headers, rows) for title, headers, rows in sections if rows]
    if not sections:
        return None

    # Calculate figure size
    total_rows = sum(len(rows) + 2 for _, _, rows in sections)  # +2 for title + header
    fig_height = max(3, total_rows * 0.45 + 1.5)
    max_cols = max(len(h) for _, h, _ in sections)
    fig_width = max(8, max_cols * 2.2)

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    ax.axis("off")

    # Title
    title_text = "Ringkasan Operasional" if mode == "restock" else "Ringkasan BD"
    title_text += " \u2014 Refresh by Coco"
    if period:
        title_text += f"\nPeriode: {period}"
    ax.set_title(title_text, fontsize=14, fontweight="bold", pad=20, loc="center")

    # We stack tables vertically using subplots
    plt.close(fig)

    # Use subplots approach for multiple tables
    n_sections = len(sections)
    fig, axes = plt.subplots(
        n_sections, 1,
        figsize=(fig_width, sum(max(1.5, len(rows) * 0.45 + 1.0) for _, _, rows in sections) + 1.5),
        gridspec_kw={"hspace": 0.6},
    )
    if n_sections == 1:
        axes = [axes]

    # Main title
    fig.suptitle(title_text, fontsize=14, fontweight="bold", y=0.98)

    for idx, (title, headers, rows) in enumerate(sections):
        ax = axes[idx]
        ax.axis("off")
        ax.set_title(title, fontsize=11, fontweight="bold", loc="left", pad=10)

        table = ax.table(
            cellText=rows,
            colLabels=headers,
            loc="center",
            cellLoc="center",
        )

        table.auto_set_font_size(False)
        table.set_fontsize(9)
        table.scale(1, 1.4)

        # Style header
        for j in range(len(headers)):
            cell = table[0, j]
            cell.set_facecolor(HEADER_COLOR)
            cell.set_text_props(color=HEADER_TEXT_COLOR, fontweight="bold")
            cell.set_edgecolor("white")

        # Style data rows
        for i in range(1, len(rows) + 1):
            for j in range(len(headers)):
                cell = table[i, j]
                cell.set_facecolor(ROW_COLOR_1 if i % 2 == 0 else ROW_COLOR_2)
                cell.set_edgecolor("#E0E0E0")

        # Auto-fit column widths
        table.auto_set_column_width(list(range(len(headers))))

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=180, bbox_inches="tight", facecolor="white", pad_inches=0.3)
    plt.close(fig)
    buf.seek(0)
    return buf


def _build_restock_sections(data: dict) -> list[tuple[str, list[str], list[list[str]]]]:
    """Build table sections for restock mode."""
    sections = []

    # 1. Restock
    restock = data.get("restock", [])
    if restock:
        headers = ["Toko", "Tgl", "Jumlah", "Bayar", "Keterangan"]
        rows = [[r.get("toko", ""), r.get("tgl", ""), r.get("jumlah", ""), r.get("bayar", ""), r.get("ket", "-")] for r in restock]
        sections.append(("1. RESTOCK MERCHANT", headers, rows))

    # 2. POSM
    posm = data.get("posm", [])
    if posm:
        headers = ["Toko", "Poster", "Akrilik", "Laporan", "Alasan"]
        rows = [[p.get("toko", ""), p.get("poster", ""), p.get("akrilik", ""), p.get("laporan", ""), p.get("alasan", "-")] for p in posm]
        sections.append(("2. POSM (Poster / Akrilik)", headers, rows))

    # 3. Retur
    retur = data.get("retur", [])
    if retur:
        headers = ["Toko", "Tgl", "Jumlah", "Keterangan"]
        rows = [[r.get("toko", ""), r.get("tgl", ""), r.get("jumlah", ""), r.get("ket", "-")] for r in retur]
        sections.append(("3. RETUR / PRODUK EXPIRED", headers, rows))

    return sections


def _build_bd_sections(data: dict) -> list[tuple[str, list[str], list[list[str]]]]:
    """Build table sections for BD mode."""
    sections = []

    # 1. Prospek
    prospek = data.get("prospek", [])
    if prospek:
        headers = ["Toko", "Tgl", "Sampel", "Status", "Next Step"]
        rows = [[p.get("toko", ""), p.get("tgl", ""), p.get("sampel", ""), p.get("status", ""), p.get("next_step", "-")] for p in prospek]
        sections.append(("1. TOKO DIKUNJUNGI (Prospek)", headers, rows))

    # 2. Reject
    reject = data.get("reject", [])
    if reject:
        headers = ["Toko", "Alasan"]
        rows = [[r.get("toko", ""), r.get("alasan", "")] for r in reject]
        sections.append(("2. TOKO REJECT", headers, rows))

    # 3. Follow up
    follow_up = data.get("follow_up", [])
    if follow_up:
        headers = ["Toko", "Tgl Follow Up", "Status Terakhir"]
        rows = [[f.get("toko", ""), f.get("tgl_fu", ""), f.get("status_terakhir", "")] for f in follow_up]
        sections.append(("3. FOLLOW UP", headers, rows))

    return sections


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

async def post_init(application: Application):
    """Called after bot starts — fetch bot username for mention detection."""
    global BOT_USERNAME
    bot = await application.bot.get_me()
    BOT_USERNAME = bot.username
    logger.info(f"Bot username: @{BOT_USERNAME}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Halo! Aku bot ringkasan WhatsApp untuk Refresh by Coco 🥥\n\n"
        "Cara pakai:\n"
        "1. Kirim file zip / txt dari WhatsApp export\n"
        "2. Pilih konteks: Restock atau BD\n"
        "3. Masukkan range tanggal\n"
        "4. Tunggu sebentar, ringkasan muncul!\n\n"
        "Atau paste chat langsung sebagai text.\n\n"
        f"Di grup: mention @{BOT_USERNAME} atau reply ke pesan bot\n\n"
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
        f"Di grup: mention @{BOT_USERNAME} atau reply ke pesan bot\n\n"
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
    """Step 1: Receive file. In groups, only respond if bot is mentioned/replied to."""
    # In groups, check if bot is addressed
    if update.message.chat.type != "private":
        # Check caption for mention, or if it's a reply to the bot
        is_addressed = False
        if update.message.caption and f"@{BOT_USERNAME}" in update.message.caption:
            is_addressed = True
        if update.message.reply_to_message and update.message.reply_to_message.from_user:
            if update.message.reply_to_message.from_user.username == BOT_USERNAME:
                is_addressed = True
        # Check caption entities
        for entity in (update.message.caption_entities or []):
            if entity.type == "mention":
                caption = update.message.caption or ""
                mention = caption[entity.offset : entity.offset + entity.length]
                if mention.lower() == f"@{BOT_USERNAME.lower()}":
                    is_addressed = True
        if not is_addressed:
            return ConversationHandler.END

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
    # Remove bot mention if present
    text = text.replace(f"@{BOT_USERNAME.lower()}", "").strip()

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
    # Remove bot mention if present
    date_input = date_input.replace(f"@{BOT_USERNAME}", "").strip()

    chat_text = context.user_data.get("chat_text", "")
    mode = context.user_data.get("mode", "restock")

    if not chat_text:
        await update.message.reply_text("Tidak ada file yang tersimpan. Kirim ulang file-nya ya.")
        return ConversationHandler.END

    context.user_data["cancel"] = False
    start_date, end_date = parse_date_range(date_input)
    date_label = ""

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
        raw_summary = await call_claude(process_text, context, mode)

        # Extract JSON table data and clean summary text
        summary, table_data = extract_table_json(raw_summary)

        # Send bullet point text summary
        await send_long_message(update, summary)

        # Generate and send table image if data available
        if table_data:
            period_label = date_label if (start_date and end_date) else "Semua"
            try:
                img_buf = create_combined_table_image(table_data, mode, period_label)
                if img_buf:
                    await update.message.reply_photo(
                        photo=img_buf,
                        caption="📊 Tabel ringkasan (gambar)",
                    )
            except Exception as img_err:
                logger.warning(f"Failed to generate table image: {img_err}")
                # Non-fatal: text summary already sent

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
    """Handle plain text paste. In groups, only respond if mentioned."""
    if not is_bot_addressed(update):
        return

    chat_text = update.message.text
    # Remove bot mention
    chat_text = chat_text.replace(f"@{BOT_USERNAME}", "").strip()

    if len(chat_text) < 50:
        await update.message.reply_text(
            "Pesannya terlalu pendek — paste chat WhatsApp yang lebih panjang ya!"
        )
        return

    context.user_data["cancel"] = False
    await update.message.chat.send_action("typing")

    try:
        raw_summary = await call_claude(chat_text, context, "restock")
        summary, table_data = extract_table_json(raw_summary)
        await send_long_message(update, summary)

        if table_data:
            try:
                img_buf = create_combined_table_image(table_data, "restock")
                if img_buf:
                    await update.message.reply_photo(
                        photo=img_buf,
                        caption="📊 Tabel ringkasan (gambar)",
                    )
            except Exception as img_err:
                logger.warning(f"Failed to generate table image: {img_err}")
    except CancelledError:
        await update.message.reply_text("⛔ Proses dibatalkan.")
    except Exception as e:
        logger.error(f"Error calling Claude API: {e}")
        await update.message.reply_text(f"Maaf, ada error: {str(e)[:200]}")


# =====================================================================
# MAIN
# =====================================================================

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

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
        per_chat=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, summarize_text))

    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
