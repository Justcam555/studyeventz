#!/usr/bin/env python3
"""
build_events_page.py — Build the events.html static page for the GitHub Pages site.

Reads events from data/agents.db, exports data/events.json, and writes
events.html at the repo root. events.html fetches events.json at runtime
and renders the filtered, week-grouped layout client-side.

Usage:
    python build_events_page.py
"""

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "agents.db"
SITEMAP_OUT = ROOT / "sitemap.xml"
ROBOTS_OUT = ROOT / "robots.txt"
INDEX_OUT = ROOT / "index.html"
CHARACTERS_DIR = ROOT / "assets" / "characters"
LOGOS_DIR = ROOT / "assets" / "logos"

# ─── Brand-level (cross-country) site metadata ─────────────────────────────
SITE_URL  = "https://www.studyeventz.com"
SITE_KEY  = "studyeventz-public-2026"  # must match wrangler.toml [vars].SITE_KEY
INGEST_URL = "https://studyeventz-app.mylogins555.workers.dev/i"

# Old-path redirect shims (kept so inbound links to /events.html etc. still work)
LEGACY_PAGES = ("events.html", "about.html", "contact.html", "submit.html")


# ─── Multi-country config ──────────────────────────────────────────────────
# Adding a new country = appending a Country() to COUNTRIES. The build loop
# emits a complete page tree at /<country.code>/ for each one. The root /
# becomes a country picker.

@dataclass(frozen=True)
class Country:
    code: str               # URL slug + output dir, e.g. "thailand"
    name_en: str            # "Thailand"
    name_native: str        # "ไทย" — short native name for hero / picker
    flag: str               # "🇹🇭"
    primary_lang: str       # BCP-47 code for the native language pair, e.g. "th"
    iso2: str               # ISO 3166-1 alpha-2 for JSON-LD addressCountry, e.g. "TH"
    agent_db_match: str     # SQL LIKE pattern for agents.country, e.g. "%Thailand%"
    timezone: str           # IANA tz for ICS calendar exports
    title: str              # browser <title> for events.html
    meta_desc_en: str       # English meta description (<160 chars for SERP)
    meta_desc_native: str   # Native-language meta description
    contact_email: str      # contact us email

    # Notify banner: "line" markets show a LINE OA add-friend chip; every other
    # market shows an email chip (mailto). Set notify_channel per market.
    notify_channel: str = "email"   # "line" | "email"
    notify_text_native: str = ""    # native-language banner CTA sentence
    line_handle: str = ""           # @handle — only used when notify_channel == "line"
    line_url: str = ""              # add-friend URL — only used when notify_channel == "line"

    # Optional local-city quick-filter chip on the events page. Empty label =
    # no chip (markets with a single city, or several hubs, omit it).
    local_filter_label: str = ""    # chip label, e.g. "Bangkok"
    local_filter_match: str = ""    # lowercase substring matched against event location

    # English-native markets (e.g. Ghana, Nigeria): render single-language
    # English by dropping the bilingual native-language counterparts, rather
    # than carrying a translation map. Leaves only the English copy.
    english_only: bool = False

    # Native-language localisation. Maps a source substring found verbatim in
    # the page templates (Thai, the original build language) to its translation
    # for this market. Thailand leaves this empty (templates are already Thai),
    # so its output is unchanged. Applied longest-key-first to avoid a short
    # key clobbering a longer one it is a substring of.
    translations: dict = field(default_factory=dict)

    # Per-country output paths
    @property
    def root(self) -> Path:           return ROOT / self.code
    @property
    def html_out(self) -> Path:       return self.root / "events.html"
    @property
    def about_out(self) -> Path:      return self.root / "about.html"
    @property
    def contact_out(self) -> Path:    return self.root / "contact.html"
    @property
    def submit_out(self) -> Path:     return self.root / "submit.html"
    @property
    def privacy_out(self) -> Path:    return self.root / "privacy.html"
    @property
    def json_out(self) -> Path:       return self.root / "data" / "events.json"
    # Per-country public URLs
    @property
    def site_path(self) -> str:       return f"/{self.code}"
    @property
    def site_url(self) -> str:        return f"{SITE_URL}{self.site_path}"
    @property
    def events_url(self) -> str:      return f"{self.site_url}/events.html"


THAILAND = Country(
    code="thailand",
    name_en="Thailand",
    name_native="ไทย",
    flag="🇹🇭",
    primary_lang="th",
    iso2="TH",
    agent_db_match="%Thailand%",
    timezone="Asia/Bangkok",
    title="Study Abroad Events in Thailand | Education Fairs & University Webinars | StudyEventz",
    meta_desc_en=("Find study abroad events in Thailand — fairs, webinars and briefings for "
                  "students considering the UK, Australia, USA, Canada and Europe. Updated weekly."),
    meta_desc_native="รวมงาน Study Abroad ในไทย อัปเดตทุกสัปดาห์",
    contact_email="info@studyeventz.com",
    notify_channel="line",
    notify_text_native="รับการแจ้งเตือนงานใหม่ทุกสัปดาห์ → ติดตามเราบน LINE",
    line_handle="@studyeventz",
    line_url="https://lin.ee/RdZs9AD",
    local_filter_label="Bangkok",
    local_filter_match="bangkok",
    # translations left empty: the templates are already in Thai, so Thailand's
    # output is byte-for-byte unchanged by the localisation pass.
)

# ─── Vietnam ────────────────────────────────────────────────────────────────
# Email-only market (no LINE OA). The `translations` map carries the Vietnamese
# for every native (Thai) string baked into the page templates. AI-DRAFTED —
# flag for native-speaker review before this market is published.
VIETNAM = Country(
    code="vietnam",
    name_en="Vietnam",
    name_native="Việt Nam",
    flag="🇻🇳",
    primary_lang="vi",
    iso2="VN",
    agent_db_match="%Vietnam%",
    timezone="Asia/Ho_Chi_Minh",
    title="Sự kiện Du học tại Việt Nam | Hội thảo & Triển lãm Giáo dục Đại học | StudyEventz",
    meta_desc_en=("Find study abroad events in Vietnam — fairs, webinars and briefings for "
                  "students considering the UK, Australia, USA, Canada and Europe. Updated weekly."),
    meta_desc_native="Tổng hợp sự kiện du học tại Việt Nam, cập nhật hằng tuần.",
    contact_email="info@studyeventz.com",
    notify_channel="email",
    notify_text_native="Nhận thông báo sự kiện mới hằng tuần → email cho chúng tôi",
    translations={
        # ── Events page ──
        "รวมอีเวนต์เรียนต่อต่างประเทศในไทย": "Tổng hợp sự kiện du học tại Việt Nam",
        "รวมงานแฟร์มหาวิทยาลัย เวบินาร์ และกิจกรรมเรียนต่อต่างประเทศไว้ในที่เดียว":
            "Hội chợ đại học, hội thảo trực tuyến và sự kiện du học, tất cả ở một nơi.",
        "อัปเดตทุกสัปดาห์ พร้อมอีเวนต์ในอีก 30 วันข้างหน้า":
            "Cập nhật hằng tuần với các sự kiện trong 30 ngày tới.",
        "ตัวกรอง": "Bộ lọc",
        "studyeventz รวบรวมงาน study abroad จากบริษัทแนะแนวทั่วประเทศไทย อัปเดตทุกวันจันทร์":
            "studyeventz tổng hợp các sự kiện du học từ các công ty tư vấn trên khắp Việt Nam. Cập nhật mỗi thứ Hai.",
        # ── About page ──
        "studyeventz เป็นคู่มืออิสระสำหรับค้นหากิจกรรมเรียนต่อต่างประเทศในไทย":
            "studyeventz là cẩm nang độc lập giúp tìm các sự kiện du học tại Việt Nam",
        "เกี่ยวกับเรา": "Về chúng tôi",
        "studyeventz เป็นคู่มืออิสระสำหรับค้นหากิจกรรมเรียนต่อต่างประเทศ ไม่ว่าจะเป็นงานแฟร์มหาวิทยาลัย วันให้ข้อมูล Open Day หรือกำหนดปิดรับสมัครทุนการศึกษา โดยรวบรวมไว้ในที่เดียว และอัปเดตทุกสัปดาห์":
            "studyeventz là cẩm nang độc lập giúp tìm các sự kiện du học — hội chợ đại học, ngày thông tin, open day và hạn nộp học bổng — tất cả ở một nơi và được cập nhật hằng tuần.",
        "ปกติแล้ว การหากิจกรรมเหล่านี้ต้องใช้เวลาค้นหาจาก Facebook หลายสิบเพจ เว็บไซต์เอเจนซี่ และปฏิทินกิจกรรมของมหาวิทยาลัยต่าง ๆ แต่เราเป็นคนทำงานนั้นให้โดยอัตโนมัติ ทุกสัปดาห์ เรารวบรวมกิจกรรมจากบริษัทแนะแนวการศึกษาและพาร์ตเนอร์มหาวิทยาลัยทั่วตลาด ตรวจสอบและลบข้อมูลซ้ำ แล้วเผยแพร่เป็นรายการกิจกรรมที่สะอาด ชัดเจน และเชื่อถือได้":
            "Thông thường, việc tìm các sự kiện này đồng nghĩa với việc lùng sục hàng chục trang Facebook, website của các công ty tư vấn và lịch sự kiện của các trường đại học. Chúng tôi làm việc đó một cách tự động: mỗi tuần, chúng tôi thu thập sự kiện từ các công ty tư vấn giáo dục và đối tác đại học trên toàn thị trường, loại bỏ các mục trùng lặp, và công bố một danh sách gọn gàng mà bạn thực sự có thể tin cậy.",
        "เราเริ่มต้นจากประเทศไทย ซึ่งในแต่ละปีมีงานเรียนต่อต่างประเทศหลายร้อยงาน แต่ยังไม่มีศูนย์กลางเดียวสำหรับค้นหาข้อมูลเหล่านี้ เราเป็นแพลตฟอร์มอิสระ ไม่ได้เป็นตัวแทนของมหาวิทยาลัยหรือเอเจนซี่ใดเป็นพิเศษ ดังนั้นสิ่งที่คุณเห็นคือภาพรวมของตัวเลือกที่หลากหลาย ไม่ใช่การนำเสนอจากบริษัทใดบริษัทหนึ่งเท่านั้น":
            "Chúng tôi khởi đầu tại Thái Lan, nơi mỗi năm có hàng trăm sự kiện du học nhưng không có một nơi tập trung nào để tìm chúng. Chúng tôi độc lập — không đại diện cho bất kỳ trường đại học hay công ty tư vấn nào, nên những gì bạn thấy là toàn cảnh các lựa chọn, chứ không phải lời chào mời của riêng một công ty.",
        "สนใจนำ studyeventz ไปใช้ในตลาดของคุณหรือไม่? เรายินดีพูดคุยกับคุณครับ/ค่ะ":
            "Bạn muốn đưa studyeventz đến thị trường của mình? Chúng tôi rất mong được trò chuyện với bạn.",
        # ── Contact page ──
        "ติดต่อ studyeventz เพื่อแจ้งเพิ่มงาน แจ้งแก้ไขข้อมูล หรือร่วมงานกับเรา":
            "Liên hệ studyeventz để thêm sự kiện, báo lỗi thông tin, hoặc hợp tác cùng chúng tôi",
        "ติดต่อเรา": "Liên hệ",
        "มีงานที่เราควรเพิ่มในรายการ พบข้อมูลที่ล้าสมัย หรืออยากร่วมงานกับเราใช่ไหม? อีเมลหาเราได้ที่ ":
            "Bạn có sự kiện nên được đưa vào danh sách, phát hiện thông tin lỗi thời, hoặc muốn hợp tác với chúng tôi? Hãy email cho chúng tôi tại ",
        " แล้วเราจะติดต่อกลับไป": " và chúng tôi sẽ phản hồi.",
        "แจ้งเพิ่มกิจกรรม": "Thêm một sự kiện",
        "หากคุณกำลังจัดงานแฟร์เรียนต่อต่างประเทศ Open Day หรืองานให้ข้อมูล ส่งรายละเอียดมาให้เรา แล้วเราจะเพิ่มลงในรายการ":
            "Bạn đang tổ chức hội chợ du học, open day hay buổi thông tin? Gửi cho chúng tôi thông tin chi tiết và chúng tôi sẽ thêm vào danh sách.",
        "ส่งงานเข้ามา": "Gửi sự kiện",
        "แจ้งแก้ไขข้อมูล": "Báo lỗi thông tin",
        "พบวันที่ผิด หรือลิงก์ใช้งานไม่ได้ใช่ไหม? แจ้งให้เราทราบ แล้วเราจะรีบแก้ไขให้":
            "Phát hiện sai ngày hoặc liên kết hỏng? Hãy báo cho chúng tôi và chúng tôi sẽ sửa ngay.",
        "ความร่วมมือ": "Hợp tác",
        "หากคุณสนใจนำ studyeventz ไปเปิดในตลาดใหม่ หรืออยากร่วมมือกับเราในตลาดที่เราครอบคลุมอยู่แล้ว ติดต่อเราได้เลย":
            "Nếu bạn muốn đưa studyeventz đến một thị trường mới, hoặc hợp tác với chúng tôi tại thị trường chúng tôi đã có mặt, hãy liên hệ.",
        # ── Submit page ──
        "แจ้งเพิ่มกิจกรรมเรียนต่อต่างประเทศใน studyeventz":
            "Gửi sự kiện du học tới studyeventz",
        "กรอกรายละเอียดด้านล่าง เราจะตรวจสอบและเพิ่มลงในรายการของเรา ฟรี ไม่มีค่าใช้จ่าย":
            "Điền thông tin bên dưới. Chúng tôi sẽ kiểm tra và thêm vào danh sách. Miễn phí cho đơn vị tổ chức.",
        "รายละเอียดกิจกรรม": "Thông tin sự kiện",
        "ผู้จัด": "Đơn vị tổ chức",
        "ชื่อกิจกรรม": "Tên sự kiện",
        "วันที่": "Ngày",
        "เวลา": "Giờ",
        "สถานที่": "Địa điểm",
        "ลิงก์ลงทะเบียน": "Liên kết đăng ký",
        "ข้อมูลผู้แจ้ง": "Thông tin người gửi",
        "ชื่อ": "Tên của bạn",
        "อีเมล": "Email",
        "หมายเหตุเพิ่มเติม": "Ghi chú thêm",
        "ส่ง": "Gửi",
        "ขอบคุณค่ะ": "Xin cảm ơn!",
        # ── Privacy page (AI-drafted, review before launch) ──
        "นโยบายความเป็นส่วนตัว": "Chính sách quyền riêng tư",
        "studyeventz ให้ความสำคัญกับความเป็นส่วนตัวของคุณ เราไม่ใช้คุกกี้ ไม่ใช้ตัวติดตามเพื่อการโฆษณา และไม่ขายข้อมูลของคุณ":
            "studyeventz tôn trọng quyền riêng tư của bạn. Chúng tôi không dùng cookie, không dùng công cụ theo dõi quảng cáo, và không bao giờ bán dữ liệu của bạn.",
        "เราเก็บข้อมูลเล็กน้อยไว้ในเบราว์เซอร์ของคุณ เพื่อจดจำตลาดที่คุณเลือก และเพื่อพักข้อมูลสถิติการใช้งานแบบไม่ระบุตัวตนก่อนส่ง ข้อมูลนี้อยู่บนอุปกรณ์ของคุณ และคุณลบได้ทุกเมื่อผ่านการตั้งค่าเบราว์เซอร์":
            "Chúng tôi lưu một lượng nhỏ dữ liệu trong trình duyệt của bạn để ghi nhớ thị trường bạn đã chọn và để tạm giữ số liệu sử dụng ẩn danh trước khi gửi đi. Dữ liệu này nằm trên thiết bị của bạn và bạn có thể xóa bất cứ lúc nào qua cài đặt trình duyệt.",
        "เราเก็บสถิติการใช้งานแบบไม่ระบุตัวตน เช่น หน้าที่เปิดดูและกิจกรรมที่คลิก เพื่อปรับปรุงรายการให้ดีขึ้น เซิร์ฟเวอร์ของเราบันทึกชนิดเบราว์เซอร์ หน้าที่อ้างอิงเข้ามา และที่อยู่ IP ในรูปแบบที่แปลงเป็นค่าแฮชทางเดียว เราไม่เคยเก็บที่อยู่ IP จริงของคุณ และไม่ระบุตัวตนของคุณเป็นรายบุคคล":
            "Chúng tôi thu thập số liệu sử dụng ẩn danh — chẳng hạn những trang được xem và sự kiện được nhấp — để cải thiện danh sách. Máy chủ của chúng tôi ghi lại loại trình duyệt, trang giới thiệu, và địa chỉ IP của bạn ở dạng băm một chiều. Chúng tôi không bao giờ lưu địa chỉ IP thật và không nhận dạng bạn theo cá nhân.",
        "เว็บไซต์นี้ทำงานบนโครงสร้างพื้นฐานของ Cloudflare ซึ่งเป็นผู้ประมวลผลข้อมูลให้เรา เราโฮสต์ฟอนต์ของเราเอง และไม่ใช้ Google Analytics, Meta Pixel หรือเครือข่ายโฆษณาใด ๆ":
            "Trang web này chạy trên hạ tầng Cloudflare — đơn vị xử lý dữ liệu cho chúng tôi. Chúng tôi tự lưu trữ phông chữ của mình, và không dùng Google Analytics, Meta Pixel, hay bất kỳ mạng quảng cáo nào.",
        "หากมีคำถามเกี่ยวกับความเป็นส่วนตัว หรือต้องการให้ลบข้อมูลของคุณ ติดต่อเราได้ที่ info@studyeventz.com":
            "Nếu bạn có thắc mắc về quyền riêng tư hoặc muốn xóa dữ liệu của mình, hãy liên hệ chúng tôi tại info@studyeventz.com",
        # ── Country-specific English copy ──
        "studyeventz is an independent guide to study abroad events in Thailand — fairs, webinars and briefings gathered weekly.":
            "studyeventz is an independent guide to study abroad events in Vietnam — fairs, webinars and briefings gathered weekly.",
        "Submit a study abroad event to studyeventz — university fair, info session, open day, webinar. Free for organizers in Thailand.":
            "Submit a study abroad event to studyeventz — university fair, info session, open day, webinar. Free for organizers in Vietnam.",
        'placeholder=\'e.g. "Bangkok, Thailand" or "Online"\'':
            'placeholder=\'e.g. "Hanoi, Vietnam" or "Online"\'',
    },
)

# ─── Taiwan ─────────────────────────────────────────────────────────────────
# Traditional Chinese (zh-Hant, Taiwan wording). AI-DRAFTED demo/placeholder
# pages for in-market contacts — flag for native-speaker review before launch.
TAIWAN = Country(
    code="taiwan",
    name_en="Taiwan",
    name_native="台灣",
    flag="🇹🇼",
    primary_lang="zh-Hant",
    iso2="TW",
    agent_db_match="%Taiwan%",
    timezone="Asia/Taipei",
    title="台灣留學活動總覽 | 大學展、線上講座與升學說明會 | StudyEventz",
    meta_desc_en=("Find study abroad events in Taiwan — fairs, webinars and briefings for "
                  "students considering the UK, Australia, USA, Canada and Europe. Updated weekly."),
    meta_desc_native="彙整台灣的留學活動——大學展、線上講座與升學說明會，每週更新。",
    contact_email="info@studyeventz.com",
    notify_channel="email",
    notify_text_native="每週掌握最新留學活動 → 來信通知我們",
    local_filter_label="Taipei",
    local_filter_match="taipei",
    translations={
        # ── Events page ──
        "รวมอีเวนต์เรียนต่อต่างประเทศในไทย": "台灣留學活動總覽",
        "รวมงานแฟร์มหาวิทยาลัย เวบินาร์ และกิจกรรมเรียนต่อต่างประเทศไว้ในที่เดียว":
            "大學展、線上講座與留學活動，一站盡覽。",
        "อัปเดตทุกสัปดาห์ พร้อมอีเวนต์ในอีก 30 วันข้างหน้า":
            "每週更新，涵蓋未來 30 天的活動。",
        "ตัวกรอง": "篩選",
        "studyeventz รวบรวมงาน study abroad จากบริษัทแนะแนวทั่วประเทศไทย อัปเดตทุกวันจันทร์":
            "studyeventz 彙整全台灣升學顧問公司的留學活動，每週一更新。",
        # ── About page ──
        "studyeventz เป็นคู่มืออิสระสำหรับค้นหากิจกรรมเรียนต่อต่างประเทศในไทย":
            "studyeventz 是協助你尋找台灣留學活動的獨立指南",
        "เกี่ยวกับเรา": "關於我們",
        "studyeventz เป็นคู่มืออิสระสำหรับค้นหากิจกรรมเรียนต่อต่างประเทศ ไม่ว่าจะเป็นงานแฟร์มหาวิทยาลัย วันให้ข้อมูล Open Day หรือกำหนดปิดรับสมัครทุนการศึกษา โดยรวบรวมไว้ในที่เดียว และอัปเดตทุกสัปดาห์":
            "studyeventz 是協助你尋找留學活動的獨立指南——不論是大學展、開放日（Open Day）還是獎學金截止日期——全部彙整於一處，並每週更新。",
        "ปกติแล้ว การหากิจกรรมเหล่านี้ต้องใช้เวลาค้นหาจาก Facebook หลายสิบเพจ เว็บไซต์เอเจนซี่ และปฏิทินกิจกรรมของมหาวิทยาลัยต่าง ๆ แต่เราเป็นคนทำงานนั้นให้โดยอัตโนมัติ ทุกสัปดาห์ เรารวบรวมกิจกรรมจากบริษัทแนะแนวการศึกษาและพาร์ตเนอร์มหาวิทยาลัยทั่วตลาด ตรวจสอบและลบข้อมูลซ้ำ แล้วเผยแพร่เป็นรายการกิจกรรมที่สะอาด ชัดเจน และเชื่อถือได้":
            "一般而言，要找到這些活動，你得翻遍數十個 Facebook 專頁、各家顧問公司的網站，以及不同大學的活動行事曆。我們將這項工作自動化：每週彙整全市場升學顧問公司與大學夥伴的活動，核實並去除重複，再發佈成一份乾淨、清晰、值得信賴的活動清單。",
        "เราเริ่มต้นจากประเทศไทย ซึ่งในแต่ละปีมีงานเรียนต่อต่างประเทศหลายร้อยงาน แต่ยังไม่มีศูนย์กลางเดียวสำหรับค้นหาข้อมูลเหล่านี้ เราเป็นแพลตฟอร์มอิสระ ไม่ได้เป็นตัวแทนของมหาวิทยาลัยหรือเอเจนซี่ใดเป็นพิเศษ ดังนั้นสิ่งที่คุณเห็นคือภาพรวมของตัวเลือกที่หลากหลาย ไม่ใช่การนำเสนอจากบริษัทใดบริษัทหนึ่งเท่านั้น":
            "我們始於泰國——當地每年有數百場留學活動，卻沒有一個集中查詢的平台。我們是獨立平台，不代表任何特定大學或顧問公司，因此你看到的是多元選擇的全貌，而非單一公司的推銷。",
        "สนใจนำ studyeventz ไปใช้ในตลาดของคุณหรือไม่? เรายินดีพูดคุยกับคุณครับ/ค่ะ":
            "想把 studyeventz 帶到你的市場嗎？我們很樂意與你聊聊。",
        # ── Contact page ──
        "ติดต่อ studyeventz เพื่อแจ้งเพิ่มงาน แจ้งแก้ไขข้อมูล หรือร่วมงานกับเรา":
            "聯絡 studyeventz：新增活動、回報資料更正，或與我們合作",
        "ติดต่อเรา": "聯絡我們",
        "มีงานที่เราควรเพิ่มในรายการ พบข้อมูลที่ล้าสมัย หรืออยากร่วมงานกับเราใช่ไหม? อีเมลหาเราได้ที่ ":
            "有想讓我們收錄的活動、發現過時的資訊，或想與我們合作嗎？歡迎來信：",
        " แล้วเราจะติดต่อกลับไป": "，我們會盡快回覆你。",
        "แจ้งเพิ่มกิจกรรม": "新增活動",
        "หากคุณกำลังจัดงานแฟร์เรียนต่อต่างประเทศ Open Day หรืองานให้ข้อมูล ส่งรายละเอียดมาให้เรา แล้วเราจะเพิ่มลงในรายการ":
            "如果你正在籌辦留學展、開放日或說明會，把詳情寄給我們，我們就會加入清單。",
        "ส่งงานเข้ามา": "提交活動",
        "แจ้งแก้ไขข้อมูล": "回報資料更正",
        "พบวันที่ผิด หรือลิงก์ใช้งานไม่ได้ใช่ไหม? แจ้งให้เราทราบ แล้วเราจะรีบแก้ไขให้":
            "發現日期有誤或連結失效？告訴我們，我們會盡快修正。",
        "ความร่วมมือ": "合作",
        "หากคุณสนใจนำ studyeventz ไปเปิดในตลาดใหม่ หรืออยากร่วมมือกับเราในตลาดที่เราครอบคลุมอยู่แล้ว ติดต่อเราได้เลย":
            "如果你有意把 studyeventz 帶入新市場，或想在我們已涵蓋的市場與我們合作，歡迎聯絡我們。",
        # ── Submit page ──
        "แจ้งเพิ่มกิจกรรมเรียนต่อต่างประเทศใน studyeventz":
            "向 studyeventz 提交留學活動",
        "กรอกรายละเอียดด้านล่าง เราจะตรวจสอบและเพิ่มลงในรายการของเรา ฟรี ไม่มีค่าใช้จ่าย":
            "填寫以下資料，我們會審核並加入清單。完全免費。",
        "รายละเอียดกิจกรรม": "活動詳情",
        "ผู้จัด": "主辦單位",
        "ชื่อกิจกรรม": "活動名稱",
        "วันที่": "日期",
        "เวลา": "時間",
        "สถานที่": "地點",
        "ลิงก์ลงทะเบียน": "報名連結",
        "ข้อมูลผู้แจ้ง": "提交者資料",
        "ชื่อ": "姓名",
        "อีเมล": "電子郵件",
        "หมายเหตุเพิ่มเติม": "其他備註",
        "ส่ง": "送出",
        "ขอบคุณค่ะ": "感謝你！",
        # ── Privacy page (AI-drafted, review before launch) ──
        "นโยบายความเป็นส่วนตัว": "隱私權政策",
        "studyeventz ให้ความสำคัญกับความเป็นส่วนตัวของคุณ เราไม่ใช้คุกกี้ ไม่ใช้ตัวติดตามเพื่อการโฆษณา และไม่ขายข้อมูลของคุณ":
            "studyeventz 重視你的隱私。我們不使用 Cookie、不使用廣告追蹤工具，也絕不販售你的資料。",
        "เราเก็บข้อมูลเล็กน้อยไว้ในเบราว์เซอร์ของคุณ เพื่อจดจำตลาดที่คุณเลือก และเพื่อพักข้อมูลสถิติการใช้งานแบบไม่ระบุตัวตนก่อนส่ง ข้อมูลนี้อยู่บนอุปกรณ์ของคุณ และคุณลบได้ทุกเมื่อผ่านการตั้งค่าเบราว์เซอร์":
            "我們會在你的瀏覽器中儲存少量資料，用來記住你選擇的市場，並在傳送前暫存匿名的使用統計。這些資料存放在你的裝置上，你可以隨時透過瀏覽器設定清除。",
        "เราเก็บสถิติการใช้งานแบบไม่ระบุตัวตน เช่น หน้าที่เปิดดูและกิจกรรมที่คลิก เพื่อปรับปรุงรายการให้ดีขึ้น เซิร์ฟเวอร์ของเราบันทึกชนิดเบราว์เซอร์ หน้าที่อ้างอิงเข้ามา และที่อยู่ IP ในรูปแบบที่แปลงเป็นค่าแฮชทางเดียว เราไม่เคยเก็บที่อยู่ IP จริงของคุณ และไม่ระบุตัวตนของคุณเป็นรายบุคคล":
            "我們收集匿名的使用統計——例如哪些頁面被瀏覽、哪些活動被點擊——以改善清單內容。我們的伺服器會記錄瀏覽器類型、來源頁面，以及經單向雜湊處理的 IP 位址。我們絕不儲存你真實的 IP 位址，也不會辨識你的個人身分。",
        "เว็บไซต์นี้ทำงานบนโครงสร้างพื้นฐานของ Cloudflare ซึ่งเป็นผู้ประมวลผลข้อมูลให้เรา เราโฮสต์ฟอนต์ของเราเอง และไม่ใช้ Google Analytics, Meta Pixel หรือเครือข่ายโฆษณาใด ๆ":
            "本網站運行於 Cloudflare 的基礎設施上，由其作為我們的資料處理者。我們自行託管字型，並不使用 Google Analytics、Meta Pixel 或任何廣告聯播網。",
        "หากมีคำถามเกี่ยวกับความเป็นส่วนตัว หรือต้องการให้ลบข้อมูลของคุณ ติดต่อเราได้ที่ info@studyeventz.com":
            "如果你對隱私有任何疑問，或希望刪除你的資料，請來信 info@studyeventz.com",
        # ── Country-specific English copy ──
        "studyeventz is an independent guide to study abroad events in Thailand — fairs, webinars and briefings gathered weekly.":
            "studyeventz is an independent guide to study abroad events in Taiwan — fairs, webinars and briefings gathered weekly.",
        "Submit a study abroad event to studyeventz — university fair, info session, open day, webinar. Free for organizers in Thailand.":
            "Submit a study abroad event to studyeventz — university fair, info session, open day, webinar. Free for organizers in Taiwan.",
        'placeholder=\'e.g. "Bangkok, Thailand" or "Online"\'':
            'placeholder=\'e.g. "Taipei, Taiwan" or "Online"\'',
    },
)

# ─── Hong Kong ───────────────────────────────────────────────────────────────
# Traditional Chinese (zh-Hant, Hong Kong wording — 網上/電郵/逢星期一).
# AI-DRAFTED demo/placeholder pages — flag for native-speaker review before launch.
HONGKONG = Country(
    code="hongkong",
    name_en="Hong Kong",
    name_native="香港",
    flag="🇭🇰",
    primary_lang="zh-Hant",
    iso2="HK",
    agent_db_match="%Hong Kong%",
    timezone="Asia/Hong_Kong",
    title="香港升學及留學活動總覽 | 大學展、網上講座與升學講座 | StudyEventz",
    meta_desc_en=("Find study abroad events in Hong Kong — fairs, webinars and briefings for "
                  "students considering the UK, Australia, USA, Canada and Europe. Updated weekly."),
    meta_desc_native="彙整香港的升學及留學活動——大學展、網上講座與升學講座，每週更新。",
    contact_email="info@studyeventz.com",
    notify_channel="email",
    notify_text_native="每週掌握最新升學活動 → 電郵通知我們",
    translations={
        # ── Events page ──
        "รวมอีเวนต์เรียนต่อต่างประเทศในไทย": "香港留學活動總覽",
        "รวมงานแฟร์มหาวิทยาลัย เวบินาร์ และกิจกรรมเรียนต่อต่างประเทศไว้ในที่เดียว":
            "大學展、網上講座與留學活動，一站盡覽。",
        "อัปเดตทุกสัปดาห์ พร้อมอีเวนต์ในอีก 30 วันข้างหน้า":
            "每週更新，涵蓋未來 30 天的活動。",
        "ตัวกรอง": "篩選",
        "studyeventz รวบรวมงาน study abroad จากบริษัทแนะแนวทั่วประเทศไทย อัปเดตทุกวันจันทร์":
            "studyeventz 彙整全香港升學顧問公司的留學活動，逢星期一更新。",
        # ── About page ──
        "studyeventz เป็นคู่มืออิสระสำหรับค้นหากิจกรรมเรียนต่อต่างประเทศในไทย":
            "studyeventz 是協助你尋找香港留學活動的獨立指南",
        "เกี่ยวกับเรา": "關於我們",
        "studyeventz เป็นคู่มืออิสระสำหรับค้นหากิจกรรมเรียนต่อต่างประเทศ ไม่ว่าจะเป็นงานแฟร์มหาวิทยาลัย วันให้ข้อมูล Open Day หรือกำหนดปิดรับสมัครทุนการศึกษา โดยรวบรวมไว้ในที่เดียว และอัปเดตทุกสัปดาห์":
            "studyeventz 是協助你尋找留學活動的獨立指南——不論是大學展、開放日（Open Day）還是獎學金截止日期——全部彙整於一處，並每週更新。",
        "ปกติแล้ว การหากิจกรรมเหล่านี้ต้องใช้เวลาค้นหาจาก Facebook หลายสิบเพจ เว็บไซต์เอเจนซี่ และปฏิทินกิจกรรมของมหาวิทยาลัยต่าง ๆ แต่เราเป็นคนทำงานนั้นให้โดยอัตโนมัติ ทุกสัปดาห์ เรารวบรวมกิจกรรมจากบริษัทแนะแนวการศึกษาและพาร์ตเนอร์มหาวิทยาลัยทั่วตลาด ตรวจสอบและลบข้อมูลซ้ำ แล้วเผยแพร่เป็นรายการกิจกรรมที่สะอาด ชัดเจน และเชื่อถือได้":
            "一般而言，要找到這些活動，你得翻遍數十個 Facebook 專頁、各家顧問公司的網站，以及不同大學的活動行事曆。我們將這項工作自動化：每週彙整全市場升學顧問公司與大學夥伴的活動，核實並去除重複，再發佈成一份乾淨、清晰、值得信賴的活動清單。",
        "เราเริ่มต้นจากประเทศไทย ซึ่งในแต่ละปีมีงานเรียนต่อต่างประเทศหลายร้อยงาน แต่ยังไม่มีศูนย์กลางเดียวสำหรับค้นหาข้อมูลเหล่านี้ เราเป็นแพลตฟอร์มอิสระ ไม่ได้เป็นตัวแทนของมหาวิทยาลัยหรือเอเจนซี่ใดเป็นพิเศษ ดังนั้นสิ่งที่คุณเห็นคือภาพรวมของตัวเลือกที่หลากหลาย ไม่ใช่การนำเสนอจากบริษัทใดบริษัทหนึ่งเท่านั้น":
            "我們始於泰國——當地每年有數百場留學活動，卻沒有一個集中查詢的平台。我們是獨立平台，不代表任何特定大學或顧問公司，因此你看到的是多元選擇的全貌，而非單一公司的推銷。",
        "สนใจนำ studyeventz ไปใช้ในตลาดของคุณหรือไม่? เรายินดีพูดคุยกับคุณครับ/ค่ะ":
            "想把 studyeventz 帶到你的市場嗎？我們很樂意與你聊聊。",
        # ── Contact page ──
        "ติดต่อ studyeventz เพื่อแจ้งเพิ่มงาน แจ้งแก้ไขข้อมูล หรือร่วมงานกับเรา":
            "聯絡 studyeventz：新增活動、回報資料更正，或與我們合作",
        "ติดต่อเรา": "聯絡我們",
        "มีงานที่เราควรเพิ่มในรายการ พบข้อมูลที่ล้าสมัย หรืออยากร่วมงานกับเราใช่ไหม? อีเมลหาเราได้ที่ ":
            "有想讓我們收錄的活動、發現過時的資訊，或想與我們合作嗎？歡迎電郵：",
        " แล้วเราจะติดต่อกลับไป": "，我們會盡快回覆你。",
        "แจ้งเพิ่มกิจกรรม": "新增活動",
        "หากคุณกำลังจัดงานแฟร์เรียนต่อต่างประเทศ Open Day หรืองานให้ข้อมูล ส่งรายละเอียดมาให้เรา แล้วเราจะเพิ่มลงในรายการ":
            "如果你正在籌辦留學展、開放日或說明會，把詳情寄給我們，我們就會加入清單。",
        "ส่งงานเข้ามา": "提交活動",
        "แจ้งแก้ไขข้อมูล": "回報資料更正",
        "พบวันที่ผิด หรือลิงก์ใช้งานไม่ได้ใช่ไหม? แจ้งให้เราทราบ แล้วเราจะรีบแก้ไขให้":
            "發現日期有誤或連結失效？告訴我們，我們會盡快修正。",
        "ความร่วมมือ": "合作",
        "หากคุณสนใจนำ studyeventz ไปเปิดในตลาดใหม่ หรืออยากร่วมมือกับเราในตลาดที่เราครอบคลุมอยู่แล้ว ติดต่อเราได้เลย":
            "如果你有意把 studyeventz 帶入新市場，或想在我們已涵蓋的市場與我們合作，歡迎聯絡我們。",
        # ── Submit page ──
        "แจ้งเพิ่มกิจกรรมเรียนต่อต่างประเทศใน studyeventz":
            "向 studyeventz 提交留學活動",
        "กรอกรายละเอียดด้านล่าง เราจะตรวจสอบและเพิ่มลงในรายการของเรา ฟรี ไม่มีค่าใช้จ่าย":
            "填寫以下資料，我們會審核並加入清單。完全免費。",
        "รายละเอียดกิจกรรม": "活動詳情",
        "ผู้จัด": "主辦機構",
        "ชื่อกิจกรรม": "活動名稱",
        "วันที่": "日期",
        "เวลา": "時間",
        "สถานที่": "地點",
        "ลิงก์ลงทะเบียน": "報名連結",
        "ข้อมูลผู้แจ้ง": "提交者資料",
        "ชื่อ": "姓名",
        "อีเมล": "電郵",
        "หมายเหตุเพิ่มเติม": "其他備註",
        "ส่ง": "提交",
        "ขอบคุณค่ะ": "多謝！",
        # ── Privacy page (AI-drafted, review before launch) ──
        "นโยบายความเป็นส่วนตัว": "私隱政策",
        "studyeventz ให้ความสำคัญกับความเป็นส่วนตัวของคุณ เราไม่ใช้คุกกี้ ไม่ใช้ตัวติดตามเพื่อการโฆษณา และไม่ขายข้อมูลของคุณ":
            "studyeventz 重視你的私隱。我們不使用 Cookie、不使用廣告追蹤工具，亦絕不出售你的資料。",
        "เราเก็บข้อมูลเล็กน้อยไว้ในเบราว์เซอร์ของคุณ เพื่อจดจำตลาดที่คุณเลือก และเพื่อพักข้อมูลสถิติการใช้งานแบบไม่ระบุตัวตนก่อนส่ง ข้อมูลนี้อยู่บนอุปกรณ์ของคุณ และคุณลบได้ทุกเมื่อผ่านการตั้งค่าเบราว์เซอร์":
            "我們會在你的瀏覽器中儲存少量資料，用以記住你所選的市場，並在傳送前暫存匿名的使用統計。這些資料存放於你的裝置上，你可隨時透過瀏覽器設定清除。",
        "เราเก็บสถิติการใช้งานแบบไม่ระบุตัวตน เช่น หน้าที่เปิดดูและกิจกรรมที่คลิก เพื่อปรับปรุงรายการให้ดีขึ้น เซิร์ฟเวอร์ของเราบันทึกชนิดเบราว์เซอร์ หน้าที่อ้างอิงเข้ามา และที่อยู่ IP ในรูปแบบที่แปลงเป็นค่าแฮชทางเดียว เราไม่เคยเก็บที่อยู่ IP จริงของคุณ และไม่ระบุตัวตนของคุณเป็นรายบุคคล":
            "我們收集匿名的使用統計——例如哪些頁面被瀏覽、哪些活動被點擊——以改善清單內容。我們的伺服器會記錄瀏覽器類型、來源頁面，以及經單向雜湊處理的 IP 位址。我們絕不儲存你真實的 IP 位址，亦不會識別你的個人身分。",
        "เว็บไซต์นี้ทำงานบนโครงสร้างพื้นฐานของ Cloudflare ซึ่งเป็นผู้ประมวลผลข้อมูลให้เรา เราโฮสต์ฟอนต์ของเราเอง และไม่ใช้ Google Analytics, Meta Pixel หรือเครือข่ายโฆษณาใด ๆ":
            "本網站運行於 Cloudflare 的基礎設施上，由其作為我們的資料處理者。我們自行寄存字型，並不使用 Google Analytics、Meta Pixel 或任何廣告聯播網。",
        "หากมีคำถามเกี่ยวกับความเป็นส่วนตัว หรือต้องการให้ลบข้อมูลของคุณ ติดต่อเราได้ที่ info@studyeventz.com":
            "如果你對私隱有任何疑問，或希望刪除你的資料，請電郵 info@studyeventz.com",
        # ── Country-specific English copy ──
        "studyeventz is an independent guide to study abroad events in Thailand — fairs, webinars and briefings gathered weekly.":
            "studyeventz is an independent guide to study abroad events in Hong Kong — fairs, webinars and briefings gathered weekly.",
        "Submit a study abroad event to studyeventz — university fair, info session, open day, webinar. Free for organizers in Thailand.":
            "Submit a study abroad event to studyeventz — university fair, info session, open day, webinar. Free for organizers in Hong Kong.",
        'placeholder=\'e.g. "Bangkok, Thailand" or "Online"\'':
            'placeholder=\'e.g. "Causeway Bay, Hong Kong" or "Online"\'',
    },
)

# ─── Indonesia ───────────────────────────────────────────────────────────────
# Bahasa Indonesia (id). AI-DRAFTED demo/placeholder copy — flag for native
# review before launch.
INDONESIA = Country(
    code="indonesia",
    name_en="Indonesia",
    name_native="Indonesia",
    flag="🇮🇩",
    primary_lang="id",
    iso2="ID",
    agent_db_match="%Indonesia%",
    timezone="Asia/Jakarta",
    title="Acara Studi ke Luar Negeri di Indonesia | Pameran Universitas & Webinar | StudyEventz",
    meta_desc_en=("Find study abroad events in Indonesia — fairs, webinars and briefings for "
                  "students considering the UK, Australia, USA, Canada and Europe. Updated weekly."),
    meta_desc_native="Temukan acara studi ke luar negeri di Indonesia — pameran, webinar, dan sesi informasi. Diperbarui setiap minggu.",
    contact_email="info@studyeventz.com",
    notify_channel="email",
    notify_text_native="Dapatkan info acara terbaru setiap minggu → email kami",
    local_filter_label="Jakarta",
    local_filter_match="jakarta",
    translations={
        # ── Events page ──
        "รวมอีเวนต์เรียนต่อต่างประเทศในไทย": "Kumpulan acara studi ke luar negeri di Indonesia",
        "รวมงานแฟร์มหาวิทยาลัย เวบินาร์ และกิจกรรมเรียนต่อต่างประเทศไว้ในที่เดียว":
            "Pameran universitas, webinar, dan acara studi ke luar negeri — semua dalam satu tempat.",
        "อัปเดตทุกสัปดาห์ พร้อมอีเวนต์ในอีก 30 วันข้างหน้า":
            "Diperbarui setiap minggu dengan acara dalam 30 hari ke depan.",
        "ตัวกรอง": "Filter",
        "studyeventz รวบรวมงาน study abroad จากบริษัทแนะแนวทั่วประเทศไทย อัปเดตทุกวันจันทร์":
            "studyeventz mengumpulkan acara studi ke luar negeri dari konsultan pendidikan di seluruh Indonesia. Diperbarui setiap Senin.",
        # ── About page ──
        "studyeventz เป็นคู่มืออิสระสำหรับค้นหากิจกรรมเรียนต่อต่างประเทศในไทย":
            "studyeventz adalah panduan independen untuk menemukan acara studi ke luar negeri di Indonesia",
        "เกี่ยวกับเรา": "Tentang Kami",
        "studyeventz เป็นคู่มืออิสระสำหรับค้นหากิจกรรมเรียนต่อต่างประเทศ ไม่ว่าจะเป็นงานแฟร์มหาวิทยาลัย วันให้ข้อมูล Open Day หรือกำหนดปิดรับสมัครทุนการศึกษา โดยรวบรวมไว้ในที่เดียว และอัปเดตทุกสัปดาห์":
            "studyeventz adalah panduan independen untuk menemukan acara studi ke luar negeri — pameran universitas, hari informasi, open day, dan tenggat beasiswa — dikumpulkan dalam satu tempat dan diperbarui setiap minggu.",
        "ปกติแล้ว การหากิจกรรมเหล่านี้ต้องใช้เวลาค้นหาจาก Facebook หลายสิบเพจ เว็บไซต์เอเจนซี่ และปฏิทินกิจกรรมของมหาวิทยาลัยต่าง ๆ แต่เราเป็นคนทำงานนั้นให้โดยอัตโนมัติ ทุกสัปดาห์ เรารวบรวมกิจกรรมจากบริษัทแนะแนวการศึกษาและพาร์ตเนอร์มหาวิทยาลัยทั่วตลาด ตรวจสอบและลบข้อมูลซ้ำ แล้วเผยแพร่เป็นรายการกิจกรรมที่สะอาด ชัดเจน และเชื่อถือได้":
            "Biasanya, menemukan acara seperti ini berarti menyisir puluhan halaman Facebook, situs konsultan, dan kalender acara berbagai universitas. Kami melakukannya secara otomatis: setiap minggu kami mengumpulkan acara dari konsultan pendidikan dan mitra universitas di seluruh pasar, memeriksa dan menghapus duplikat, lalu menerbitkan daftar yang bersih, jelas, dan dapat diandalkan.",
        "เราเริ่มต้นจากประเทศไทย ซึ่งในแต่ละปีมีงานเรียนต่อต่างประเทศหลายร้อยงาน แต่ยังไม่มีศูนย์กลางเดียวสำหรับค้นหาข้อมูลเหล่านี้ เราเป็นแพลตฟอร์มอิสระ ไม่ได้เป็นตัวแทนของมหาวิทยาลัยหรือเอเจนซี่ใดเป็นพิเศษ ดังนั้นสิ่งที่คุณเห็นคือภาพรวมของตัวเลือกที่หลากหลาย ไม่ใช่การนำเสนอจากบริษัทใดบริษัทหนึ่งเท่านั้น":
            "Kami memulai di Thailand, tempat ratusan acara studi ke luar negeri berlangsung setiap tahun tanpa satu pun tempat terpusat untuk menemukannya. Kami independen — tidak mewakili universitas atau konsultan tertentu, jadi yang Anda lihat adalah gambaran lengkap dari berbagai pilihan, bukan promosi satu perusahaan saja.",
        "สนใจนำ studyeventz ไปใช้ในตลาดของคุณหรือไม่? เรายินดีพูดคุยกับคุณครับ/ค่ะ":
            "Tertarik membawa studyeventz ke pasar Anda? Kami senang berbincang dengan Anda.",
        # ── Contact page ──
        "ติดต่อ studyeventz เพื่อแจ้งเพิ่มงาน แจ้งแก้ไขข้อมูล หรือร่วมงานกับเรา":
            "Hubungi studyeventz untuk menambahkan acara, melaporkan koreksi, atau bekerja sama dengan kami",
        "ติดต่อเรา": "Hubungi Kami",
        "มีงานที่เราควรเพิ่มในรายการ พบข้อมูลที่ล้าสมัย หรืออยากร่วมงานกับเราใช่ไหม? อีเมลหาเราได้ที่ ":
            "Punya acara yang sebaiknya kami tambahkan, menemukan informasi yang sudah usang, atau ingin bekerja sama? Kirim email ke ",
        " แล้วเราจะติดต่อกลับไป": " dan kami akan menghubungi Anda kembali.",
        "แจ้งเพิ่มกิจกรรม": "Tambahkan Acara",
        "หากคุณกำลังจัดงานแฟร์เรียนต่อต่างประเทศ Open Day หรืองานให้ข้อมูล ส่งรายละเอียดมาให้เรา แล้วเราจะเพิ่มลงในรายการ":
            "Jika Anda menyelenggarakan pameran studi ke luar negeri, open day, atau sesi informasi, kirimkan detailnya dan kami akan menambahkannya ke daftar.",
        "ส่งงานเข้ามา": "Kirim Acara",
        "แจ้งแก้ไขข้อมูล": "Laporkan Koreksi",
        "พบวันที่ผิด หรือลิงก์ใช้งานไม่ได้ใช่ไหม? แจ้งให้เราทราบ แล้วเราจะรีบแก้ไขให้":
            "Menemukan tanggal yang salah atau tautan rusak? Beri tahu kami dan kami akan segera memperbaikinya.",
        "ความร่วมมือ": "Kemitraan",
        "หากคุณสนใจนำ studyeventz ไปเปิดในตลาดใหม่ หรืออยากร่วมมือกับเราในตลาดที่เราครอบคลุมอยู่แล้ว ติดต่อเราได้เลย":
            "Jika Anda tertarik membawa studyeventz ke pasar baru, atau bermitra dengan kami di pasar yang sudah kami liput, silakan hubungi kami.",
        # ── Submit page ──
        "แจ้งเพิ่มกิจกรรมเรียนต่อต่างประเทศใน studyeventz":
            "Kirim acara studi ke luar negeri ke studyeventz",
        "กรอกรายละเอียดด้านล่าง เราจะตรวจสอบและเพิ่มลงในรายการของเรา ฟรี ไม่มีค่าใช้จ่าย":
            "Isi detail di bawah ini. Kami akan meninjau dan menambahkannya ke daftar. Gratis untuk penyelenggara.",
        "รายละเอียดกิจกรรม": "Detail Acara",
        "ผู้จัด": "Penyelenggara",
        "ชื่อกิจกรรม": "Nama Acara",
        "วันที่": "Tanggal",
        "เวลา": "Waktu",
        "สถานที่": "Lokasi",
        "ลิงก์ลงทะเบียน": "Tautan Pendaftaran",
        "ข้อมูลผู้แจ้ง": "Informasi Pengirim",
        "ชื่อ": "Nama",
        "อีเมล": "Email",
        "หมายเหตุเพิ่มเติม": "Catatan Tambahan",
        "ส่ง": "Kirim",
        "ขอบคุณค่ะ": "Terima kasih!",
        # ── Privacy page (AI-drafted, review before launch) ──
        "นโยบายความเป็นส่วนตัว": "Kebijakan Privasi",
        "studyeventz ให้ความสำคัญกับความเป็นส่วนตัวของคุณ เราไม่ใช้คุกกี้ ไม่ใช้ตัวติดตามเพื่อการโฆษณา และไม่ขายข้อมูลของคุณ":
            "studyeventz menghormati privasi Anda. Kami tidak menggunakan cookie, tidak menggunakan pelacak iklan, dan tidak pernah menjual data Anda.",
        "เราเก็บข้อมูลเล็กน้อยไว้ในเบราว์เซอร์ของคุณ เพื่อจดจำตลาดที่คุณเลือก และเพื่อพักข้อมูลสถิติการใช้งานแบบไม่ระบุตัวตนก่อนส่ง ข้อมูลนี้อยู่บนอุปกรณ์ของคุณ และคุณลบได้ทุกเมื่อผ่านการตั้งค่าเบราว์เซอร์":
            "Kami menyimpan sedikit data di browser Anda untuk mengingat pasar yang Anda pilih dan untuk menampung statistik penggunaan anonim sebelum dikirim. Data ini tetap di perangkat Anda dan dapat Anda hapus kapan saja melalui pengaturan browser.",
        "เราเก็บสถิติการใช้งานแบบไม่ระบุตัวตน เช่น หน้าที่เปิดดูและกิจกรรมที่คลิก เพื่อปรับปรุงรายการให้ดีขึ้น เซิร์ฟเวอร์ของเราบันทึกชนิดเบราว์เซอร์ หน้าที่อ้างอิงเข้ามา และที่อยู่ IP ในรูปแบบที่แปลงเป็นค่าแฮชทางเดียว เราไม่เคยเก็บที่อยู่ IP จริงของคุณ และไม่ระบุตัวตนของคุณเป็นรายบุคคล":
            "Kami mengumpulkan statistik penggunaan anonim — seperti halaman yang dilihat dan acara yang diklik — untuk menyempurnakan daftar. Server kami mencatat jenis browser, halaman perujuk, dan alamat IP Anda dalam bentuk hash satu arah. Kami tidak pernah menyimpan alamat IP asli Anda dan tidak mengidentifikasi Anda secara pribadi.",
        "เว็บไซต์นี้ทำงานบนโครงสร้างพื้นฐานของ Cloudflare ซึ่งเป็นผู้ประมวลผลข้อมูลให้เรา เราโฮสต์ฟอนต์ของเราเอง และไม่ใช้ Google Analytics, Meta Pixel หรือเครือข่ายโฆษณาใด ๆ":
            "Situs ini berjalan di infrastruktur Cloudflare, yang bertindak sebagai pemroses data kami. Kami meng-host font kami sendiri, dan tidak menggunakan Google Analytics, Meta Pixel, atau jaringan iklan apa pun.",
        "หากมีคำถามเกี่ยวกับความเป็นส่วนตัว หรือต้องการให้ลบข้อมูลของคุณ ติดต่อเราได้ที่ info@studyeventz.com":
            "Jika Anda memiliki pertanyaan tentang privasi atau ingin data Anda dihapus, hubungi kami di info@studyeventz.com",
        # ── Country-specific English copy ──
        "studyeventz is an independent guide to study abroad events in Thailand — fairs, webinars and briefings gathered weekly.":
            "studyeventz is an independent guide to study abroad events in Indonesia — fairs, webinars and briefings gathered weekly.",
        "Submit a study abroad event to studyeventz — university fair, info session, open day, webinar. Free for organizers in Thailand.":
            "Submit a study abroad event to studyeventz — university fair, info session, open day, webinar. Free for organizers in Indonesia.",
        'placeholder=\'e.g. "Bangkok, Thailand" or "Online"\'':
            'placeholder=\'e.g. "Jakarta, Indonesia" or "Online"\'',
    },
)

# ─── Malaysia ────────────────────────────────────────────────────────────────
# Bahasa Melayu (ms). AI-DRAFTED demo/placeholder copy — flag for native review.
MALAYSIA = Country(
    code="malaysia",
    name_en="Malaysia",
    name_native="Malaysia",
    flag="🇲🇾",
    primary_lang="ms",
    iso2="MY",
    agent_db_match="%Malaysia%",
    timezone="Asia/Kuala_Lumpur",
    title="Acara Pengajian ke Luar Negara di Malaysia | Pameran Universiti & Webinar | StudyEventz",
    meta_desc_en=("Find study abroad events in Malaysia — fairs, webinars and briefings for "
                  "students considering the UK, Australia, USA, Canada and Europe. Updated weekly."),
    meta_desc_native="Cari acara pengajian ke luar negara di Malaysia — pameran, webinar dan sesi maklumat. Dikemas kini setiap minggu.",
    contact_email="info@studyeventz.com",
    notify_channel="email",
    notify_text_native="Dapatkan info acara terbaru setiap minggu → e-mel kami",
    local_filter_label="Kuala Lumpur",
    local_filter_match="kuala lumpur",
    translations={
        # ── Events page ──
        "รวมอีเวนต์เรียนต่อต่างประเทศในไทย": "Himpunan acara pengajian ke luar negara di Malaysia",
        "รวมงานแฟร์มหาวิทยาลัย เวบินาร์ และกิจกรรมเรียนต่อต่างประเทศไว้ในที่เดียว":
            "Pameran universiti, webinar dan acara pengajian ke luar negara — semua di satu tempat.",
        "อัปเดตทุกสัปดาห์ พร้อมอีเวนต์ในอีก 30 วันข้างหน้า":
            "Dikemas kini setiap minggu dengan acara dalam 30 hari akan datang.",
        "ตัวกรอง": "Tapis",
        "studyeventz รวบรวมงาน study abroad จากบริษัทแนะแนวทั่วประเทศไทย อัปเดตทุกวันจันทร์":
            "studyeventz menghimpunkan acara pengajian ke luar negara daripada perunding pendidikan di seluruh Malaysia. Dikemas kini setiap Isnin.",
        # ── About page ──
        "studyeventz เป็นคู่มืออิสระสำหรับค้นหากิจกรรมเรียนต่อต่างประเทศในไทย":
            "studyeventz ialah panduan bebas untuk mencari acara pengajian ke luar negara di Malaysia",
        "เกี่ยวกับเรา": "Tentang Kami",
        "studyeventz เป็นคู่มืออิสระสำหรับค้นหากิจกรรมเรียนต่อต่างประเทศ ไม่ว่าจะเป็นงานแฟร์มหาวิทยาลัย วันให้ข้อมูล Open Day หรือกำหนดปิดรับสมัครทุนการศึกษา โดยรวบรวมไว้ในที่เดียว และอัปเดตทุกสัปดาห์":
            "studyeventz ialah panduan bebas untuk mencari acara pengajian ke luar negara — pameran universiti, hari maklumat, open day dan tarikh tutup biasiswa — dihimpunkan di satu tempat dan dikemas kini setiap minggu.",
        "ปกติแล้ว การหากิจกรรมเหล่านี้ต้องใช้เวลาค้นหาจาก Facebook หลายสิบเพจ เว็บไซต์เอเจนซี่ และปฏิทินกิจกรรมของมหาวิทยาลัยต่าง ๆ แต่เราเป็นคนทำงานนั้นให้โดยอัตโนมัติ ทุกสัปดาห์ เรารวบรวมกิจกรรมจากบริษัทแนะแนวการศึกษาและพาร์ตเนอร์มหาวิทยาลัยทั่วตลาด ตรวจสอบและลบข้อมูลซ้ำ แล้วเผยแพร่เป็นรายการกิจกรรมที่สะอาด ชัดเจน และเชื่อถือได้":
            "Lazimnya, mencari acara seperti ini bermakna menyelongkar berpuluh halaman Facebook, laman web perunding dan kalendar acara pelbagai universiti. Kami melakukannya secara automatik: setiap minggu kami mengumpulkan acara daripada perunding pendidikan dan rakan universiti di seluruh pasaran, menyemak dan membuang pertindihan, lalu menerbitkan senarai yang kemas, jelas dan boleh dipercayai.",
        "เราเริ่มต้นจากประเทศไทย ซึ่งในแต่ละปีมีงานเรียนต่อต่างประเทศหลายร้อยงาน แต่ยังไม่มีศูนย์กลางเดียวสำหรับค้นหาข้อมูลเหล่านี้ เราเป็นแพลตฟอร์มอิสระ ไม่ได้เป็นตัวแทนของมหาวิทยาลัยหรือเอเจนซี่ใดเป็นพิเศษ ดังนั้นสิ่งที่คุณเห็นคือภาพรวมของตัวเลือกที่หลากหลาย ไม่ใช่การนำเสนอจากบริษัทใดบริษัทหนึ่งเท่านั้น":
            "Kami bermula di Thailand, tempat ratusan acara pengajian ke luar negara berlangsung setiap tahun tanpa satu pusat tunggal untuk mencarinya. Kami bebas — tidak mewakili mana-mana universiti atau perunding tertentu, jadi apa yang anda lihat ialah gambaran penuh pelbagai pilihan, bukan promosi satu syarikat sahaja.",
        "สนใจนำ studyeventz ไปใช้ในตลาดของคุณหรือไม่? เรายินดีพูดคุยกับคุณครับ/ค่ะ":
            "Berminat membawa studyeventz ke pasaran anda? Kami ingin mendengar daripada anda.",
        # ── Contact page ──
        "ติดต่อ studyeventz เพื่อแจ้งเพิ่มงาน แจ้งแก้ไขข้อมูล หรือร่วมงานกับเรา":
            "Hubungi studyeventz untuk menambah acara, melaporkan pembetulan, atau bekerjasama dengan kami",
        "ติดต่อเรา": "Hubungi Kami",
        "มีงานที่เราควรเพิ่มในรายการ พบข้อมูลที่ล้าสมัย หรืออยากร่วมงานกับเราใช่ไหม? อีเมลหาเราได้ที่ ":
            "Ada acara yang patut kami tambah, menemui maklumat lapuk, atau ingin bekerjasama dengan kami? E-mel kami di ",
        " แล้วเราจะติดต่อกลับไป": " dan kami akan menghubungi anda semula.",
        "แจ้งเพิ่มกิจกรรม": "Tambah Acara",
        "หากคุณกำลังจัดงานแฟร์เรียนต่อต่างประเทศ Open Day หรืองานให้ข้อมูล ส่งรายละเอียดมาให้เรา แล้วเราจะเพิ่มลงในรายการ":
            "Jika anda menganjurkan pameran pengajian ke luar negara, open day atau sesi maklumat, hantarkan butirannya dan kami akan menambahnya ke senarai.",
        "ส่งงานเข้ามา": "Hantar Acara",
        "แจ้งแก้ไขข้อมูล": "Laporkan Pembetulan",
        "พบวันที่ผิด หรือลิงก์ใช้งานไม่ได้ใช่ไหม? แจ้งให้เราทราบ แล้วเราจะรีบแก้ไขให้":
            "Menemui tarikh yang salah atau pautan rosak? Beritahu kami dan kami akan membaikinya dengan segera.",
        "ความร่วมมือ": "Kerjasama",
        "หากคุณสนใจนำ studyeventz ไปเปิดในตลาดใหม่ หรืออยากร่วมมือกับเราในตลาดที่เราครอบคลุมอยู่แล้ว ติดต่อเราได้เลย":
            "Jika anda berminat membawa studyeventz ke pasaran baharu, atau bekerjasama dengan kami di pasaran yang telah kami liputi, hubungi kami.",
        # ── Submit page ──
        "แจ้งเพิ่มกิจกรรมเรียนต่อต่างประเทศใน studyeventz":
            "Hantar acara pengajian ke luar negara ke studyeventz",
        "กรอกรายละเอียดด้านล่าง เราจะตรวจสอบและเพิ่มลงในรายการของเรา ฟรี ไม่มีค่าใช้จ่าย":
            "Isi butiran di bawah. Kami akan menyemak dan menambahnya ke senarai. Percuma untuk penganjur.",
        "รายละเอียดกิจกรรม": "Butiran Acara",
        "ผู้จัด": "Penganjur",
        "ชื่อกิจกรรม": "Nama Acara",
        "วันที่": "Tarikh",
        "เวลา": "Masa",
        "สถานที่": "Lokasi",
        "ลิงก์ลงทะเบียน": "Pautan Pendaftaran",
        "ข้อมูลผู้แจ้ง": "Maklumat Penghantar",
        "ชื่อ": "Nama",
        "อีเมล": "E-mel",
        "หมายเหตุเพิ่มเติม": "Catatan Tambahan",
        "ส่ง": "Hantar",
        "ขอบคุณค่ะ": "Terima kasih!",
        # ── Privacy page (AI-drafted, review before launch) ──
        "นโยบายความเป็นส่วนตัว": "Dasar Privasi",
        "studyeventz ให้ความสำคัญกับความเป็นส่วนตัวของคุณ เราไม่ใช้คุกกี้ ไม่ใช้ตัวติดตามเพื่อการโฆษณา และไม่ขายข้อมูลของคุณ":
            "studyeventz menghormati privasi anda. Kami tidak menggunakan cookie, tidak menggunakan penjejak iklan, dan tidak sekali-kali menjual data anda.",
        "เราเก็บข้อมูลเล็กน้อยไว้ในเบราว์เซอร์ของคุณ เพื่อจดจำตลาดที่คุณเลือก และเพื่อพักข้อมูลสถิติการใช้งานแบบไม่ระบุตัวตนก่อนส่ง ข้อมูลนี้อยู่บนอุปกรณ์ของคุณ และคุณลบได้ทุกเมื่อผ่านการตั้งค่าเบราว์เซอร์":
            "Kami menyimpan sedikit data dalam pelayar anda untuk mengingati pasaran yang anda pilih dan untuk menyimpan sementara statistik penggunaan tanpa nama sebelum dihantar. Data ini kekal pada peranti anda dan boleh anda padam pada bila-bila masa melalui tetapan pelayar.",
        "เราเก็บสถิติการใช้งานแบบไม่ระบุตัวตน เช่น หน้าที่เปิดดูและกิจกรรมที่คลิก เพื่อปรับปรุงรายการให้ดีขึ้น เซิร์ฟเวอร์ของเราบันทึกชนิดเบราว์เซอร์ หน้าที่อ้างอิงเข้ามา และที่อยู่ IP ในรูปแบบที่แปลงเป็นค่าแฮชทางเดียว เราไม่เคยเก็บที่อยู่ IP จริงของคุณ และไม่ระบุตัวตนของคุณเป็นรายบุคคล":
            "Kami mengumpulkan statistik penggunaan tanpa nama — seperti halaman yang dilihat dan acara yang diklik — untuk menambah baik senarai. Pelayan kami merekod jenis pelayar, halaman perujuk, dan alamat IP anda dalam bentuk hash sehala. Kami tidak sekali-kali menyimpan alamat IP sebenar anda dan tidak mengenal pasti anda secara peribadi.",
        "เว็บไซต์นี้ทำงานบนโครงสร้างพื้นฐานของ Cloudflare ซึ่งเป็นผู้ประมวลผลข้อมูลให้เรา เราโฮสต์ฟอนต์ของเราเอง และไม่ใช้ Google Analytics, Meta Pixel หรือเครือข่ายโฆษณาใด ๆ":
            "Laman ini beroperasi di atas infrastruktur Cloudflare, yang bertindak sebagai pemproses data kami. Kami menghoskan fon kami sendiri, dan tidak menggunakan Google Analytics, Meta Pixel, atau mana-mana rangkaian pengiklanan.",
        "หากมีคำถามเกี่ยวกับความเป็นส่วนตัว หรือต้องการให้ลบข้อมูลของคุณ ติดต่อเราได้ที่ info@studyeventz.com":
            "Jika anda mempunyai sebarang pertanyaan tentang privasi atau ingin data anda dipadam, hubungi kami di info@studyeventz.com",
        # ── Country-specific English copy ──
        "studyeventz is an independent guide to study abroad events in Thailand — fairs, webinars and briefings gathered weekly.":
            "studyeventz is an independent guide to study abroad events in Malaysia — fairs, webinars and briefings gathered weekly.",
        "Submit a study abroad event to studyeventz — university fair, info session, open day, webinar. Free for organizers in Thailand.":
            "Submit a study abroad event to studyeventz — university fair, info session, open day, webinar. Free for organizers in Malaysia.",
        'placeholder=\'e.g. "Bangkok, Thailand" or "Online"\'':
            'placeholder=\'e.g. "Kuala Lumpur, Malaysia" or "Online"\'',
    },
)

# ─── Ghana ───────────────────────────────────────────────────────────────────
# English-native market: english_only drops the bilingual native lines, leaving
# clean single-language English. No translation map needed.
GHANA = Country(
    code="ghana",
    name_en="Ghana",
    name_native="Ghana",
    flag="🇬🇭",
    primary_lang="en",
    iso2="GH",
    agent_db_match="%Ghana%",
    timezone="Africa/Accra",
    title="Study Abroad Events in Ghana | University Fairs & Webinars | StudyEventz",
    meta_desc_en=("Find study abroad events in Ghana — fairs, webinars and briefings for "
                  "students considering the UK, USA, Canada, Australia and Europe. Updated weekly."),
    meta_desc_native="",
    contact_email="info@studyeventz.com",
    notify_channel="email",
    notify_text_native="Get notified about new study abroad events every week → email us",
    local_filter_label="Accra",
    local_filter_match="accra",
    english_only=True,
)

# ─── Nigeria ─────────────────────────────────────────────────────────────────
NIGERIA = Country(
    code="nigeria",
    name_en="Nigeria",
    name_native="Nigeria",
    flag="🇳🇬",
    primary_lang="en",
    iso2="NG",
    agent_db_match="%Nigeria%",
    timezone="Africa/Lagos",
    title="Study Abroad Events in Nigeria | University Fairs & Webinars | StudyEventz",
    meta_desc_en=("Find study abroad events in Nigeria — fairs, webinars and briefings for "
                  "students considering the UK, USA, Canada, Australia and Europe. Updated weekly."),
    meta_desc_native="",
    contact_email="info@studyeventz.com",
    notify_channel="email",
    notify_text_native="Get notified about new study abroad events every week → email us",
    local_filter_label="Lagos",
    local_filter_match="lagos",
    english_only=True,
)

# ─── Singapore ───────────────────────────────────────────────────────────────
# English-native city-state: english_only render, no local-city chip.
SINGAPORE = Country(
    code="singapore",
    name_en="Singapore",
    name_native="Singapore",
    flag="🇸🇬",
    primary_lang="en",
    iso2="SG",
    agent_db_match="%Singapore%",
    timezone="Asia/Singapore",
    title="Study Abroad Events in Singapore | University Fairs & Webinars | StudyEventz",
    meta_desc_en=("Find study abroad events in Singapore — fairs, webinars and briefings for "
                  "students considering the UK, USA, Canada, Australia and Europe. Updated weekly."),
    meta_desc_native="",
    contact_email="info@studyeventz.com",
    notify_channel="email",
    notify_text_native="Get notified about new study abroad events every week → email us",
    english_only=True,
)

# ─── Cambodia ────────────────────────────────────────────────────────────────
# Khmer (km). AI-DRAFTED demo/placeholder copy — Khmer is lower-confidence than
# the other localisations, so flag clearly for native review before launch.
CAMBODIA = Country(
    code="cambodia",
    name_en="Cambodia",
    name_native="កម្ពុជា",
    flag="🇰🇭",
    primary_lang="km",
    iso2="KH",
    agent_db_match="%Cambodia%",
    timezone="Asia/Phnom_Penh",
    title="ព្រឹត្តិការណ៍សិក្សានៅបរទេសក្នុងប្រទេសកម្ពុជា | ពិព័រណ៍សាកលវិទ្យាល័យ និងវេបៀណា | StudyEventz",
    meta_desc_en=("Find study abroad events in Cambodia — fairs, webinars and briefings for "
                  "students considering the UK, Australia, USA, Canada and Europe. Updated weekly."),
    meta_desc_native="ស្វែងរកព្រឹត្តិការណ៍សិក្សានៅបរទេសក្នុងប្រទេសកម្ពុជា ធ្វើបច្ចុប្បន្នភាពរៀងរាល់សប្តាហ៍។",
    contact_email="info@studyeventz.com",
    notify_channel="email",
    notify_text_native="ទទួលដំណឹងព្រឹត្តិការណ៍ថ្មីរៀងរាល់សប្តាហ៍ → ផ្ញើអ៊ីមែលមកយើង",
    local_filter_label="Phnom Penh",
    local_filter_match="phnom penh",
    translations={
        # ── Events page ──
        "รวมอีเวนต์เรียนต่อต่างประเทศในไทย": "ព្រឹត្តិការណ៍សិក្សានៅបរទេសក្នុងប្រទេសកម្ពុជា",
        "รวมงานแฟร์มหาวิทยาลัย เวบินาร์ และกิจกรรมเรียนต่อต่างประเทศไว้ในที่เดียว":
            "ពិព័រណ៍សាកលវិទ្យាល័យ វេបៀណា និងព្រឹត្តិការណ៍សិក្សានៅបរទេស ទាំងអស់នៅកន្លែងតែមួយ។",
        "อัปเดตทุกสัปดาห์ พร้อมอีเวนต์ในอีก 30 วันข้างหน้า":
            "ធ្វើបច្ចុប្បន្នភាពរៀងរាល់សប្តាហ៍ ជាមួយព្រឹត្តិការណ៍ក្នុងរយៈពេល៣០ថ្ងៃខាងមុខ។",
        "ตัวกรอง": "តម្រង",
        "studyeventz รวบรวมงาน study abroad จากบริษัทแนะแนวทั่วประเทศไทย อัปเดตทุกวันจันทร์":
            "studyeventz ប្រមូលផ្តុំព្រឹត្តិការណ៍សិក្សានៅបរទេសពីក្រុមហ៊ុនប្រឹក្សាអប់រំទូទាំងប្រទេសកម្ពុជា។ ធ្វើបច្ចុប្បន្នភាពរៀងរាល់ថ្ងៃច័ន្ទ។",
        # ── About page ──
        "studyeventz เป็นคู่มืออิสระสำหรับค้นหากิจกรรมเรียนต่อต่างประเทศในไทย":
            "studyeventz គឺជាមគ្គុទេសក៍ឯករាជ្យសម្រាប់ស្វែងរកព្រឹត្តិការណ៍សិក្សានៅបរទេសក្នុងប្រទេសកម្ពុជា",
        "เกี่ยวกับเรา": "អំពីយើង",
        "studyeventz เป็นคู่มืออิสระสำหรับค้นหากิจกรรมเรียนต่อต่างประเทศ ไม่ว่าจะเป็นงานแฟร์มหาวิทยาลัย วันให้ข้อมูล Open Day หรือกำหนดปิดรับสมัครทุนการศึกษา โดยรวบรวมไว้ในที่เดียว และอัปเดตทุกสัปดาห์":
            "studyeventz គឺជាមគ្គុទេសក៍ឯករាជ្យសម្រាប់ស្វែងរកព្រឹត្តិការណ៍សិក្សានៅបរទេស — មិនថាជាពិព័រណ៍សាកលវិទ្យាល័យ ថ្ងៃផ្តល់ព័ត៌មាន Open Day ឬកាលបរិច្ឆេទផុតកំណត់អាហារូបករណ៍ — ប្រមូលផ្តុំនៅកន្លែងតែមួយ និងធ្វើបច្ចុប្បន្នភាពរៀងរាល់សប្តាហ៍។",
        "ปกติแล้ว การหากิจกรรมเหล่านี้ต้องใช้เวลาค้นหาจาก Facebook หลายสิบเพจ เว็บไซต์เอเจนซี่ และปฏิทินกิจกรรมของมหาวิทยาลัยต่าง ๆ แต่เราเป็นคนทำงานนั้นให้โดยอัตโนมัติ ทุกสัปดาห์ เรารวบรวมกิจกรรมจากบริษัทแนะแนวการศึกษาและพาร์ตเนอร์มหาวิทยาลัยทั่วตลาด ตรวจสอบและลบข้อมูลซ้ำ แล้วเผยแพร่เป็นรายการกิจกรรมที่สะอาด ชัดเจน และเชื่อถือได้":
            "ជាធម្មតា ការស្វែងរកព្រឹត្តិការណ៍ទាំងនេះ មានន័យថាត្រូវរុករកទំព័រ Facebook រាប់សិប គេហទំព័រភ្នាក់ងារ និងប្រតិទិនព្រឹត្តិការណ៍របស់សាកលវិទ្យាល័យផ្សេងៗ។ យើងធ្វើការងារនោះដោយស្វ័យប្រវត្តិ៖ រៀងរាល់សប្តាហ៍ យើងប្រមូលព្រឹត្តិការណ៍ពីក្រុមហ៊ុនប្រឹក្សាអប់រំ និងដៃគូសាកលវិទ្យាល័យទូទាំងទីផ្សារ ផ្ទៀងផ្ទាត់ និងលុបព័ត៌មានស្ទួន រួចបោះពុម្ពផ្សាយជាបញ្ជីដ៏ស្អាត ច្បាស់លាស់ និងគួរឱ្យទុកចិត្ត។",
        "เราเริ่มต้นจากประเทศไทย ซึ่งในแต่ละปีมีงานเรียนต่อต่างประเทศหลายร้อยงาน แต่ยังไม่มีศูนย์กลางเดียวสำหรับค้นหาข้อมูลเหล่านี้ เราเป็นแพลตฟอร์มอิสระ ไม่ได้เป็นตัวแทนของมหาวิทยาลัยหรือเอเจนซี่ใดเป็นพิเศษ ดังนั้นสิ่งที่คุณเห็นคือภาพรวมของตัวเลือกที่หลากหลาย ไม่ใช่การนำเสนอจากบริษัทใดบริษัทหนึ่งเท่านั้น":
            "យើងបានចាប់ផ្តើមនៅប្រទេសថៃ ដែលរៀងរាល់ឆ្នាំមានព្រឹត្តិការណ៍សិក្សានៅបរទេសរាប់រយ ប៉ុន្តែគ្មានកន្លែងកណ្តាលតែមួយសម្រាប់ស្វែងរកវា។ យើងជាវេទិកាឯករាជ្យ — មិនតំណាងឱ្យសាកលវិទ្យាល័យ ឬភ្នាក់ងារណាមួយជាក់លាក់ទេ ដូច្នេះអ្វីដែលអ្នកឃើញ គឺជាទិដ្ឋភាពទូទៅនៃជម្រើសចម្រុះ មិនមែនជាការផ្សព្វផ្សាយរបស់ក្រុមហ៊ុនណាមួយឡើយ។",
        "สนใจนำ studyeventz ไปใช้ในตลาดของคุณหรือไม่? เรายินดีพูดคุยกับคุณครับ/ค่ะ":
            "ចាប់អារម្មណ៍នាំ studyeventz ទៅកាន់ទីផ្សាររបស់អ្នកមែនទេ? យើងរីករាយក្នុងការពិភាក្សាជាមួយអ្នក។",
        # ── Contact page ──
        "ติดต่อ studyeventz เพื่อแจ้งเพิ่มงาน แจ้งแก้ไขข้อมูล หรือร่วมงานกับเรา":
            "ទាក់ទង studyeventz ដើម្បីបន្ថែមព្រឹត្តិការណ៍ រាយការណ៍កំហុស ឬសហការជាមួយយើង",
        "ติดต่อเรา": "ទាក់ទងយើង",
        "มีงานที่เราควรเพิ่มในรายการ พบข้อมูลที่ล้าสมัย หรืออยากร่วมงานกับเราใช่ไหม? อีเมลหาเราได้ที่ ":
            "មានព្រឹត្តិការណ៍ដែលយើងគួរបន្ថែម ឃើញព័ត៌មានហួសសម័យ ឬចង់សហការជាមួយយើងមែនទេ? សូមផ្ញើអ៊ីមែលមកយើងតាម ",
        " แล้วเราจะติดต่อกลับไป": " រួចយើងនឹងទាក់ទងត្រឡប់ទៅវិញ។",
        "แจ้งเพิ่มกิจกรรม": "បន្ថែមព្រឹត្តិការណ៍",
        "หากคุณกำลังจัดงานแฟร์เรียนต่อต่างประเทศ Open Day หรืองานให้ข้อมูล ส่งรายละเอียดมาให้เรา แล้วเราจะเพิ่มลงในรายการ":
            "ប្រសិនបើអ្នកកំពុងរៀបចំពិព័រណ៍សិក្សានៅបរទេស Open Day ឬវគ្គផ្តល់ព័ត៌មាន សូមផ្ញើព័ត៌មានលម្អិតមកយើង រួចយើងនឹងបន្ថែមវាទៅក្នុងបញ្ជី។",
        "ส่งงานเข้ามา": "ដាក់ស្នើព្រឹត្តិការណ៍",
        "แจ้งแก้ไขข้อมูล": "រាយការណ៍កំហុសព័ត៌មាន",
        "พบวันที่ผิด หรือลิงก์ใช้งานไม่ได้ใช่ไหม? แจ้งให้เราทราบ แล้วเราจะรีบแก้ไขให้":
            "ឃើញកាលបរិច្ឆេទខុស ឬតំណភ្ជាប់ខូចមែនទេ? សូមប្រាប់យើង រួចយើងនឹងកែតម្រូវវាភ្លាមៗ។",
        "ความร่วมมือ": "ភាពជាដៃគូ",
        "หากคุณสนใจนำ studyeventz ไปเปิดในตลาดใหม่ หรืออยากร่วมมือกับเราในตลาดที่เราครอบคลุมอยู่แล้ว ติดต่อเราได้เลย":
            "ប្រសិនបើអ្នកចាប់អារម្មណ៍នាំ studyeventz ទៅកាន់ទីផ្សារថ្មី ឬសហការជាមួយយើងនៅទីផ្សារដែលយើងមានវត្តមានរួចហើយ សូមទាក់ទងមកយើង។",
        # ── Submit page ──
        "แจ้งเพิ่มกิจกรรมเรียนต่อต่างประเทศใน studyeventz":
            "ដាក់ស្នើព្រឹត្តិការណ៍សិក្សានៅបរទេសទៅ studyeventz",
        "กรอกรายละเอียดด้านล่าง เราจะตรวจสอบและเพิ่มลงในรายการของเรา ฟรี ไม่มีค่าใช้จ่าย":
            "បំពេញព័ត៌មានលម្អិតខាងក្រោម។ យើងនឹងពិនិត្យ និងបន្ថែមវាទៅក្នុងបញ្ជី។ ឥតគិតថ្លៃសម្រាប់អ្នករៀបចំ។",
        "รายละเอียดกิจกรรม": "ព័ត៌មានលម្អិតព្រឹត្តិការណ៍",
        "ผู้จัด": "អ្នករៀបចំ",
        "ชื่อกิจกรรม": "ឈ្មោះព្រឹត្តិការណ៍",
        "วันที่": "កាលបរិច្ឆេទ",
        "เวลา": "ម៉ោង",
        "สถานที่": "ទីតាំង",
        "ลิงก์ลงทะเบียน": "តំណចុះឈ្មោះ",
        "ข้อมูลผู้แจ้ง": "ព័ត៌មានអ្នកដាក់ស្នើ",
        "ชื่อ": "ឈ្មោះ",
        "อีเมล": "អ៊ីមែល",
        "หมายเหตุเพิ่มเติม": "កំណត់សម្គាល់បន្ថែម",
        "ส่ง": "ផ្ញើ",
        "ขอบคุณค่ะ": "សូមអរគុណ!",
        # ── Privacy page (AI-drafted, review before launch) ──
        "นโยบายความเป็นส่วนตัว": "គោលការណ៍ឯកជនភាព",
        "studyeventz ให้ความสำคัญกับความเป็นส่วนตัวของคุณ เราไม่ใช้คุกกี้ ไม่ใช้ตัวติดตามเพื่อการโฆษณา และไม่ขายข้อมูลของคุณ":
            "studyeventz គោរពឯកជនភាពរបស់អ្នក។ យើងមិនប្រើខូឃី មិនប្រើឧបករណ៍តាមដានផ្សាយពាណិជ្ជកម្ម ហើយមិនដែលលក់ទិន្នន័យរបស់អ្នកឡើយ។",
        "เราเก็บข้อมูลเล็กน้อยไว้ในเบราว์เซอร์ของคุณ เพื่อจดจำตลาดที่คุณเลือก และเพื่อพักข้อมูลสถิติการใช้งานแบบไม่ระบุตัวตนก่อนส่ง ข้อมูลนี้อยู่บนอุปกรณ์ของคุณ และคุณลบได้ทุกเมื่อผ่านการตั้งค่าเบราว์เซอร์":
            "យើងរក្សាទុកទិន្នន័យតិចតួចនៅក្នុងកម្មវិធីរុករករបស់អ្នក ដើម្បីចងចាំទីផ្សារដែលអ្នកបានជ្រើសរើស និងដើម្បីផ្ទុកស្ថិតិការប្រើប្រាស់អនាមិកមុនពេលផ្ញើ។ ទិន្នន័យនេះស្ថិតនៅលើឧបករណ៍របស់អ្នក ហើយអ្នកអាចលុបវាបានគ្រប់ពេលតាមរយៈការកំណត់កម្មវិធីរុករក។",
        "เราเก็บสถิติการใช้งานแบบไม่ระบุตัวตน เช่น หน้าที่เปิดดูและกิจกรรมที่คลิก เพื่อปรับปรุงรายการให้ดีขึ้น เซิร์ฟเวอร์ของเราบันทึกชนิดเบราว์เซอร์ หน้าที่อ้างอิงเข้ามา และที่อยู่ IP ในรูปแบบที่แปลงเป็นค่าแฮชทางเดียว เราไม่เคยเก็บที่อยู่ IP จริงของคุณ และไม่ระบุตัวตนของคุณเป็นรายบุคคล":
            "យើងប្រមូលស្ថិតិការប្រើប្រាស់អនាមិក — ដូចជាទំព័រដែលបានមើល និងព្រឹត្តិការណ៍ដែលបានចុច — ដើម្បីកែលម្អបញ្ជី។ ម៉ាស៊ីនមេរបស់យើងកត់ត្រាប្រភេទកម្មវិធីរុករក ទំព័របញ្ជូន និងអាសយដ្ឋាន IP របស់អ្នកក្នុងទម្រង់ hash មួយទិសដៅ។ យើងមិនដែលរក្សាទុកអាសយដ្ឋាន IP ពិតរបស់អ្នក ហើយមិនកំណត់អត្តសញ្ញាណអ្នកជាលក្ខណៈបុគ្គលឡើយ។",
        "เว็บไซต์นี้ทำงานบนโครงสร้างพื้นฐานของ Cloudflare ซึ่งเป็นผู้ประมวลผลข้อมูลให้เรา เราโฮสต์ฟอนต์ของเราเอง และไม่ใช้ Google Analytics, Meta Pixel หรือเครือข่ายโฆษณาใด ๆ":
            "គេហទំព័រនេះដំណើរការលើហេដ្ឋារចនាសម្ព័ន្ធ Cloudflare ដែលដើរតួជាអ្នកដំណើរការទិន្នន័យរបស់យើង។ យើងបង្ហោះពុម្ពអក្សរផ្ទាល់ខ្លួន ហើយមិនប្រើ Google Analytics, Meta Pixel ឬបណ្តាញផ្សាយពាណិជ្ជកម្មណាមួយឡើយ។",
        "หากมีคำถามเกี่ยวกับความเป็นส่วนตัว หรือต้องการให้ลบข้อมูลของคุณ ติดต่อเราได้ที่ info@studyeventz.com":
            "ប្រសិនបើអ្នកមានសំណួរអំពីឯកជនភាព ឬចង់ឱ្យលុបទិន្នន័យរបស់អ្នក សូមទាក់ទងមកយើងតាម info@studyeventz.com",
        # ── Country-specific English copy ──
        "studyeventz is an independent guide to study abroad events in Thailand — fairs, webinars and briefings gathered weekly.":
            "studyeventz is an independent guide to study abroad events in Cambodia — fairs, webinars and briefings gathered weekly.",
        "Submit a study abroad event to studyeventz — university fair, info session, open day, webinar. Free for organizers in Thailand.":
            "Submit a study abroad event to studyeventz — university fair, info session, open day, webinar. Free for organizers in Cambodia.",
        'placeholder=\'e.g. "Bangkok, Thailand" or "Online"\'':
            'placeholder=\'e.g. "Phnom Penh, Cambodia" or "Online"\'',
    },
)

# Future-ready: appending another Country() launches that market with one build run.
COUNTRIES: list[Country] = [THAILAND, VIETNAM, TAIWAN, HONGKONG, INDONESIA, MALAYSIA,
                            GHANA, NIGERIA, SINGAPORE, CAMBODIA]


# SVG icon paths for the sticky notify banner (24×24 viewBox).
_LINE_ICON_PATH = (
    "M12 3C6.48 3 2 6.62 2 11.07c0 4 3.66 7.34 8.6 7.96.33.07.78.22.9.51.1.26.06.66.03.93 0 0-.12.71-.14.86-.04.26-.2 1.01.88.55 1.09-.46 5.86-3.45 7.99-5.91h.01C21.42 14.31 22 12.77 22 11.07 22 6.62 17.52 3 12 3zM7.92 13.5H6.04a.4.4 0 01-.4-.4V9.34a.4.4 0 11.8 0v3.36h1.48a.4.4 0 110 .8zm1.66-.4a.4.4 0 11-.8 0V9.34a.4.4 0 11.8 0v3.76zm4.4 0a.4.4 0 01-.32.39c-.04.01-.07.01-.11.01a.4.4 0 01-.32-.16l-1.76-2.4v2.16a.4.4 0 11-.8 0V9.34a.4.4 0 01.32-.39c.04-.01.07-.01.11-.01a.4.4 0 01.32.16l1.76 2.4V9.34a.4.4 0 11.8 0v3.76zm2.74-2.28a.4.4 0 110 .8h-1.04v.68h1.04a.4.4 0 110 .8h-1.44a.4.4 0 01-.4-.4V9.34a.4.4 0 01.4-.4h1.44a.4.4 0 110 .8h-1.04v.68h1.04z"
)
_EMAIL_ICON_PATH = (
    "M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z"
)


def render_notify_banner(country: "Country") -> str:
    """Build the sticky bottom banner. LINE markets get a LINE add-friend chip;
    every other market gets an email (mailto) chip. The native CTA text comes
    from country.notify_text_native. For the LINE channel this reproduces the
    original markup byte-for-byte (Thailand's pages are unchanged)."""
    if country.notify_channel == "line":
        return (
            '<aside class="line-banner" role="contentinfo" aria-label="LINE Official Account">\n'
            '  <svg class="line-icon" viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">\n'
            f'    <path d="{_LINE_ICON_PATH}"/>\n'
            '  </svg>\n'
            f'  <span class="line-banner-text">{country.notify_text_native}</span>\n'
            f'  <a id="line-link" href="{country.line_url}" target="_blank" rel="noopener">\n'
            f'    <span class="line-banner-handle">{country.line_handle}</span>\n'
            '  </a>\n'
            '</aside>'
        )
    # Email channel (default for non-LINE markets)
    return (
        '<aside class="line-banner" role="contentinfo" aria-label="Email updates">\n'
        '  <svg class="line-icon" viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">\n'
        f'    <path d="{_EMAIL_ICON_PATH}"/>\n'
        '  </svg>\n'
        f'  <span class="line-banner-text" lang="{country.primary_lang}">{country.notify_text_native}</span>\n'
        f'  <a href="mailto:{country.contact_email}">\n'
        f'    <span class="line-banner-handle">{country.contact_email}</span>\n'
        '  </a>\n'
        '</aside>'
    )


def _english_only(html: str) -> str:
    """Render an English-native market (Ghana, Nigeria, …): strip the bilingual
    native-language (Thai) counterparts, leaving only the English text. The base
    template mixes several bilingual patterns, each handled in order below."""
    TH = r'[฀-๿]'
    # 1. Thai <meta> description — drop the whole tag.
    html = re.sub(r'[ \t]*<meta[^>]*\blang="th"[^>]*>\n?', '', html)
    # 2. Block-level native lines (paired with an English sibling) -> drop.
    html = re.sub(r'[ \t]*<(p|h[1-6])\b[^>]*\blang="th"[^>]*>.*?</\1>\n?', '', html, flags=re.S)
    # 3. site-about Thai line (class="th", no lang attr) -> drop.
    html = re.sub(r'[ \t]*<p class="th">.*?</p>\n?', '', html, flags=re.S)
    # 4. Inline labels "<span ... lang='th'>Thai</span> / English" -> "English".
    html = re.sub(r'<span[^>]*\blang="th"[^>]*>[^<]*</span>\s*/\s*', '', html)
    # 5. Raw inline "Thai / English" (section titles, links, buttons) -> "English".
    #    Stay within one short text run — never cross a tag, quote or newline,
    #    so it can't run past the end of a JS string into later code.
    html = re.sub(TH + r'[^<>/\n\'"]*?\s*/\s*', '', html)
    # 6. Any remaining standalone Thai run (e.g. a trailing JS string) -> drop.
    html = re.sub(r'\s*' + TH + r'+', '', html)
    return html


def localize(html: str, country: "Country") -> str:
    """Apply a market's native-language translations and language tags to a
    rendered template. Longest keys first so a short source string can't clobber
    a longer one it is a substring of. Thailand (empty map, lang 'th') is a
    no-op and its output is unchanged. English-only markets strip the native
    counterparts instead of translating."""
    if country.english_only:
        return _english_only(html)
    for src in sorted(country.translations, key=len, reverse=True):
        html = html.replace(src, country.translations[src])
    if country.primary_lang != "th":
        # Swap content/meta language attributes only — the bracketed CSS
        # selector [lang="th"] (Thai webfont rule) is left untouched, so native
        # text in other scripts simply falls back to the system font stack.
        html = html.replace(' lang="th"', f' lang="{country.primary_lang}"')
    return html

# Tokens to skip when extracting initials from agent names
STOPWORDS_FOR_INITIALS = {"co", "ltd", "the", "and", "pty", "inc", "llc", "corp", "limited"}


def extract_initials(name: str) -> str:
    """Return up to 2 uppercase initials for use in the fallback monogram."""
    if not name:
        return "?"
    words = [w for w in re.findall(r"[A-Za-z]+", name)
             if w.lower() not in STOPWORDS_FOR_INITIALS]
    if not words:
        return (name[:1].upper() or "?")
    # Single-token brands (e.g. "Hkies", "Applyboard") → first two letters,
    # so the monogram is a balanced two-character badge rather than one letter.
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def find_logo(agent_name: str) -> str | None:
    """Look for assets/logos/{agent_name}.png (literal + slug variants).
    Returns an absolute URL (leading /) so it works from any country subdir."""
    if not LOGOS_DIR.exists() or not agent_name:
        return None
    slug = re.sub(r"[^A-Za-z0-9]+", "_", agent_name).strip("_")
    candidates = [f"{agent_name}.png", f"{slug}.png", f"{slug.lower()}.png"]
    seen: set[str] = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if (LOGOS_DIR / cand).exists():
            return f"/assets/logos/{quote(cand)}"
    return None


# Explicit agent → logo mapping. Substring matched against agent_name (lowercase).
# First match wins. Tuple: (substring, logo_path_relative_to_LOGOS_DIR_or_None, initials_override_or_None).
AGENT_LOGO_MAP: list[tuple[str, str | None, str | None]] = [
    ("studyin",            "StudyeventZ logos/studyin-logo.svg",                  None),
    ("aecc",               "StudyeventZ logos/aecc_logo.svg",                     None),
    ("idp",                "StudyeventZ logos/idp-logo.svg",                      None),
    ("one education",      "StudyeventZ logos/One Education Logo - Green.png",    None),
    ("british education",  "StudyeventZ logos/logo-brit-ed.svg",                  None),
    ("brit education",     "StudyeventZ logos/logo-brit-ed.svg",                  None),
    ("brit-ed",            "StudyeventZ logos/logo-brit-ed.svg",                  None),
    ("iec abroad",         "StudyeventZ logos/IEC_Logo-removebg-preview.png",     None),
    ("gouni",              "StudyeventZ logos/GoUni logopng.png",                 None),
    ("go uni",             "StudyeventZ logos/GoUni logopng.png",                 None),
    ("hands on",           "StudyeventZ logos/HandsOn Logo.png",                  None),
    ("hands-on",           "StudyeventZ logos/HandsOn Logo.png",                  None),
    ("mango",              "StudyeventZ logos/Mango.png",                         None),
]

# Logo files that need a coloured background circle behind them.
# Value is the CSS colour to use; None means default (teal, defined in CSS).
LOGOS_NEEDING_BG: dict[str, str | None] = {
    # No bg overrides currently — the new One Education PNG has green baked in.
}


def find_logo_for_agent(agent_name: str) -> tuple[str | None, str | None, bool, str]:
    """Resolve (logo_url, initials_override, needs_bg, bg_color_override) for an agent name.

    bg_color_override is an empty string when the default teal background should
    apply (or when needs_bg is False); otherwise a CSS colour string.

    Order:
    1. AGENT_LOGO_MAP substring match (explicit overrides)
    2. find_logo() literal/slug filename match in LOGOS_DIR root
    """
    if not agent_name:
        return None, None, False, ""
    name_lower = agent_name.lower()
    for substr, logo_rel, initials in AGENT_LOGO_MAP:
        if substr in name_lower:
            if logo_rel is None:
                return None, initials, False, ""
            full = LOGOS_DIR / logo_rel
            if full.exists():
                needs_bg = logo_rel in LOGOS_NEEDING_BG
                bg_color = LOGOS_NEEDING_BG.get(logo_rel) or "" if needs_bg else ""
                return f"/assets/logos/{quote(logo_rel)}", initials, needs_bg, bg_color
            print(
                f"  WARN: mapped logo missing for '{agent_name}': {full}",
                file=sys.stderr,
            )
            return None, initials, False, ""
    return find_logo(agent_name), None, False, ""


def _natural_key(name: str):
    """Sort 'studyeventz 1' before 'studyeventz 10'."""
    parts = re.split(r"(\d+)", name)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def discover_character_images() -> list[str]:
    """Return absolute URLs for each character PNG, in natural order.
    Absolute (leading /) so paths work from any country subdir."""
    if not CHARACTERS_DIR.exists():
        return []
    pngs = sorted(CHARACTERS_DIR.glob("*.png"), key=lambda p: _natural_key(p.name))
    return [f"/assets/characters/{quote(p.name)}" for p in pngs]


def build_event_json_ld(events: list[dict], country: "Country") -> str:
    """Return a JSON array string of schema.org/Event objects, one per event,
    ready to drop into a <script type="application/ld+json"> block."""
    docs = []
    for ev in events:
        is_online = "online" in (ev.get("location") or "").lower()
        location_doc: dict
        if is_online:
            location_doc = {
                "@type": "VirtualLocation",
                "url": country.events_url,
            }
        else:
            location_doc = {
                "@type": "Place",
                "name": ev.get("location") or country.name_en,
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": ev.get("location") or country.name_en,
                    "addressCountry": country.iso2,
                },
            }
        organizer_doc = {"@type": "Organization", "name": ev.get("organizer") or ev.get("agent_name") or "studyeventz"}
        if ev.get("agent_website"):
            organizer_doc["url"] = ev["agent_website"]

        doc = {
            "@context": "https://schema.org",
            "@type": "Event",
            "name": ev.get("name") or "",
            "startDate": ev.get("date") or "",
            "endDate": ev.get("date") or "",
            "eventStatus": "https://schema.org/EventScheduled",
            "eventAttendanceMode": (
                "https://schema.org/OnlineEventAttendanceMode" if is_online
                else "https://schema.org/OfflineEventAttendanceMode"
            ),
            "location": location_doc,
            "organizer": organizer_doc,
            "url": f"{country.events_url}#event-{ev.get('id', '')}",
        }
        if ev.get("registration_url"):
            doc["offers"] = {
                "@type": "Offer",
                "url": ev["registration_url"],
                "availability": "https://schema.org/InStock",
            }
        docs.append(doc)
    return json.dumps(docs, ensure_ascii=False, indent=2)


def write_seo_files() -> None:
    """Write sitemap.xml and robots.txt at the repo root. Enumerates every
    country in COUNTRIES so adding a new market is a one-line change."""
    today = datetime.now().date().isoformat()
    urls: list[str] = []
    # Root (country picker)
    urls.append(
        f"  <url><loc>{SITE_URL}/</loc><lastmod>{today}</lastmod>"
        f"<changefreq>monthly</changefreq><priority>0.8</priority></url>"
    )
    # Per-country pages
    for c in COUNTRIES:
        urls.append(
            f"  <url><loc>{c.events_url}</loc><lastmod>{today}</lastmod>"
            f"<changefreq>weekly</changefreq><priority>1.0</priority></url>"
        )
        urls.append(
            f"  <url><loc>{c.site_url}/about.html</loc><lastmod>{today}</lastmod>"
            f"<changefreq>monthly</changefreq><priority>0.6</priority></url>"
        )
        urls.append(
            f"  <url><loc>{c.site_url}/contact.html</loc><lastmod>{today}</lastmod>"
            f"<changefreq>monthly</changefreq><priority>0.6</priority></url>"
        )
        urls.append(
            f"  <url><loc>{c.site_url}/submit.html</loc><lastmod>{today}</lastmod>"
            f"<changefreq>monthly</changefreq><priority>0.4</priority></url>"
        )
        urls.append(
            f"  <url><loc>{c.site_url}/privacy.html</loc><lastmod>{today}</lastmod>"
            f"<changefreq>yearly</changefreq><priority>0.3</priority></url>"
        )
    SITEMAP_OUT.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls) + "\n</urlset>\n",
        encoding="utf-8",
    )
    ROBOTS_OUT.write_text(
        f"""User-agent: *
Allow: /

Sitemap: {SITE_URL}/sitemap.xml
""",
        encoding="utf-8",
    )


def _normalize_event_name(name: str) -> str:
    """Lowercase, strip punctuation and whitespace for dedup key matching."""
    return re.sub(r"[^a-z0-9ก-๙]+", " ", (name or "").lower()).strip()


def deduplicate_rows(rows: list) -> list:
    """Collapse duplicates:
       - Online events: dedup across agents on (name, date) — multiple agents
         promoting the same webinar should appear once.
       - Physical events: dedup only within the same agent on (name, date) —
         cross-agent listings of in-person fairs/seminars stay separate so
         each agent keeps its own attribution.
       In both cases, keep the row with the most complete location string."""
    groups: dict[tuple, list] = {}
    for r in rows:
        is_online = "online" in (r["location"] or "").lower()
        if is_online:
            key = ("online", _normalize_event_name(r["name"]), r["date"])
        else:
            # Physical events: collapse rows that resolve to the same logo
            # (treats "IDP Education Services Co., Ltd." and "IDP Thailand" as the
            # same agent because they both map to idp-logo.svg). Agents without a
            # logo mapping fall back to exact agent_name.
            logo_url, _, _, _ = find_logo_for_agent(r["agent_name"])
            agent_key = logo_url or r["agent_name"]
            key = ("physical", _normalize_event_name(r["name"]), r["date"], agent_key)
        groups.setdefault(key, []).append(r)

    kept: list = []
    dropped = 0
    for key, group in groups.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        # Sort: longest location first, then lowest id for stability
        best = sorted(
            group,
            key=lambda r: (-len((r["location"] or "").strip()), r["id"]),
        )[0]
        kept.append(best)
        dropped += len(group) - 1
    if dropped:
        print(f"Deduplicated {dropped} event(s) — collapsed duplicates by (name, date).")
    # Preserve SQL ordering (date, time)
    kept.sort(key=lambda r: (r["date"], r["time"] or ""))
    return kept


# How far back the past-events archive reaches. Mirrors ARCHIVE_RETENTION_YEARS
# in scrape_events.py, which is what keeps these rows from being pruned.
ARCHIVE_RETENTION_YEARS = 2


def _row_to_event(r) -> dict:
    """Map a joined events↔agents row to the JSON shape the frontend renders."""
    logo_url, initials_override, needs_bg, bg_color = find_logo_for_agent(r["agent_name"])
    return {
        "id": r["id"],
        "name": r["name"],
        "date": r["date"],
        "time": r["time"] or "",
        "location": r["location"] or "",
        "organizer": r["organizer"] or r["agent_name"],
        "agent_name": r["agent_name"],
        "agent_country": r["agent_country"] or "",
        "agent_website": r["agent_website"] or "",
        "registration_url": r["registration_url"] or "",
        "logo_url": logo_url or "",
        "logo_needs_bg": needs_bg,
        "logo_bg_color": bg_color,
        "initials": initials_override or extract_initials(r["agent_name"]),
    }


def export_events_json(country: "Country") -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    today = datetime.now().date()
    cutoff = today + timedelta(days=30)
    # Past archive reaches back ARCHIVE_RETENTION_YEARS (day=28 guards Feb 29).
    try:
        archive_from = today.replace(year=today.year - ARCHIVE_RETENTION_YEARS)
    except ValueError:
        archive_from = today.replace(year=today.year - ARCHIVE_RETENTION_YEARS, day=28)
    yesterday = today - timedelta(days=1)

    _SELECT = (
        """SELECT e.id, e.name, e.date, e.time, e.location, e.organizer,
                  e.registration_url, a.company_name AS agent_name,
                  a.website AS agent_website, a.country AS agent_country
           FROM events e JOIN agents a ON e.agent_id = a.id
           WHERE e.date BETWEEN ? AND ?
             AND a.country LIKE ?
           ORDER BY e.date, e.time"""
    )

    upcoming_rows = deduplicate_rows(conn.execute(
        _SELECT, (today.isoformat(), cutoff.isoformat(), country.agent_db_match),
    ).fetchall())

    # Past events bounded by the archive retention window. deduplicate_rows
    # returns ascending (date, time); reverse for most-recent-first archive.
    past_rows = deduplicate_rows(conn.execute(
        _SELECT, (archive_from.isoformat(), yesterday.isoformat(), country.agent_db_match),
    ).fetchall())

    events_out = [_row_to_event(r) for r in upcoming_rows]
    past_out = [_row_to_event(r) for r in reversed(past_rows)]

    data = {
        "country":      country.code,
        "country_name": country.name_en,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window":       {"from": today.isoformat(), "to": cutoff.isoformat()},
        "archive":      {"from": archive_from.isoformat(), "to": yesterday.isoformat()},
        "events":       events_out,
        "past":         past_out,
    }

    country.json_out.parent.mkdir(parents=True, exist_ok=True)
    country.json_out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(events_out)


# Legacy fallback SVG silhouettes — only used if no PNGs exist in assets/characters/.
# Kept so the page still renders if the asset directory is missing.
CHARACTER_SVGS = [
    # 0: Student with backpack, standing
    """<svg viewBox="0 0 110 180" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <ellipse cx="55" cy="172" rx="28" ry="4" fill="#000" opacity=".35"/>
      <!-- backpack -->
      <rect x="22" y="68" width="22" height="42" rx="6" fill="#3a4a5c"/>
      <rect x="26" y="78" width="14" height="3" fill="#1a2a3a"/>
      <!-- body -->
      <path d="M40 70 Q55 60 70 70 L74 130 Q55 134 36 130 Z" fill="#dfe6ee"/>
      <!-- arms -->
      <path d="M40 72 L34 110 L38 112 L44 76 Z" fill="#dfe6ee"/>
      <path d="M70 72 L76 110 L72 112 L66 76 Z" fill="#dfe6ee"/>
      <!-- legs -->
      <rect x="44" y="128" width="9" height="38" fill="#5a6b7d"/>
      <rect x="57" y="128" width="9" height="38" fill="#5a6b7d"/>
      <!-- shoes -->
      <ellipse cx="48" cy="168" rx="7" ry="3" fill="#1a2a3a"/>
      <ellipse cx="62" cy="168" rx="7" ry="3" fill="#1a2a3a"/>
      <!-- head -->
      <circle cx="55" cy="48" r="18" fill="#f0d9b5"/>
      <!-- hair -->
      <path d="M37 44 Q40 28 55 26 Q70 28 73 44 Q72 36 65 34 L62 40 Q55 33 48 40 L45 34 Q38 36 37 44 Z" fill="#1a1a1a"/>
      <!-- eyes -->
      <ellipse cx="49" cy="50" rx="1.7" ry="2.4" fill="#1a1a1a"/>
      <ellipse cx="61" cy="50" rx="1.7" ry="2.4" fill="#1a1a1a"/>
      <!-- mouth -->
      <path d="M52 58 Q55 60 58 58" stroke="#1a1a1a" stroke-width="1.2" fill="none" stroke-linecap="round"/>
      <!-- backpack strap front -->
      <path d="M44 72 L42 100" stroke="#1a2a3a" stroke-width="2" fill="none"/>
    </svg>""",
    # 1: Student with laptop, seated/casual
    """<svg viewBox="0 0 110 180" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <ellipse cx="55" cy="172" rx="30" ry="4" fill="#000" opacity=".35"/>
      <!-- head -->
      <circle cx="55" cy="48" r="18" fill="#f0d9b5"/>
      <!-- hair (longer) -->
      <path d="M36 46 Q34 28 55 24 Q76 28 74 46 Q74 38 68 36 Q60 30 50 32 Q40 36 36 46 Z" fill="#2a2a2a"/>
      <path d="M37 48 Q33 60 36 70 L39 56 Z" fill="#2a2a2a"/>
      <!-- eyes (focused down) -->
      <ellipse cx="49" cy="52" rx="1.7" ry="1.5" fill="#1a1a1a"/>
      <ellipse cx="61" cy="52" rx="1.7" ry="1.5" fill="#1a1a1a"/>
      <!-- mouth -->
      <path d="M52 60 L58 60" stroke="#1a1a1a" stroke-width="1.2" stroke-linecap="round"/>
      <!-- body -->
      <path d="M38 70 Q55 64 72 70 L74 124 Q55 128 36 124 Z" fill="#94a3b8"/>
      <!-- arms forward holding laptop -->
      <path d="M38 80 L30 116 L42 118 L46 86 Z" fill="#94a3b8"/>
      <path d="M72 80 L80 116 L68 118 L64 86 Z" fill="#94a3b8"/>
      <!-- laptop -->
      <rect x="28" y="116" width="54" height="14" rx="2" fill="#1a2a3a"/>
      <rect x="30" y="118" width="50" height="10" fill="#5fb8b8"/>
      <!-- laptop base -->
      <path d="M26 130 L84 130 L80 134 L30 134 Z" fill="#3a4a5c"/>
      <!-- legs (seated, short visible) -->
      <rect x="42" y="134" width="11" height="32" fill="#5a6b7d"/>
      <rect x="57" y="134" width="11" height="32" fill="#5a6b7d"/>
      <ellipse cx="47" cy="168" rx="8" ry="3" fill="#1a2a3a"/>
      <ellipse cx="63" cy="168" rx="8" ry="3" fill="#1a2a3a"/>
    </svg>""",
    # 2: Student looking up (head tilted, dreaming)
    """<svg viewBox="0 0 110 180" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <ellipse cx="55" cy="172" rx="28" ry="4" fill="#000" opacity=".35"/>
      <!-- body -->
      <path d="M40 72 Q55 64 70 72 L72 132 Q55 136 38 132 Z" fill="#cbd5e1"/>
      <!-- arms relaxed -->
      <path d="M40 74 L34 124 L40 126 L46 78 Z" fill="#cbd5e1"/>
      <path d="M70 74 L76 124 L70 126 L64 78 Z" fill="#cbd5e1"/>
      <!-- legs -->
      <rect x="44" y="130" width="9" height="36" fill="#475569"/>
      <rect x="57" y="130" width="9" height="36" fill="#475569"/>
      <ellipse cx="48" cy="168" rx="7" ry="3" fill="#1a2a3a"/>
      <ellipse cx="62" cy="168" rx="7" ry="3" fill="#1a2a3a"/>
      <!-- head tilted up + back -->
      <g transform="rotate(-12, 55, 50)">
        <circle cx="55" cy="48" r="18" fill="#f0d9b5"/>
        <!-- spiky hair -->
        <path d="M37 42 Q40 24 55 24 Q70 24 73 42 Q70 30 65 30 L62 38 Q55 28 48 38 L45 30 Q40 30 37 42 Z" fill="#1a1a1a"/>
        <!-- eyes (looking up, oval high) -->
        <ellipse cx="49" cy="46" rx="1.7" ry="2.2" fill="#1a1a1a"/>
        <ellipse cx="61" cy="46" rx="1.7" ry="2.2" fill="#1a1a1a"/>
        <!-- mouth small o -->
        <ellipse cx="55" cy="58" rx="1.5" ry="2" fill="#1a1a1a"/>
      </g>
      <!-- little sparkle for "dreaming" feel -->
      <circle cx="84" cy="34" r="2" fill="#f4a825"/>
      <circle cx="92" cy="42" r="1.5" fill="#f4a825"/>
    </svg>""",
    # 3: Student walking with books
    """<svg viewBox="0 0 110 180" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <ellipse cx="55" cy="172" rx="28" ry="4" fill="#000" opacity=".35"/>
      <!-- head -->
      <circle cx="55" cy="48" r="18" fill="#f0d9b5"/>
      <!-- short hair -->
      <path d="M37 44 Q39 30 55 28 Q71 30 73 44 Q72 36 66 34 Q60 30 55 30 Q50 30 44 34 Q38 36 37 44 Z" fill="#3a2a1a"/>
      <!-- eyes -->
      <ellipse cx="49" cy="50" rx="1.7" ry="2.2" fill="#1a1a1a"/>
      <ellipse cx="61" cy="50" rx="1.7" ry="2.2" fill="#1a1a1a"/>
      <!-- mouth confident -->
      <path d="M50 58 Q55 62 60 58" stroke="#1a1a1a" stroke-width="1.4" fill="none" stroke-linecap="round"/>
      <!-- body -->
      <path d="M40 70 Q55 64 70 70 L72 128 Q55 132 38 128 Z" fill="#e2e8f0"/>
      <!-- left arm holding books to chest -->
      <path d="M40 76 L36 104 L52 108 L52 88 Z" fill="#e2e8f0"/>
      <!-- books -->
      <rect x="38" y="96" width="22" height="16" fill="#0d2233"/>
      <rect x="38" y="100" width="22" height="2" fill="#f4a825"/>
      <rect x="38" y="106" width="22" height="2" fill="#0d7377"/>
      <!-- right arm swinging -->
      <path d="M70 74 L78 102 L74 104 L66 78 Z" fill="#e2e8f0"/>
      <!-- legs (walking, one forward) -->
      <path d="M44 128 L42 168 L52 168 L52 130 Z" fill="#334155"/>
      <path d="M58 130 L62 168 L70 166 L66 128 Z" fill="#334155"/>
      <ellipse cx="47" cy="168" rx="7" ry="3" fill="#1a2a3a"/>
      <ellipse cx="65" cy="167" rx="7" ry="3" fill="#1a2a3a"/>
    </svg>""",
]


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__PAGE_TITLE__</title>
<meta name="description" content="__META_DESC_EN__">
<meta name="description" lang="th" content="__META_DESC_TH__">
<link rel="canonical" href="__EVENTS_PAGE__">

<!-- Open Graph -->
<meta property="og:type" content="website">
<meta property="og:url" content="__SITE_URL__">
<meta property="og:title" content="__PAGE_TITLE__">
<meta property="og:description" content="__META_DESC_EN__">
<meta property="og:image" content="__OG_IMAGE__">
<meta property="og:locale" content="en_US">
<meta property="og:locale:alternate" content="th_TH">

<!-- Twitter card -->
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="__PAGE_TITLE__">
<meta name="twitter:description" content="__META_DESC_EN__">
<meta name="twitter:image" content="__OG_IMAGE__">

<!-- JSON-LD structured data: one Event object per upcoming event -->
<script type="application/ld+json">
__JSON_LD__
</script>

<!-- Thai-optimized typography (Noto Sans Thai pairs cleanly with system Latin fonts) -->
<link rel="stylesheet" href="/assets/fonts/noto-sans-thai.css">
<style>
  :root {
    --teal: #0d7377;
    --teal-dark: #095a5d;
    --gold: #f4a825;
    --ink: #0d2233;
    --bg: #f5f7f9;
    --card: #ffffff;
    --text: #1a2530;
    --muted: #5d6b78;
    --border: #e2e8ed;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         background: var(--bg); color: var(--text); line-height: 1.5; }

  /* ── Header ── */
  .site-header { background: var(--teal); color: #fff; }
  .header-bar { max-width: 1180px; margin: 0 auto; padding: 1rem 1.5rem;
                display: flex; align-items: center; justify-content: space-between;
                gap: 1.5rem; flex-wrap: wrap; }
  .brand { font-size: 1.6rem; font-weight: 800; letter-spacing: -.02em; }
  .brand .gold { color: var(--gold); }
  .brand-link { display: inline-flex; align-items: center; gap: .55rem;
                text-decoration: none; color: inherit;
                transition: opacity .15s; }
  .brand-link:hover { opacity: .85; }
  .brand-flag { font-size: 1.3rem; line-height: 1; }
  .nav { display: flex; gap: .25rem; flex-wrap: wrap; }
  .nav a { color: rgba(255,255,255,.85); text-decoration: none;
           padding: .5rem .9rem; border-radius: 6px; font-size: .9rem; font-weight: 500;
           transition: background .15s, color .15s; }
  .nav a:hover { background: rgba(255,255,255,.12); color: #fff; }
  .nav a.active { background: rgba(255,255,255,.18); color: #fff; font-weight: 600; }

  .hero { background: var(--teal-dark); color: #fff; padding: 2rem 1.5rem 2.2rem; }
  .hero-inner { max-width: 1180px; margin: 0 auto; }
  .hero h1 { font-size: 1.9rem; font-weight: 700; margin-bottom: 0; line-height: 1.2; }
  .hero p { opacity: .9; font-size: .95rem; margin: 0; line-height: 1.45; }
  /* Thai counterparts — same size as the English line they precede, gold colour */
  /* Thai script needs ~1.3× the Latin size + a font with proper Thai metrics
     to appear visually equivalent in weight. */
  [lang="th"] { font-family: "Noto Sans Thai", "Sukhumvit Set", "Leelawadee UI",
                              -apple-system, BlinkMacSystemFont, sans-serif; }
  /* Use .hero p.X selectors so these beat the generic '.hero p' rule above */
  .hero p.hero-th-title { color: var(--gold); font-size: 2.47rem; font-weight: 700;
                          margin: 0; line-height: 1.2; letter-spacing: .01em; opacity: 1; }
  .hero p.hero-th-sub   { color: var(--gold); font-size: 1.15rem; font-weight: 400;
                          margin: 0; line-height: 1.55; opacity: 1; }
  /* Spacing between bilingual pairs */
  .hero-pair { margin-bottom: 1.1rem; }
  .hero-pair:last-child { margin-bottom: 0; }

  /* ── Filters ── */
  .filters { background: #fff; border-bottom: 1px solid var(--border);
             padding: 1rem 1.5rem; position: sticky; top: 0; z-index: 10; }
  .filters-inner { max-width: 1180px; margin: 0 auto;
                   display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; }
  .filter-label { font-size: .75rem; text-transform: uppercase; letter-spacing: .08em;
                  color: var(--muted); margin-right: .5rem; font-weight: 600; }
  .filter-context { font-size: .85rem; color: var(--muted); margin-right: .5rem; }
  .chip { background: #f0f2f5; color: var(--text); border: 1px solid transparent;
          padding: .45rem .95rem; border-radius: 20px; font-size: .85rem;
          font-weight: 500; cursor: pointer; transition: all .15s;
          display: inline-flex; align-items: center; }
  .chip:hover { background: #e5e9ee; }
  .chip.active { background: var(--teal); color: #fff; border-color: var(--teal); }
  .chip .count { opacity: .7; font-size: .75rem; margin-left: .3rem; font-weight: 400; }
  .chip.active .count { opacity: .9; }
  /* Wrap the online-toggle count in parentheses for emphasis */
  #online-toggle .count::before { content: "("; }
  #online-toggle .count::after  { content: ")"; }

  /* Online toggle — separate from the exclusive filter group */
  .chip.toggle { margin-left: auto; border: 1px dashed #b8c2cd;
                 background: #fff; color: var(--muted); }
  .chip.toggle:hover { background: #f0f2f5; }
  .chip.toggle.active { background: var(--gold); color: #fff;
                        border-color: var(--gold); border-style: solid; }
  .filter-divider { width: 1px; align-self: stretch; background: var(--border);
                    margin: 0 .3rem; }
  @media (max-width: 640px) {
    .chip.toggle { margin-left: 0; }
    .filter-divider { display: none; }
  }

  /* ── Main ── */
  main { max-width: 1180px; margin: 0 auto; padding: 1.5rem; }

  /* Week section */
  .week-section { margin-bottom: 2.2rem; }
  .week-header { font-size: .8rem; font-weight: 700; text-transform: uppercase;
                 letter-spacing: .12em; color: var(--muted);
                 padding: .5rem 0 .9rem; border-bottom: 2px solid var(--border);
                 margin-bottom: 1rem; }

  /* ── Event card ── */
  .card { background: var(--card); border-radius: 10px; overflow: hidden;
          box-shadow: 0 1px 3px rgba(13,34,51,.06), 0 1px 2px rgba(13,34,51,.04);
          margin-bottom: 1rem; display: flex; position: relative;
          border-left: 4px solid var(--gold);
          transition: box-shadow .15s, transform .15s; }
  .card:hover { box-shadow: 0 4px 12px rgba(13,34,51,.1), 0 2px 4px rgba(13,34,51,.06);
                transform: translateY(-1px); }

  /* Logo column (left) */
  .logo-col { width: 80px; min-width: 80px; background: #f7f9fa;
              display: flex; align-items: center; justify-content: center;
              padding: .5rem; border-right: 1px solid var(--border); }
  .logo-col img { width: 60px; height: 60px; max-width: 60px; max-height: 60px;
                  object-fit: contain; display: block; }
  .logo-link { display: inline-flex; align-items: center; justify-content: center;
               text-decoration: none; color: inherit; transition: opacity .15s; }
  .logo-link:hover { opacity: .8; }
  /* White-on-transparent logos get a teal circle behind them (same family as initials-avatar) */
  .logo-col img.needs-bg { width: 52px; height: 52px; max-width: 52px; max-height: 52px;
                           background: var(--teal); border-radius: 50%;
                           padding: 4px; object-fit: contain; }
  .initials-avatar {
    width: 52px; height: 52px; border-radius: 50%;
    background: var(--teal); color: #fff;
    display: flex; align-items: center; justify-content: center;
    font-size: 1.05rem; font-weight: 700; letter-spacing: .03em;
    line-height: 1;
  }

  /* Body / details (centre) */
  .body-col { flex: 1; padding: 1.1rem 1.3rem 1.2rem; min-width: 0; }
  .logo-inline { display: none; align-items: center; gap: .55rem; margin-bottom: .4rem; }
  .logo-inline img { max-height: 28px; max-width: 90px; object-fit: contain; display: block; }
  .logo-inline img.needs-bg { width: 28px; height: 28px; max-width: 28px;
                              background: var(--teal); border-radius: 50%; padding: 4px; }
  .logo-inline .initials-avatar { width: 28px; height: 28px; font-size: .72rem; }

  /* Character column (right) */
  .char-col { width: 120px; min-width: 120px; background: var(--ink);
              overflow: hidden; position: relative; }
  .char-col img,
  .char-col svg { width: 100%; height: 100%; object-fit: cover;
                  object-position: top center; display: block;
                  transform: scaleX(-1); /* face left, towards the event details */ }
  .organizer { font-size: .78rem; font-weight: 700; color: var(--teal);
               text-transform: uppercase; letter-spacing: .06em; margin-bottom: .25rem; }
  .event-name { font-size: 1.05rem; font-weight: 700; color: var(--text);
                margin-bottom: .65rem; line-height: 1.35; }
  .meta { display: flex; gap: .4rem; flex-wrap: wrap; margin-bottom: .9rem; }
  .pill { display: inline-flex; align-items: center; gap: .35rem;
          background: #f0f2f5; padding: .3rem .65rem; border-radius: 14px;
          font-size: .78rem; color: var(--muted); }
  .pill svg { width: 12px; height: 12px; flex-shrink: 0; }
  a.pill.pill-link { text-decoration: none; cursor: pointer;
                     transition: background .15s, color .15s; }
  a.pill.pill-link:hover { background: var(--teal); color: #fff; }

  .card-actions { display: flex; gap: .6rem; align-items: center; }
  .btn-register { background: var(--teal); color: #fff; text-decoration: none;
                  padding: .5rem 1.1rem; border-radius: 6px; font-size: .85rem;
                  font-weight: 600; transition: background .15s; display: inline-block; }
  .btn-register:hover { background: var(--teal-dark); }
  .btn-register.disabled { background: #cbd5d8; color: #fff; cursor: not-allowed; pointer-events: none; }

  /* Empty state */
  .empty { background: #fff; border-radius: 10px; padding: 3rem 2rem;
           text-align: center; color: var(--muted);
           border: 1px dashed var(--border); }
  .empty h3 { color: var(--text); margin-bottom: .5rem; font-size: 1.1rem; }

  .footer-meta { color: var(--muted); font-size: .78rem; text-align: center;
                 padding: 1.5rem 1rem 1rem; }

  /* Past-events archive (collapsible, below the live listings) */
  .archive-jump { margin-left: auto; text-decoration: none;
                  border: 1px solid var(--border); color: var(--muted); }
  .archive-jump::before { content: "↺"; margin-right: .35rem; font-weight: 700; }
  .archive-jump:hover { border-color: var(--teal); color: var(--teal); }
  @media (max-width: 640px) { .archive-jump { margin-left: 0; } }
  .archive { max-width: 1180px; margin: 0 auto; padding: 0 1.5rem; scroll-margin-top: 70px; }
  .archive-toggle { width: 100%; display: flex; align-items: center;
                    justify-content: center; gap: .5rem; background: #fff;
                    border: 1px solid var(--border); border-radius: 10px;
                    padding: .8rem 1rem; cursor: pointer; color: var(--text);
                    font-size: .9rem; font-weight: 600;
                    transition: background .15s, border-color .15s; }
  .archive-toggle:hover { background: #f7f9fb; border-color: var(--teal); }
  .archive-toggle .count { background: #f0f2f5; color: var(--muted);
                           border-radius: 12px; padding: .05rem .5rem;
                           font-size: .78rem; font-weight: 700; }
  .archive-chevron { transition: transform .2s; font-size: .8rem; color: var(--muted); }
  .archive-toggle[aria-expanded="true"] .archive-chevron { transform: rotate(180deg); }
  .archive-body { padding-top: 1rem; }
  .archive-search { width: 100%; box-sizing: border-box; padding: .7rem .9rem;
                    border: 1px solid var(--border); border-radius: 10px;
                    font-size: .9rem; margin-bottom: 1rem; }
  .archive-search:focus { outline: none; border-color: var(--teal); }
  .archive .card { opacity: .82; }
  .archive .month-header { font-size: .8rem; font-weight: 700;
                           text-transform: uppercase; letter-spacing: .06em;
                           color: var(--muted); margin: 1.2rem 0 .6rem; }
  .archive .empty { padding: 2rem 1rem; }

  /* About section (above the LINE banner) */
  .site-about { max-width: 1180px; margin: 0 auto;
                padding: 1.5rem 1.5rem 2rem; color: var(--muted);
                font-size: .9rem; line-height: 1.6; text-align: center; }
  .site-about p { margin: .25rem 0; }
  .site-about .th { color: var(--text); font-weight: 500; }

  /* LINE OA sticky banner */
  body { padding-bottom: 92px; }  /* room for the fixed banner */
  .line-banner { position: fixed; left: 0; right: 0; bottom: 0;
                 background: var(--teal); color: var(--gold);
                 padding: 1.25rem 1rem; z-index: 50;
                 box-shadow: 0 -2px 8px rgba(13,34,51,.15);
                 display: flex; align-items: center; justify-content: center; gap: .8rem; }
  .line-banner a { color: var(--gold); text-decoration: none; font-weight: 600; }
  .line-banner a:hover { text-decoration: underline; }
  .line-banner-text { font-size: 1.02rem; }
  .line-banner-handle { background: rgba(244, 168, 37, .15);
                        border: 1px solid rgba(244, 168, 37, .35);
                        padding: .2rem .65rem; border-radius: 14px;
                        font-size: .9rem; margin-left: .25rem; }
  .line-icon { width: 30px; height: 30px; flex-shrink: 0; }
  @media (max-width: 640px) {
    .line-banner { padding: .9rem .8rem; gap: .5rem; flex-wrap: wrap; }
    .line-banner-text { font-size: .92rem; text-align: center; }
    .line-icon { width: 26px; height: 26px; }
    body { padding-bottom: 115px; }
  }

  /* Mobile */
  @media (max-width: 640px) {
    .header-bar { padding: .8rem 1rem; }
    .brand { font-size: 1.4rem; }
    .hero { padding: 1.5rem 1rem 1.7rem; }
    .hero h1 { font-size: 1.5rem; }
    .filters { padding: .8rem 1rem; }
    main { padding: 1rem; }
    .card { flex-direction: column; }
    .logo-col { display: none; }
    .char-col { width: 100%; min-width: 0; height: 200px; order: -1; }
    .char-col img,
    .char-col svg { width: 100%; height: 100%; object-fit: cover; object-position: top center; }
    .body-col { padding: 1rem 1.1rem 1.1rem; }
    .logo-inline { display: inline-flex; }
  }
</style>
</head>
<body>

<header class="site-header">
  <div class="header-bar">
    <a class="brand-link" href="/?pick" aria-label="Change country"><span class="brand">studyevent<span class="gold">z</span></span><span class="brand-flag" aria-hidden="true">__COUNTRY_FLAG__</span></a>
    <nav class="nav">
      <a href="events.html" class="active">Events</a>
      <a href="about.html">About Us</a>
      <a href="contact.html">Contact Us</a>
      <a href="privacy.html">Privacy</a>
    </nav>
  </div>
</header>

<section class="hero">
  <div class="hero-inner">
    <div class="hero-pair">
      <p class="hero-th-title" lang="th">รวมอีเวนต์เรียนต่อต่างประเทศในไทย</p>
      <h1>Study Abroad Events in __COUNTRY_NAME__</h1>
    </div>
    <div class="hero-pair">
      <p class="hero-th-sub" lang="th">รวมงานแฟร์มหาวิทยาลัย เวบินาร์ และกิจกรรมเรียนต่อต่างประเทศไว้ในที่เดียว</p>
      <p>Find university fairs, webinars, and study abroad briefings across __COUNTRY_NAME__, all in one place.</p>
    </div>
    <div class="hero-pair">
      <p class="hero-th-sub" lang="th">อัปเดตทุกสัปดาห์ พร้อมอีเวนต์ในอีก 30 วันข้างหน้า</p>
      <p>Updated weekly with events happening in the next 30 days.</p>
    </div>
  </div>
</section>

<div class="filters" id="filters">
  <div class="filters-inner">
    <span class="filter-label"><span lang="th">ตัวกรอง</span> / Filter</span>
    <span class="filter-context">Events in the next 30 days</span>
    <button class="chip active" data-filter="all">All <span class="count" data-count="all">0</span></button>
    <button class="chip" data-filter="australia">Australia <span class="count" data-count="australia">0</span></button>
    <button class="chip" data-filter="uk">UK <span class="count" data-count="uk">0</span></button>
    __LOCAL_FILTER_CHIP__
    <span class="filter-divider"></span>
    <button class="chip toggle" id="online-toggle" aria-pressed="false">
      <span class="label">+ Include online events</span>
      <span class="count" data-count="online">0</span>
    </button>
    <a class="chip archive-jump" id="archive-jump" href="#archive" hidden>
      Past events <span class="count" id="archive-jump-count">0</span>
    </a>
  </div>
</div>

<main id="event-root">
  <div class="empty"><h3>Loading…</h3></div>
</main>

<div class="footer-meta" id="footer-meta"></div>

<section class="archive" id="archive" hidden>
  <button class="archive-toggle" id="archive-toggle" aria-expanded="false" aria-controls="archive-body">
    <span class="archive-toggle-label">Past events <span class="count" id="archive-count">0</span></span>
    <span class="archive-chevron" aria-hidden="true">▾</span>
  </button>
  <div class="archive-body" id="archive-body" hidden>
    <input type="search" class="archive-search" id="archive-search"
           placeholder="Search past events by name, organizer or place…"
           aria-label="Search past events" autocomplete="off">
    <div id="archive-root"></div>
  </div>
</section>

<section class="site-about">
  <p class="th">studyeventz รวบรวมงาน study abroad จากบริษัทแนะแนวทั่วประเทศไทย อัปเดตทุกวันจันทร์</p>
  <p>studyeventz aggregates study abroad events from consultancies across __COUNTRY_NAME__. Updated every Monday.</p>
</section>

__NOTIFY_BANNER__

<script>
// ── Front-end analytics (queues to localStorage, no backend yet) ──────────
const ANALYTICS_KEY = 'studyeventz_analytics';
const ANALYTICS_MAX = 500;

function getSessionId() {
  try {
    let sid = sessionStorage.getItem('studyeventz_sid');
    if (!sid) {
      sid = (window.crypto && crypto.randomUUID)
        ? crypto.randomUUID()
        : Date.now().toString(36) + Math.random().toString(36).slice(2);
      sessionStorage.setItem('studyeventz_sid', sid);
    }
    return sid;
  } catch (e) { return 'no-session'; }
}

function track(type, payload) {
  payload = payload || {};
  const event = Object.assign({
    type,
    ts: new Date().toISOString(),
    session_id: getSessionId(),
    page: location.pathname,
    country: "__COUNTRY_CODE__",
  }, payload);
  console.log('[studyeventz]', type, payload);
  try {
    const raw = localStorage.getItem(ANALYTICS_KEY);
    const queue = raw ? JSON.parse(raw) : [];
    queue.push(event);
    while (queue.length > ANALYTICS_MAX) queue.shift();
    localStorage.setItem(ANALYTICS_KEY, JSON.stringify(queue));
  } catch (e) {
    // localStorage may be unavailable (Safari private mode, quota exceeded) — fall back to console only
  }
}

// Helper for debugging in console: studyeventz.dump() to see the queue
window.studyeventz = {
  dump: () => JSON.parse(localStorage.getItem(ANALYTICS_KEY) || '[]'),
  clear: () => localStorage.removeItem(ANALYTICS_KEY),
  pending: () => JSON.parse(localStorage.getItem(PENDING_KEY) || '[]'),
  flush: () => flushPending(),
};

// ── Backend ingest (sends queued events to the Cloudflare Worker) ──────────
// Layered ON TOP of the existing track()/localStorage queue — does not replace it.
// If INGEST_URL is empty, this whole layer is a no-op and the frontend still
// works exactly as before.
const INGEST_URL  = "__INGEST_URL__";
const SITE_KEY    = "__SITE_KEY__";
const LINE_HANDLE = "__LINE_HANDLE__";
const LOCAL_MATCH = "__LOCAL_MATCH__";
const PENDING_KEY = 'studyeventz_pending';
const PENDING_MAX = 500;
const CLICK_TYPES = new Set([
  'event_register_click', 'logo_click', 'location_click', 'calendar_click', 'line_click'
]);

function getPending() {
  try { return JSON.parse(localStorage.getItem(PENDING_KEY) || '[]'); }
  catch (e) { return []; }
}
function setPending(arr) {
  try {
    // Cap to avoid unbounded growth if the backend is down for a long time
    const capped = arr.length > PENDING_MAX ? arr.slice(-PENDING_MAX) : arr;
    localStorage.setItem(PENDING_KEY, JSON.stringify(capped));
  } catch (e) { /* storage full — drop silently */ }
}
function addPending(event) {
  if (!INGEST_URL) return;
  const arr = getPending();
  arr.push(event);
  setPending(arr);
}

function flushPending() {
  if (!INGEST_URL) return;
  const pending = getPending();
  if (pending.length === 0) return;

  // The Worker expects a JSON body and reads ?k= for the site key (so sendBeacon works too).
  const url = INGEST_URL + (INGEST_URL.includes('?') ? '&' : '?') + 'k=' + encodeURIComponent(SITE_KEY);
  const body = JSON.stringify(pending);

  // sendBeacon: fire-and-forget, survives page-unload, no headers control needed
  if (navigator.sendBeacon) {
    try {
      const blob = new Blob([body], { type: 'application/json' });
      if (navigator.sendBeacon(url, blob)) {
        // Optimistic — if the backend later 4xx/5xx's this batch, we lose it.
        // Acceptable for v1; we never block the user's click on a backend round-trip.
        setPending([]);
        return;
      }
    } catch (e) { /* fall through to fetch */ }
  }

  // Fallback: fetch with keepalive so the request survives navigation
  try {
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
      keepalive: true,
    }).then(r => {
      // Only clear on confirmed 2xx — keepalive fetches return real responses
      if (r && r.ok) setPending([]);
    }).catch(() => { /* leave in queue, retry next pageload */ });
  } catch (e) { /* leave in queue */ }
}

// Wrap track() so the existing local behaviour is unchanged and we add backend send
const _baseTrack = track;
track = function(type, payload) {
  _baseTrack(type, payload);
  if (!INGEST_URL) return;
  // The event object track() built is the last item in the queue
  const queue = JSON.parse(localStorage.getItem(ANALYTICS_KEY) || '[]');
  const justAdded = queue[queue.length - 1];
  if (!justAdded) return;
  addPending(justAdded);
  // Flush click events immediately (user may navigate away); batch impressions
  if (CLICK_TYPES.has(type)) flushPending();
};

// Periodic flush so impression batches go out without waiting for unload
setInterval(flushPending, 5000);

// Flush when user navigates away / hides the tab
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') flushPending();
});

// On page load, drain any events that piled up from previous sessions
window.addEventListener('load', flushPending);

// Card impressions — fires once per card per pageload when 50% visible
const SEEN_IMPRESSIONS = new Set();
const impressionObserver = ('IntersectionObserver' in window) ? new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (entry.isIntersecting && entry.intersectionRatio >= 0.5) {
      const card = entry.target;
      const id = card.dataset.eventId;
      if (id && !SEEN_IMPRESSIONS.has(id)) {
        SEEN_IMPRESSIONS.add(id);
        track('event_impression', {
          event_id: id,
          event_name: card.dataset.eventName,
          agent_name: card.dataset.agent,
          date: card.dataset.date,
          online: card.dataset.online === 'true',
        });
        impressionObserver.unobserve(card);
      }
    }
  }
}, { threshold: 0.5 }) : null;

function attachCardTracking() {
  if (!impressionObserver) return;
  document.querySelectorAll('.card').forEach(c => {
    if (!SEEN_IMPRESSIONS.has(c.dataset.eventId)) impressionObserver.observe(c);
  });
}

// Click delegation — register button, logo, and LINE banner
document.addEventListener('click', (e) => {
  // LINE banner click (outside any card)
  if (e.target.closest('#line-link')) {
    track('line_click', { handle: LINE_HANDLE });
    return;
  }
  const card = e.target.closest('.card');
  if (!card) return;
  const meta = {
    event_id: card.dataset.eventId,
    event_name: card.dataset.eventName,
    agent_name: card.dataset.agent,
    date: card.dataset.date,
  };
  if (e.target.closest('.btn-register:not(.disabled)')) {
    const link = e.target.closest('a');
    track('event_register_click', Object.assign({}, meta, {
      registration_url: link ? link.href : null,
    }));
  } else if (e.target.closest('.logo-col, .logo-inline')) {
    track('logo_click', meta);
  } else if (e.target.closest('.pill-calendar')) {
    const link = e.target.closest('a');
    track('calendar_click', Object.assign({}, meta, {
      calendar_url: link ? link.href : null,
    }));
  } else if (e.target.closest('.pill-link')) {
    const link = e.target.closest('a');
    track('location_click', Object.assign({}, meta, {
      maps_url: link ? link.href : null,
    }));
  }
});

const CHARACTERS = __CHARACTERS_JSON__;

function characterMarkup(entry) {
  // String entries are image URLs; objects with .svg are inline fallbacks.
  if (typeof entry === 'string') {
    return `<img src="${entry}" alt="" loading="lazy">`;
  }
  return entry.svg;
}

function pad2(n) { return String(n).padStart(2, '0'); }

// Parse a free-form time field like "1:00 PM - 3:00 PM", "14:00", "10am - 12pm".
// Returns {startH, startM, endH, endM} in 24h, or null if unparseable.
function parseTimeRange(timeStr) {
  if (!timeStr) return null;
  const s = timeStr.replace(/[–—−]/g, '-');
  const re = /(\d{1,2})(?::(\d{2}))?\s*(AM|PM|am|pm)?(?:\s*-\s*(\d{1,2})(?::(\d{2}))?\s*(AM|PM|am|pm)?)?/;
  const m = s.match(re);
  if (!m) return null;
  function to24(h, ap) {
    h = parseInt(h, 10);
    if (!ap) return h;
    const a = ap.toLowerCase();
    if (a === 'am' && h === 12) return 0;
    if (a === 'pm' && h !== 12) return h + 12;
    return h;
  }
  const startH = to24(m[1], m[3]);
  const startM = m[2] ? parseInt(m[2], 10) : 0;
  // If end AP missing, inherit start AP; if end hour missing, default to start + 2h
  const endH = m[4] ? to24(m[4], m[6] || m[3]) : (startH + 2) % 24;
  const endM = m[5] ? parseInt(m[5], 10) : startM;
  return { startH, startM, endH, endM };
}

// Escape special chars per RFC 5545 (iCalendar)
function icsEscape(s) {
  return (s || '').replace(/\\/g, '\\\\').replace(/[\r\n]+/g, '\\n')
                  .replace(/,/g, '\\,').replace(/;/g, '\\;');
}

function buildIcsContent(ev) {
  const dateCompact = (ev.date || '').replace(/-/g, '');
  const range = parseTimeRange(ev.time);
  let dtstart, dtend;
  if (range) {
    dtstart = `DTSTART;TZID=__TIMEZONE__:${dateCompact}T${pad2(range.startH)}${pad2(range.startM)}00`;
    dtend   = `DTEND;TZID=__TIMEZONE__:${dateCompact}T${pad2(range.endH)}${pad2(range.endM)}00`;
  } else {
    const d = new Date(ev.date + 'T00:00:00');
    d.setDate(d.getDate() + 1);
    const nextCompact = d.toISOString().slice(0, 10).replace(/-/g, '');
    dtstart = `DTSTART;VALUE=DATE:${dateCompact}`;
    dtend   = `DTEND;VALUE=DATE:${nextCompact}`;
  }
  const now = new Date().toISOString().replace(/[-:.]/g, '').slice(0, 15) + 'Z';
  const description = [
    `Organized by ${ev.organizer || ev.agent_name || 'studyeventz'}`,
    ev.registration_url ? `Register: ${ev.registration_url}` : '',
    `Listed on studyeventz: https://www.studyeventz.com/events.html`,
  ].filter(Boolean).join('\n');
  return [
    'BEGIN:VCALENDAR',
    'VERSION:2.0',
    'PRODID:-//studyeventz//__COUNTRY_NAME__ Events//EN',
    'CALSCALE:GREGORIAN',
    'METHOD:PUBLISH',
    'BEGIN:VEVENT',
    `UID:studyeventz-${ev.id}-${ev.date}@studyeventz.com`,
    `DTSTAMP:${now}`,
    dtstart,
    dtend,
    `SUMMARY:${icsEscape(ev.name)}`,
    `DESCRIPTION:${icsEscape(description)}`,
    `LOCATION:${icsEscape(ev.location)}`,
    ev.registration_url ? `URL:${ev.registration_url}` : '',
    'END:VEVENT',
    'END:VCALENDAR',
  ].filter(Boolean).join('\r\n');
}

function buildCalendarUrl(ev) {
  // Data URL with text/calendar mime type triggers each OS's native
  // "Add to Calendar" handler: Apple Calendar on iOS, Google/Samsung
  // Calendar on Android, default app on desktop.
  return 'data:text/calendar;charset=utf-8,' + encodeURIComponent(buildIcsContent(ev));
}

function calendarFilename(ev) {
  const slug = (ev.name || 'event').toLowerCase()
    .replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40);
  return `${slug || 'event'}-${ev.date}.ics`;
}

function locationPill(ev) {
  if (!ev.location) return '';
  const content = `${ICONS.location}${escapeHTML(ev.location)}`;
  // Online events have no physical pin — keep as a plain span
  if (isOnline(ev)) return `<span class="pill">${content}</span>`;
  // Strip trailing "/ Online" so Maps doesn't get confused on hybrid events
  let query = ev.location.replace(/\s*\/\s*online\s*$/i, '').trim();
  if (!/__COUNTRY_NAME__/i.test(query)) query += ', __COUNTRY_NAME__';
  const url = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(query)}`;
  return `<a class="pill pill-link" href="${url}" target="_blank" rel="noopener">${content}</a>`;
}

function logoInner(ev) {
  if (ev.logo_url) {
    const cls = ev.logo_needs_bg ? ' class="needs-bg"' : '';
    const style = ev.logo_bg_color ? ` style="background: ${ev.logo_bg_color}"` : '';
    return `<img${cls}${style} src="${ev.logo_url}" alt="${escapeHTML(ev.agent_name)} logo" loading="lazy">`;
  }
  return `<span class="initials-avatar" aria-hidden="true">${escapeHTML(ev.initials || '?')}</span>`;
}

function logoMarkup(ev) {
  const inner = logoInner(ev);
  const url = ev.agent_website || '';
  if (!url) return inner;
  const safeUrl = url.startsWith('http') ? url : ('https://' + url);
  return `<a class="logo-link" href="${escapeHTML(safeUrl)}" target="_blank" rel="noopener" aria-label="${escapeHTML(ev.agent_name)} website">${inner}</a>`;
}

// SVG icons for meta pills
const ICONS = {
  date: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M5 1v1H3a1 1 0 00-1 1v10a1 1 0 001 1h10a1 1 0 001-1V3a1 1 0 00-1-1h-2V1h-1v1H6V1H5zm-2 4h10v8H3V5z"/></svg>',
  time: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 1a7 7 0 100 14A7 7 0 008 1zm0 2a5 5 0 110 10 5 5 0 010-10zm-.5 2v3.25l2.5 1.5-.5.85L7 8.25V5h.5z"/></svg>',
  location: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 1a5 5 0 00-5 5c0 3.5 5 9 5 9s5-5.5 5-9a5 5 0 00-5-5zm0 3a2 2 0 110 4 2 2 0 010-4z"/></svg>'
};

function fmtDateRange(start) {
  const opts = { day: 'numeric', month: 'short' };
  const end = new Date(start); end.setDate(end.getDate() + 6);
  const s = start.toLocaleDateString('en-GB', opts);
  const e = end.toLocaleDateString('en-GB', opts);
  return `Week of ${s.replace(' ','–').split('–')[0]}–${e.replace(' ',' ')}`;
}

function startOfWeek(dateStr) {
  // ISO week: Monday start
  const d = new Date(dateStr + 'T00:00:00');
  const day = (d.getDay() + 6) % 7; // 0=Mon
  d.setDate(d.getDate() - day);
  d.setHours(0, 0, 0, 0);
  return d;
}

function weekLabel(monday) {
  const sunday = new Date(monday); sunday.setDate(sunday.getDate() + 6);
  const mDay = monday.getDate();
  const sDay = sunday.getDate();
  const mMonth = monday.toLocaleDateString('en-GB', { month: 'short' });
  const sMonth = sunday.toLocaleDateString('en-GB', { month: 'short' });
  if (mMonth === sMonth) return `Week of ${mDay}–${sDay} ${mMonth}`;
  return `Week of ${mDay} ${mMonth} – ${sDay} ${sMonth}`;
}

function fmtEventDate(dateStr) {
  const d = new Date(dateStr + 'T00:00:00');
  return d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
}

function escapeHTML(s) {
  return (s || '').replace(/[&<>"']/g, c => (
    { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]
  ));
}

function destinationCountry(ev) {
  // Infer Australia/UK from event name + location.
  const hay = (ev.name + ' ' + ev.location).toLowerCase();
  const auKeys = ['australia', 'sydney', 'melbourne', 'brisbane', 'perth', 'adelaide',
                  'canberra', 'macquarie', 'monash', 'unsw', 'australian'];
  const ukKeys = ['united kingdom', ' uk ', 'uk:', ' uk,', 'london', 'manchester',
                  'edinburgh', 'oxford', 'cambridge', 'britain', 'british'];
  if (auKeys.some(k => hay.includes(k))) return 'australia';
  if (ukKeys.some(k => hay.includes(k))) return 'uk';
  return null;
}

function isOnline(ev) {
  return (ev.location || '').toLowerCase().includes('online');
}

function matchesInPerson(ev, filter) {
  // Apply only the in-person filter. Online events do NOT match here — they're
  // routed through the Online toggle instead.
  if (isOnline(ev)) return false;
  if (filter === 'all') return true;
  if (filter === 'australia' || filter === 'uk') return destinationCountry(ev) === filter;
  if (filter === 'local') return LOCAL_MATCH && (ev.location || '').toLowerCase().includes(LOCAL_MATCH);
  return false;
}

function eventVisible(ev, filter, showOnline) {
  if (isOnline(ev)) return showOnline;
  return matchesInPerson(ev, filter);
}

function renderCard(ev, idx) {
  const charSvg = characterMarkup(CHARACTERS[idx % CHARACTERS.length]);
  const logo = logoMarkup(ev);
  const regUrl = ev.registration_url || '';
  const btn = regUrl
    ? `<a class="btn-register" href="${escapeHTML(regUrl)}" target="_blank" rel="noopener">Register →</a>`
    : `<span class="btn-register disabled">No link</span>`;
  const calendarUrl = buildCalendarUrl(ev);
  const calendarFile = calendarFilename(ev);
  const datePill = `<a class="pill pill-link pill-calendar" href="${calendarUrl}" download="${calendarFile}" title="Add to calendar">${ICONS.date}${escapeHTML(fmtEventDate(ev.date))}</a>`;
  const timePill = ev.time ? `<span class="pill">${ICONS.time}${escapeHTML(ev.time)}</span>` : '';
  const locPill = locationPill(ev);
  return `
    <article class="card"
             data-event-id="${escapeHTML(String(ev.id))}"
             data-event-name="${escapeHTML(ev.name)}"
             data-agent="${escapeHTML(ev.agent_name)}"
             data-date="${escapeHTML(ev.date)}"
             data-online="${isOnline(ev)}">
      <div class="logo-col">${logo}</div>
      <div class="body-col">
        <div class="logo-inline">${logo}</div>
        <div class="organizer">${escapeHTML(ev.organizer)}</div>
        <div class="event-name">${escapeHTML(ev.name)}</div>
        <div class="meta">${datePill}${timePill}${locPill}</div>
        <div class="card-actions">${btn}</div>
      </div>
      <div class="char-col">${charSvg}</div>
    </article>
  `;
}

function groupByWeek(events) {
  const groups = new Map();
  for (const ev of events) {
    const monday = startOfWeek(ev.date);
    const key = monday.toISOString().slice(0, 10);
    if (!groups.has(key)) groups.set(key, { monday, events: [] });
    groups.get(key).events.push(ev);
  }
  return [...groups.values()].sort((a, b) => a.monday - b.monday);
}

function render(events, filter, showOnline) {
  const root = document.getElementById('event-root');
  const filtered = events.filter(e => eventVisible(e, filter, showOnline));
  if (filtered.length === 0) {
    const hasOnlineHidden = events.some(isOnline) && !showOnline;
    root.innerHTML = `
      <div class="empty">
        <h3>No upcoming events</h3>
        <p>${filter === 'all'
            ? 'No in-person events found in the next 30 days.' + (hasOnlineHidden ? ' Toggle <strong>+ Online</strong> to see online events.' : '')
            : 'No in-person matches for this filter.' + (hasOnlineHidden ? ' Toggle <strong>+ Online</strong> to include online events.' : '')}</p>
      </div>`;
    return;
  }
  const groups = groupByWeek(filtered);
  let idx = 0;
  root.innerHTML = groups.map(g => `
    <section class="week-section">
      <h2 class="week-header">${weekLabel(g.monday)}</h2>
      ${g.events.map(ev => renderCard(ev, idx++)).join('')}
    </section>
  `).join('');
  attachCardTracking();
}

function updateCounts(events) {
  // In-person counts ignore online events (online has its own additive toggle)
  const inPersonFilters = ['all', 'australia', 'uk', 'local'];
  for (const f of inPersonFilters) {
    const n = events.filter(e => matchesInPerson(e, f)).length;
    const el = document.querySelector(`[data-count="${f}"]`);
    if (el) el.textContent = n;
  }
  const onlineN = events.filter(isOnline).length;
  const onlineEl = document.querySelector('[data-count="online"]');
  if (onlineEl) onlineEl.textContent = onlineN;
}

let CURRENT_FILTER = 'all';
let SHOW_ONLINE = false;
let EVENTS = [];
let PAST = [];
let ARCHIVE_RENDERED = false;

function setOnlineToggle(on) {
  SHOW_ONLINE = on;
  const btn = document.getElementById('online-toggle');
  if (!btn) return;
  btn.classList.toggle('active', on);
  btn.setAttribute('aria-pressed', String(on));
  const label = btn.querySelector('.label');
  if (label) label.textContent = on ? 'Including online events ✓' : '+ Include online events';
}

document.getElementById('filters').addEventListener('click', e => {
  const btn = e.target.closest('.chip');
  if (!btn) return;
  if (btn.id === 'online-toggle') {
    setOnlineToggle(!SHOW_ONLINE);
  } else {
    document.querySelectorAll('.chip:not(.toggle)').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    CURRENT_FILTER = btn.dataset.filter;
  }
  render(EVENTS, CURRENT_FILTER, SHOW_ONLINE);
});

function monthLabel(dateStr) {
  const d = new Date(dateStr + 'T00:00:00');
  return d.toLocaleDateString('en-GB', { month: 'long', year: 'numeric' });
}

function groupByMonth(events) {
  // events arrive most-recent-first; preserve that order across and within months.
  const groups = new Map();
  for (const ev of events) {
    const key = ev.date.slice(0, 7); // YYYY-MM
    if (!groups.has(key)) groups.set(key, { label: monthLabel(ev.date), events: [] });
    groups.get(key).events.push(ev);
  }
  return [...groups.values()];
}

function renderArchive(query) {
  const root = document.getElementById('archive-root');
  const q = (query || '').trim().toLowerCase();
  const filtered = q
    ? PAST.filter(ev => (
        (ev.name + ' ' + ev.organizer + ' ' + ev.agent_name + ' ' + ev.location)
          .toLowerCase().includes(q)))
    : PAST;
  if (filtered.length === 0) {
    root.innerHTML = `<div class="empty"><p>${q ? 'No past events match your search.' : 'No past events yet.'}</p></div>`;
    return;
  }
  let idx = 0;
  root.innerHTML = groupByMonth(filtered).map(g => `
    <section class="month-section">
      <h3 class="month-header">${escapeHTML(g.label)}</h3>
      ${g.events.map(ev => renderCard(ev, idx++)).join('')}
    </section>
  `).join('');
}

function initArchive() {
  const section = document.getElementById('archive');
  if (!PAST.length) return;               // nothing to show — leave it hidden
  section.hidden = false;
  document.getElementById('archive-count').textContent = PAST.length;

  const toggle = document.getElementById('archive-toggle');
  const body = document.getElementById('archive-body');

  function setOpen(open) {
    toggle.setAttribute('aria-expanded', String(open));
    body.hidden = !open;
    if (open && !ARCHIVE_RENDERED) { renderArchive(''); ARCHIVE_RENDERED = true; }
  }

  toggle.addEventListener('click', () => {
    setOpen(toggle.getAttribute('aria-expanded') !== 'true');
  });

  // Discoverable jump link in the filter bar — reveal it and wire it to open
  // the archive and scroll down to it (otherwise it's buried below the list).
  const jump = document.getElementById('archive-jump');
  if (jump) {
    document.getElementById('archive-jump-count').textContent = PAST.length;
    jump.hidden = false;
    jump.addEventListener('click', (e) => {
      e.preventDefault();
      setOpen(true);
      section.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }

  const search = document.getElementById('archive-search');
  let t;
  search.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(() => renderArchive(search.value), 120);
  });
}

fetch('data/events.json', { cache: 'no-store' })
  .then(r => {
    if (!r.ok) throw new Error('Failed to load events data');
    return r.json();
  })
  .then(data => {
    EVENTS = data.events || [];
    PAST = data.past || [];
    updateCounts(EVENTS);
    render(EVENTS, CURRENT_FILTER, SHOW_ONLINE);
    initArchive();
    const meta = document.getElementById('footer-meta');
    if (data.generated_at) {
      meta.textContent = `${EVENTS.length} event${EVENTS.length === 1 ? '' : 's'} · Updated ${new Date(data.generated_at).toLocaleString('en-GB')}`;
    }
  })
  .catch(err => {
    document.getElementById('event-root').innerHTML = `
      <div class="empty">
        <h3>Could not load events</h3>
        <p>${escapeHTML(err.message)}</p>
        <p style="margin-top:.5rem;font-size:.85rem">If you're viewing this locally, serve via <code>python -m http.server</code> rather than <code>file://</code>.</p>
      </div>`;
  });
</script>

</body>
</html>
"""


ABOUT_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>About — studyeventz</title>
<meta name="description" content="studyeventz is an independent guide to study abroad events in Thailand — fairs, webinars and briefings gathered weekly.">
<meta name="description" lang="th" content="studyeventz เป็นคู่มืออิสระสำหรับค้นหากิจกรรมเรียนต่อต่างประเทศในไทย">
<link rel="canonical" href="__SITE_URL__/about.html">

<link rel="stylesheet" href="/assets/fonts/noto-sans-thai.css">

<style>
  :root {
    --teal: #0d7377; --teal-dark: #095a5d; --gold: #f4a825;
    --ink: #0d2233; --bg: #f5f7f9; --text: #1a2530;
    --muted: #5d6b78; --border: #e2e8ed;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         background: var(--bg); color: var(--text); line-height: 1.6;
         padding-bottom: 92px; }
  [lang="th"] { font-family: "Noto Sans Thai", "Sukhumvit Set", "Leelawadee UI",
                              -apple-system, BlinkMacSystemFont, sans-serif; }

  /* Header */
  .site-header { background: var(--teal); color: #fff; }
  .header-bar { max-width: 1180px; margin: 0 auto; padding: 1rem 1.5rem;
                display: flex; align-items: center; justify-content: space-between;
                gap: 1.5rem; flex-wrap: wrap; }
  .brand { font-size: 1.6rem; font-weight: 800; letter-spacing: -.02em; }
  .brand .gold { color: var(--gold); }
  .brand-link { display: inline-flex; align-items: center; gap: .55rem;
                text-decoration: none; color: inherit;
                transition: opacity .15s; }
  .brand-link:hover { opacity: .85; }
  .brand-flag { font-size: 1.3rem; line-height: 1; }
  .nav { display: flex; gap: .25rem; flex-wrap: wrap; }
  .nav a { color: rgba(255,255,255,.85); text-decoration: none;
           padding: .5rem .9rem; border-radius: 6px; font-size: .9rem; font-weight: 500;
           transition: background .15s, color .15s; }
  .nav a:hover { background: rgba(255,255,255,.12); color: #fff; }
  .nav a.active { background: rgba(255,255,255,.18); color: #fff; font-weight: 600; }

  /* Hero strip */
  .about-hero { background: var(--teal-dark); color: #fff; padding: 2rem 1.5rem 2.2rem; }
  .about-hero-inner { max-width: 760px; margin: 0 auto; }
  .about-hero h1 { font-size: 1.9rem; font-weight: 700; line-height: 1.2; margin: 0; }
  .about-hero .hero-th-title { color: var(--gold); font-size: 2.47rem; font-weight: 700;
                               line-height: 1.2; margin: 0 0 .35rem 0; letter-spacing: .01em; }

  /* Body content */
  .about-content { max-width: 760px; margin: 0 auto; padding: 2.5rem 1.5rem 3rem; }
  .about-pair { margin-bottom: 2rem; }
  .about-pair p { font-size: 1.05rem; margin: 0; }
  .about-pair p.th { color: var(--text); font-weight: 500; margin-bottom: .5rem; }
  .about-pair p:not(.th) { color: var(--muted); }
  .about-pair.about-cta { border-top: 1px solid var(--border); padding-top: 2rem;
                          margin-top: 2.5rem; margin-bottom: 0; }
  .about-pair.about-cta p.th { color: var(--teal); font-weight: 600; }
  .about-pair.about-cta p:not(.th) { color: var(--text); font-weight: 500; }

  /* LINE OA sticky banner (same as events page) */
  .line-banner { position: fixed; left: 0; right: 0; bottom: 0;
                 background: var(--teal); color: var(--gold);
                 padding: 1.25rem 1rem; z-index: 50;
                 box-shadow: 0 -2px 8px rgba(13,34,51,.15);
                 display: flex; align-items: center; justify-content: center; gap: .8rem; }
  .line-banner a { color: var(--gold); text-decoration: none; font-weight: 600; }
  .line-banner a:hover { text-decoration: underline; }
  .line-banner-text { font-size: 1.02rem; }
  .line-banner-handle { background: rgba(244, 168, 37, .15);
                        border: 1px solid rgba(244, 168, 37, .35);
                        padding: .2rem .65rem; border-radius: 14px;
                        font-size: .9rem; margin-left: .25rem; }
  .line-icon { width: 30px; height: 30px; flex-shrink: 0; }

  @media (max-width: 640px) {
    .header-bar { padding: .8rem 1rem; }
    .brand { font-size: 1.4rem; }
    .about-hero { padding: 1.5rem 1rem 1.7rem; }
    .about-hero h1 { font-size: 1.5rem; }
    .about-hero .hero-th-title { font-size: 1.95rem; }
    .about-content { padding: 2rem 1.1rem 2.5rem; }
    .about-pair p { font-size: .98rem; }
    .line-banner { padding: .9rem .8rem; gap: .5rem; flex-wrap: wrap; }
    .line-banner-text { font-size: .92rem; text-align: center; }
    .line-icon { width: 26px; height: 26px; }
    body { padding-bottom: 115px; }
  }
</style>
</head>
<body>

<header class="site-header">
  <div class="header-bar">
    <a class="brand-link" href="/?pick" aria-label="Change country"><span class="brand">studyevent<span class="gold">z</span></span><span class="brand-flag" aria-hidden="true">__COUNTRY_FLAG__</span></a>
    <nav class="nav">
      <a href="events.html">Events</a>
      <a href="about.html" class="active">About Us</a>
      <a href="contact.html">Contact Us</a>
      <a href="privacy.html">Privacy</a>
    </nav>
  </div>
</header>

<section class="about-hero">
  <div class="about-hero-inner">
    <p class="hero-th-title" lang="th">เกี่ยวกับเรา</p>
    <h1>About Us</h1>
  </div>
</section>

<main class="about-content">
  <section class="about-pair">
    <p class="th" lang="th">studyeventz เป็นคู่มืออิสระสำหรับค้นหากิจกรรมเรียนต่อต่างประเทศ ไม่ว่าจะเป็นงานแฟร์มหาวิทยาลัย วันให้ข้อมูล Open Day หรือกำหนดปิดรับสมัครทุนการศึกษา โดยรวบรวมไว้ในที่เดียว และอัปเดตทุกสัปดาห์</p>
    <p>studyeventz is an independent guide to study abroad events — university fairs, information days, open days and scholarship deadlines — gathered in one place and updated every week.</p>
  </section>

  <section class="about-pair">
    <p class="th" lang="th">ปกติแล้ว การหากิจกรรมเหล่านี้ต้องใช้เวลาค้นหาจาก Facebook หลายสิบเพจ เว็บไซต์เอเจนซี่ และปฏิทินกิจกรรมของมหาวิทยาลัยต่าง ๆ แต่เราเป็นคนทำงานนั้นให้โดยอัตโนมัติ ทุกสัปดาห์ เรารวบรวมกิจกรรมจากบริษัทแนะแนวการศึกษาและพาร์ตเนอร์มหาวิทยาลัยทั่วตลาด ตรวจสอบและลบข้อมูลซ้ำ แล้วเผยแพร่เป็นรายการกิจกรรมที่สะอาด ชัดเจน และเชื่อถือได้</p>
    <p>Finding these events normally means trawling dozens of Facebook pages, agency websites and university calendars. We do that work automatically: every week we collect events from education consultancies and university partners across the market, remove the duplicates, and publish a single clean list you can actually rely on.</p>
  </section>

  <section class="about-pair">
    <p class="th" lang="th">เราเริ่มต้นจากประเทศไทย ซึ่งในแต่ละปีมีงานเรียนต่อต่างประเทศหลายร้อยงาน แต่ยังไม่มีศูนย์กลางเดียวสำหรับค้นหาข้อมูลเหล่านี้ เราเป็นแพลตฟอร์มอิสระ ไม่ได้เป็นตัวแทนของมหาวิทยาลัยหรือเอเจนซี่ใดเป็นพิเศษ ดังนั้นสิ่งที่คุณเห็นคือภาพรวมของตัวเลือกที่หลากหลาย ไม่ใช่การนำเสนอจากบริษัทใดบริษัทหนึ่งเท่านั้น</p>
    <p>We started in Thailand, where hundreds of study abroad events run every year with no single place to find them. We're independent — we don't represent any one university or agency, so what you see is the full range of options, not one company's pitch.</p>
  </section>

  <section class="about-pair about-cta">
    <p class="th" lang="th">สนใจนำ studyeventz ไปใช้ในตลาดของคุณหรือไม่? เรายินดีพูดคุยกับคุณครับ/ค่ะ</p>
    <p>Interested in bringing studyeventz to your market? We'd like to hear from you.</p>
  </section>
</main>

__NOTIFY_BANNER__

</body>
</html>
"""


def build_about_html(country: "Country") -> None:
    """Write <country.code>/about.html — a static localised About page."""
    html = localize(ABOUT_HTML, country)
    for ph, val in {
        "__SITE_URL__":       SITE_URL,
        "__COUNTRY_SITE__":   country.site_url,
        "__COUNTRY_CODE__":   country.code,
        "__COUNTRY_NAME__":   country.name_en,
        "__COUNTRY_FLAG__":   country.flag,
        "__NOTIFY_BANNER__":  render_notify_banner(country),
    }.items():
        html = html.replace(ph, val)
    country.root.mkdir(parents=True, exist_ok=True)
    country.about_out.write_text(html, encoding="utf-8")


PRIVACY_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy — studyeventz</title>
<meta name="description" content="How studyeventz handles your data — no cookies, no advertising trackers, anonymous analytics only.">
<link rel="canonical" href="__SITE_URL__/privacy.html">

<link rel="stylesheet" href="/assets/fonts/noto-sans-thai.css">

<style>
  :root {
    --teal: #0d7377; --teal-dark: #095a5d; --gold: #f4a825;
    --ink: #0d2233; --bg: #f5f7f9; --text: #1a2530;
    --muted: #5d6b78; --border: #e2e8ed;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         background: var(--bg); color: var(--text); line-height: 1.6;
         padding-bottom: 92px; }
  [lang="th"] { font-family: "Noto Sans Thai", "Sukhumvit Set", "Leelawadee UI",
                              -apple-system, BlinkMacSystemFont, sans-serif; }

  .site-header { background: var(--teal); color: #fff; }
  .header-bar { max-width: 1180px; margin: 0 auto; padding: 1rem 1.5rem;
                display: flex; align-items: center; justify-content: space-between;
                gap: 1.5rem; flex-wrap: wrap; }
  .brand { font-size: 1.6rem; font-weight: 800; letter-spacing: -.02em; }
  .brand .gold { color: var(--gold); }
  .brand-link { display: inline-flex; align-items: center; gap: .55rem;
                text-decoration: none; color: inherit; transition: opacity .15s; }
  .brand-link:hover { opacity: .85; }
  .brand-flag { font-size: 1.3rem; line-height: 1; }
  .nav { display: flex; gap: .25rem; flex-wrap: wrap; }
  .nav a { color: rgba(255,255,255,.85); text-decoration: none;
           padding: .5rem .9rem; border-radius: 6px; font-size: .9rem; font-weight: 500;
           transition: background .15s, color .15s; }
  .nav a:hover { background: rgba(255,255,255,.12); color: #fff; }
  .nav a.active { background: rgba(255,255,255,.18); color: #fff; font-weight: 600; }

  .about-hero { background: var(--teal-dark); color: #fff; padding: 2rem 1.5rem 2.2rem; }
  .about-hero-inner { max-width: 760px; margin: 0 auto; }
  .about-hero h1 { font-size: 1.9rem; font-weight: 700; line-height: 1.2; margin: 0; }
  .about-hero .hero-th-title { color: var(--gold); font-size: 2.47rem; font-weight: 700;
                               line-height: 1.2; margin: 0 0 .35rem 0; letter-spacing: .01em; }

  .about-content { max-width: 760px; margin: 0 auto; padding: 2.5rem 1.5rem 3rem; }
  .about-pair { margin-bottom: 2rem; }
  .about-pair p { font-size: 1.05rem; margin: 0; }
  .about-pair p.th { color: var(--text); font-weight: 500; margin-bottom: .5rem; }
  .about-pair p:not(.th) { color: var(--muted); }
  .privacy-updated { color: var(--muted); font-size: .9rem; margin-top: 2.5rem;
                     border-top: 1px solid var(--border); padding-top: 1.5rem; }

  .line-banner { position: fixed; left: 0; right: 0; bottom: 0;
                 background: var(--teal); color: var(--gold);
                 padding: 1.25rem 1rem; z-index: 50;
                 box-shadow: 0 -2px 8px rgba(13,34,51,.15);
                 display: flex; align-items: center; justify-content: center; gap: .8rem; }
  .line-banner a { color: var(--gold); text-decoration: none; font-weight: 600; }
  .line-banner a:hover { text-decoration: underline; }
  .line-banner-text { font-size: 1.02rem; }
  .line-banner-handle { background: rgba(244, 168, 37, .15);
                        border: 1px solid rgba(244, 168, 37, .35);
                        padding: .2rem .65rem; border-radius: 14px;
                        font-size: .9rem; margin-left: .25rem; }
  .line-icon { width: 30px; height: 30px; flex-shrink: 0; }

  @media (max-width: 640px) {
    .header-bar { padding: .8rem 1rem; }
    .brand { font-size: 1.4rem; }
    .about-hero { padding: 1.5rem 1rem 1.7rem; }
    .about-hero h1 { font-size: 1.5rem; }
    .about-hero .hero-th-title { font-size: 1.95rem; }
    .about-content { padding: 2rem 1.1rem 2.5rem; }
    .about-pair p { font-size: .98rem; }
    .line-banner { padding: .9rem .8rem; gap: .5rem; flex-wrap: wrap; }
    .line-banner-text { font-size: .92rem; text-align: center; }
    .line-icon { width: 26px; height: 26px; }
    body { padding-bottom: 115px; }
  }
</style>
</head>
<body>

<header class="site-header">
  <div class="header-bar">
    <a class="brand-link" href="/?pick" aria-label="Change country"><span class="brand">studyevent<span class="gold">z</span></span><span class="brand-flag" aria-hidden="true">__COUNTRY_FLAG__</span></a>
    <nav class="nav">
      <a href="events.html">Events</a>
      <a href="about.html">About Us</a>
      <a href="contact.html">Contact Us</a>
      <a href="privacy.html" class="active">Privacy</a>
    </nav>
  </div>
</header>

<section class="about-hero">
  <div class="about-hero-inner">
    <p class="hero-th-title" lang="th">นโยบายความเป็นส่วนตัว</p>
    <h1>Privacy</h1>
  </div>
</section>

<main class="about-content">
  <section class="about-pair">
    <p class="th" lang="th">studyeventz ให้ความสำคัญกับความเป็นส่วนตัวของคุณ เราไม่ใช้คุกกี้ ไม่ใช้ตัวติดตามเพื่อการโฆษณา และไม่ขายข้อมูลของคุณ</p>
    <p>studyeventz respects your privacy. We don't use cookies, we don't use advertising trackers, and we never sell your data.</p>
  </section>

  <section class="about-pair">
    <p class="th" lang="th">เราเก็บข้อมูลเล็กน้อยไว้ในเบราว์เซอร์ของคุณ เพื่อจดจำตลาดที่คุณเลือก และเพื่อพักข้อมูลสถิติการใช้งานแบบไม่ระบุตัวตนก่อนส่ง ข้อมูลนี้อยู่บนอุปกรณ์ของคุณ และคุณลบได้ทุกเมื่อผ่านการตั้งค่าเบราว์เซอร์</p>
    <p>We store a small amount of data in your browser to remember the market you chose and to hold anonymous usage statistics before they are sent. This data stays on your device and you can clear it at any time through your browser settings.</p>
  </section>

  <section class="about-pair">
    <p class="th" lang="th">เราเก็บสถิติการใช้งานแบบไม่ระบุตัวตน เช่น หน้าที่เปิดดูและกิจกรรมที่คลิก เพื่อปรับปรุงรายการให้ดีขึ้น เซิร์ฟเวอร์ของเราบันทึกชนิดเบราว์เซอร์ หน้าที่อ้างอิงเข้ามา และที่อยู่ IP ในรูปแบบที่แปลงเป็นค่าแฮชทางเดียว เราไม่เคยเก็บที่อยู่ IP จริงของคุณ และไม่ระบุตัวตนของคุณเป็นรายบุคคล</p>
    <p>We collect anonymous usage statistics — such as which pages are viewed and which events are clicked — to improve the listings. Our servers log your browser type, the referring page, and your IP address in a one-way hashed form. We never store your real IP address and we do not identify you personally.</p>
  </section>

  <section class="about-pair">
    <p class="th" lang="th">เว็บไซต์นี้ทำงานบนโครงสร้างพื้นฐานของ Cloudflare ซึ่งเป็นผู้ประมวลผลข้อมูลให้เรา เราโฮสต์ฟอนต์ของเราเอง และไม่ใช้ Google Analytics, Meta Pixel หรือเครือข่ายโฆษณาใด ๆ</p>
    <p>This site runs on Cloudflare infrastructure, which acts as our data processor. We host our own fonts, and we don't use Google Analytics, the Meta Pixel, or any advertising networks.</p>
  </section>

  <section class="about-pair">
    <p class="th" lang="th">หากมีคำถามเกี่ยวกับความเป็นส่วนตัว หรือต้องการให้ลบข้อมูลของคุณ ติดต่อเราได้ที่ info@studyeventz.com</p>
    <p>If you have any questions about privacy or would like your data removed, contact us at <a href="mailto:info@studyeventz.com" style="color:var(--teal);font-weight:600">info@studyeventz.com</a>.</p>
  </section>

  <p class="privacy-updated">Last updated: June 2026.</p>
</main>

__NOTIFY_BANNER__

</body>
</html>
"""


def build_privacy_html(country: "Country") -> None:
    """Write <country.code>/privacy.html — a static localised Privacy page."""
    html = localize(PRIVACY_HTML, country)
    for ph, val in {
        "__SITE_URL__":       country.site_url,
        "__COUNTRY_SITE__":   country.site_url,
        "__COUNTRY_CODE__":   country.code,
        "__COUNTRY_NAME__":   country.name_en,
        "__COUNTRY_FLAG__":   country.flag,
        "__NOTIFY_BANNER__":  render_notify_banner(country),
    }.items():
        html = html.replace(ph, val)
    country.root.mkdir(parents=True, exist_ok=True)
    country.privacy_out.write_text(html, encoding="utf-8")


CONTACT_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Contact — studyeventz</title>
<meta name="description" content="Contact studyeventz to list a study abroad event, report a correction, or explore a partnership. Email __EMAIL__.">
<meta name="description" lang="th" content="ติดต่อ studyeventz เพื่อแจ้งเพิ่มงาน แจ้งแก้ไขข้อมูล หรือร่วมงานกับเรา">
<link rel="canonical" href="__SITE_URL__/contact.html">

<link rel="stylesheet" href="/assets/fonts/noto-sans-thai.css">

<style>
  :root {
    --teal: #0d7377; --teal-dark: #095a5d; --gold: #f4a825;
    --ink: #0d2233; --bg: #f5f7f9; --text: #1a2530;
    --muted: #5d6b78; --border: #e2e8ed;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         background: var(--bg); color: var(--text); line-height: 1.6;
         padding-bottom: 92px; }
  [lang="th"] { font-family: "Noto Sans Thai", "Sukhumvit Set", "Leelawadee UI",
                              -apple-system, BlinkMacSystemFont, sans-serif; }

  /* Header */
  .site-header { background: var(--teal); color: #fff; }
  .header-bar { max-width: 1180px; margin: 0 auto; padding: 1rem 1.5rem;
                display: flex; align-items: center; justify-content: space-between;
                gap: 1.5rem; flex-wrap: wrap; }
  .brand { font-size: 1.6rem; font-weight: 800; letter-spacing: -.02em; }
  .brand .gold { color: var(--gold); }
  .brand-link { display: inline-flex; align-items: center; gap: .55rem;
                text-decoration: none; color: inherit;
                transition: opacity .15s; }
  .brand-link:hover { opacity: .85; }
  .brand-flag { font-size: 1.3rem; line-height: 1; }
  .nav { display: flex; gap: .25rem; flex-wrap: wrap; }
  .nav a { color: rgba(255,255,255,.85); text-decoration: none;
           padding: .5rem .9rem; border-radius: 6px; font-size: .9rem; font-weight: 500;
           transition: background .15s, color .15s; }
  .nav a:hover { background: rgba(255,255,255,.12); color: #fff; }
  .nav a.active { background: rgba(255,255,255,.18); color: #fff; font-weight: 600; }

  /* Hero */
  .about-hero { background: var(--teal-dark); color: #fff; padding: 2rem 1.5rem 2.2rem; }
  .about-hero-inner { max-width: 760px; margin: 0 auto; }
  .about-hero h1 { font-size: 1.9rem; font-weight: 700; line-height: 1.2; margin: 0; }
  .about-hero .hero-th-title { color: var(--gold); font-size: 2.47rem; font-weight: 700;
                               line-height: 1.2; margin: 0 0 .35rem 0; letter-spacing: .01em; }

  /* Body */
  .about-content { max-width: 760px; margin: 0 auto; padding: 2.5rem 1.5rem 3rem; }

  /* Intro (with prominent email CTA) */
  .contact-intro { margin-bottom: 2.5rem; padding-bottom: 2.5rem;
                   border-bottom: 1px solid var(--border); }
  .contact-intro p { font-size: 1.05rem; margin: 0; }
  .contact-intro p.th { color: var(--text); font-weight: 500; margin-bottom: .5rem; }
  .contact-intro p:not(.th) { color: var(--muted); margin-bottom: 1.5rem; }
  .email-btn { display: inline-block; background: var(--teal); color: #fff;
               padding: .85rem 1.4rem; border-radius: 8px;
               font-size: 1.05rem; font-weight: 600; text-decoration: none;
               transition: background .15s; }
  .email-btn:hover { background: var(--teal-dark); }

  /* Sub-categories */
  .contact-category { margin-bottom: 2rem; }
  .contact-category .th-title { color: var(--gold); font-size: 1.2rem;
                                font-weight: 700; margin: 0 0 .1rem 0;
                                line-height: 1.3; }
  .contact-category .en-title { color: var(--teal); font-size: 1rem;
                                font-weight: 700; text-transform: uppercase;
                                letter-spacing: .04em; margin: 0 0 .65rem 0;
                                line-height: 1.3; }
  .contact-category p { font-size: 1rem; margin: 0; }
  .contact-category p.th { color: var(--text); margin-bottom: .35rem; }
  .contact-category p:not(.th) { color: var(--muted); }

  /* LINE banner */
  .line-banner { position: fixed; left: 0; right: 0; bottom: 0;
                 background: var(--teal); color: var(--gold);
                 padding: 1.25rem 1rem; z-index: 50;
                 box-shadow: 0 -2px 8px rgba(13,34,51,.15);
                 display: flex; align-items: center; justify-content: center; gap: .8rem; }
  .line-banner a { color: var(--gold); text-decoration: none; font-weight: 600; }
  .line-banner a:hover { text-decoration: underline; }
  .line-banner-text { font-size: 1.02rem; }
  .line-banner-handle { background: rgba(244, 168, 37, .15);
                        border: 1px solid rgba(244, 168, 37, .35);
                        padding: .2rem .65rem; border-radius: 14px;
                        font-size: .9rem; margin-left: .25rem; }
  .line-icon { width: 30px; height: 30px; flex-shrink: 0; }

  @media (max-width: 640px) {
    .header-bar { padding: .8rem 1rem; }
    .brand { font-size: 1.4rem; }
    .about-hero { padding: 1.5rem 1rem 1.7rem; }
    .about-hero h1 { font-size: 1.5rem; }
    .about-hero .hero-th-title { font-size: 1.95rem; }
    .about-content { padding: 2rem 1.1rem 2.5rem; }
    .contact-intro p, .contact-category p { font-size: .98rem; }
    .email-btn { font-size: 1rem; padding: .75rem 1.2rem; }
    .line-banner { padding: .9rem .8rem; gap: .5rem; flex-wrap: wrap; }
    .line-banner-text { font-size: .92rem; text-align: center; }
    .line-icon { width: 26px; height: 26px; }
    body { padding-bottom: 115px; }
  }
</style>
</head>
<body>

<header class="site-header">
  <div class="header-bar">
    <a class="brand-link" href="/?pick" aria-label="Change country"><span class="brand">studyevent<span class="gold">z</span></span><span class="brand-flag" aria-hidden="true">__COUNTRY_FLAG__</span></a>
    <nav class="nav">
      <a href="events.html">Events</a>
      <a href="about.html">About Us</a>
      <a href="contact.html" class="active">Contact Us</a>
      <a href="privacy.html">Privacy</a>
    </nav>
  </div>
</header>

<section class="about-hero">
  <div class="about-hero-inner">
    <p class="hero-th-title" lang="th">ติดต่อเรา</p>
    <h1>Contact Us</h1>
  </div>
</section>

<main class="about-content">

  <section class="contact-intro">
    <p class="th" lang="th">มีงานที่เราควรเพิ่มในรายการ พบข้อมูลที่ล้าสมัย หรืออยากร่วมงานกับเราใช่ไหม? อีเมลหาเราได้ที่ <a href="mailto:__EMAIL__" style="color:inherit;font-weight:600">__EMAIL__</a> แล้วเราจะติดต่อกลับไป</p>
    <p>Have an event we should list, spotted something out of date, or want to work with us? Email us at <a href="mailto:__EMAIL__" style="color:inherit;font-weight:600">__EMAIL__</a> and we'll get back to you.</p>
    <a class="email-btn" href="mailto:__EMAIL__">✉  __EMAIL__</a>
  </section>

  <section class="contact-category">
    <h2 class="th-title" lang="th">แจ้งเพิ่มกิจกรรม</h2>
    <h2 class="en-title">List an event</h2>
    <p class="th" lang="th">หากคุณกำลังจัดงานแฟร์เรียนต่อต่างประเทศ Open Day หรืองานให้ข้อมูล ส่งรายละเอียดมาให้เรา แล้วเราจะเพิ่มลงในรายการ</p>
    <p>Running a study abroad fair, open day or info session? Send us the details and we'll add it.</p>
    <p style="margin-top:.65rem"><a href="submit.html" style="color:var(--teal);font-weight:600;text-decoration:none;border-bottom:1px solid currentColor">→ ส่งงานเข้ามา / Submit your event</a></p>
  </section>

  <section class="contact-category">
    <h2 class="th-title" lang="th">แจ้งแก้ไขข้อมูล</h2>
    <h2 class="en-title">Report a correction</h2>
    <p class="th" lang="th">พบวันที่ผิด หรือลิงก์ใช้งานไม่ได้ใช่ไหม? แจ้งให้เราทราบ แล้วเราจะรีบแก้ไขให้</p>
    <p>Found a wrong date or a dead link? Let us know and we'll fix it.</p>
  </section>

  <section class="contact-category">
    <h2 class="th-title" lang="th">ความร่วมมือ</h2>
    <h2 class="en-title">Partnerships</h2>
    <p class="th" lang="th">หากคุณสนใจนำ studyeventz ไปเปิดในตลาดใหม่ หรืออยากร่วมมือกับเราในตลาดที่เราครอบคลุมอยู่แล้ว ติดต่อเราได้เลย</p>
    <p>If you'd like to bring studyeventz to a new market, or partner with us in one we already cover, get in touch.</p>
  </section>

</main>

__NOTIFY_BANNER__

</body>
</html>
"""


def build_contact_html(country: "Country") -> None:
    """Write <country.code>/contact.html — a static localised Contact page."""
    html = localize(CONTACT_HTML, country)
    for ph, val in {
        "__SITE_URL__":       SITE_URL,
        "__COUNTRY_SITE__":   country.site_url,
        "__COUNTRY_CODE__":   country.code,
        "__COUNTRY_NAME__":   country.name_en,
        "__COUNTRY_FLAG__":   country.flag,
        "__NOTIFY_BANNER__":  render_notify_banner(country),
        "__EMAIL__":          country.contact_email,
    }.items():
        html = html.replace(ph, val)
    country.root.mkdir(parents=True, exist_ok=True)
    country.contact_out.write_text(html, encoding="utf-8")


SUBMIT_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Submit an Event — studyeventz</title>
<meta name="description" content="Submit a study abroad event to studyeventz — university fair, info session, open day, webinar. Free for organizers in Thailand.">
<meta name="description" lang="th" content="แจ้งเพิ่มกิจกรรมเรียนต่อต่างประเทศใน studyeventz">
<link rel="canonical" href="__SITE_URL__/submit.html">
<meta name="robots" content="noindex">

<link rel="stylesheet" href="/assets/fonts/noto-sans-thai.css">

<style>
  :root {
    --teal: #0d7377; --teal-dark: #095a5d; --gold: #f4a825;
    --ink: #0d2233; --bg: #f5f7f9; --text: #1a2530;
    --muted: #5d6b78; --border: #e2e8ed; --error: #c0392b;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         background: var(--bg); color: var(--text); line-height: 1.6;
         padding-bottom: 92px; }
  [lang="th"] { font-family: "Noto Sans Thai", "Sukhumvit Set", "Leelawadee UI",
                              -apple-system, BlinkMacSystemFont, sans-serif; }

  .site-header { background: var(--teal); color: #fff; }
  .header-bar { max-width: 1180px; margin: 0 auto; padding: 1rem 1.5rem;
                display: flex; align-items: center; justify-content: space-between;
                gap: 1.5rem; flex-wrap: wrap; }
  .brand { font-size: 1.6rem; font-weight: 800; letter-spacing: -.02em; }
  .brand .gold { color: var(--gold); }
  .brand-link { display: inline-flex; align-items: center; gap: .55rem;
                text-decoration: none; color: inherit;
                transition: opacity .15s; }
  .brand-link:hover { opacity: .85; }
  .brand-flag { font-size: 1.3rem; line-height: 1; }
  .nav { display: flex; gap: .25rem; flex-wrap: wrap; }
  .nav a { color: rgba(255,255,255,.85); text-decoration: none;
           padding: .5rem .9rem; border-radius: 6px; font-size: .9rem; font-weight: 500;
           transition: background .15s, color .15s; }
  .nav a:hover { background: rgba(255,255,255,.12); color: #fff; }
  .nav a.active { background: rgba(255,255,255,.18); color: #fff; font-weight: 600; }

  .about-hero { background: var(--teal-dark); color: #fff; padding: 2rem 1.5rem 2.2rem; }
  .about-hero-inner { max-width: 760px; margin: 0 auto; }
  .about-hero h1 { font-size: 1.9rem; font-weight: 700; line-height: 1.2; margin: 0; }
  .about-hero .hero-th-title { color: var(--gold); font-size: 2.47rem; font-weight: 700;
                               line-height: 1.2; margin: 0 0 .35rem 0; letter-spacing: .01em; }
  .about-hero p.intro { opacity: .9; font-size: .95rem; margin-top: .5rem; }
  .about-hero p.intro.th { color: var(--gold); margin-top: .75rem; }

  .about-content { max-width: 760px; margin: 0 auto; padding: 2rem 1.5rem 3rem; }

  /* Form */
  .submit-form { background: #fff; border-radius: 10px; padding: 2rem;
                 box-shadow: 0 1px 3px rgba(13,34,51,.06); }
  .field-group { margin-bottom: 1.25rem; }
  .field-group:last-of-type { margin-bottom: 0; }
  .field-label { display: block; font-size: .85rem; font-weight: 600;
                 color: var(--text); margin-bottom: .35rem; }
  .field-label .th { color: var(--teal); font-weight: 700; }
  .field-label .req { color: var(--error); margin-left: .15rem; }
  .field-hint { font-size: .78rem; color: var(--muted); margin-top: .25rem; }
  input[type=text], input[type=email], input[type=url], input[type=date],
  input[type=time], textarea, select {
    width: 100%; padding: .65rem .85rem; border: 1px solid var(--border);
    border-radius: 6px; font-size: .95rem; font-family: inherit;
    color: var(--text); background: #fff; transition: border-color .15s;
  }
  textarea { resize: vertical; min-height: 80px; line-height: 1.45; }
  input:focus, textarea:focus { outline: none; border-color: var(--teal); }
  input.error, textarea.error { border-color: var(--error); }

  .field-row { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
  @media (max-width: 540px) { .field-row { grid-template-columns: 1fr; gap: 1.25rem; } }

  .form-section-title { font-size: .78rem; font-weight: 700; text-transform: uppercase;
                        letter-spacing: .08em; color: var(--muted);
                        margin: 2rem 0 1rem 0; padding-top: 1.5rem;
                        border-top: 1px solid var(--border); }
  .form-section-title:first-child { margin-top: 0; padding-top: 0; border: none; }

  .submit-btn { background: var(--teal); color: #fff; border: none;
                padding: .85rem 1.6rem; border-radius: 8px;
                font-size: 1.02rem; font-weight: 600; cursor: pointer;
                transition: background .15s; margin-top: 1.5rem; }
  .submit-btn:hover:not(:disabled) { background: var(--teal-dark); }
  .submit-btn:disabled { background: #cbd5d8; cursor: not-allowed; }

  .form-msg { padding: 1rem 1.2rem; border-radius: 8px; margin-top: 1rem;
              font-size: .92rem; display: none; }
  .form-msg.ok    { display: block; background: #e7f5ee; color: #1f7a3f;
                    border: 1px solid #b5dec5; }
  .form-msg.err   { display: block; background: #fde7e5; color: var(--error);
                    border: 1px solid #f1bbb5; }

  /* LINE banner */
  .line-banner { position: fixed; left: 0; right: 0; bottom: 0;
                 background: var(--teal); color: var(--gold);
                 padding: 1.25rem 1rem; z-index: 50;
                 box-shadow: 0 -2px 8px rgba(13,34,51,.15);
                 display: flex; align-items: center; justify-content: center; gap: .8rem; }
  .line-banner a { color: var(--gold); text-decoration: none; font-weight: 600; }
  .line-banner a:hover { text-decoration: underline; }
  .line-banner-text { font-size: 1.02rem; }
  .line-banner-handle { background: rgba(244, 168, 37, .15);
                        border: 1px solid rgba(244, 168, 37, .35);
                        padding: .2rem .65rem; border-radius: 14px;
                        font-size: .9rem; margin-left: .25rem; }
  .line-icon { width: 30px; height: 30px; flex-shrink: 0; }

  @media (max-width: 640px) {
    .header-bar { padding: .8rem 1rem; }
    .brand { font-size: 1.4rem; }
    .about-hero { padding: 1.5rem 1rem 1.7rem; }
    .about-hero h1 { font-size: 1.5rem; }
    .about-hero .hero-th-title { font-size: 1.95rem; }
    .about-content { padding: 1.5rem 1rem 2.5rem; }
    .submit-form { padding: 1.5rem 1.2rem; }
    .line-banner { padding: .9rem .8rem; gap: .5rem; flex-wrap: wrap; }
    .line-banner-text { font-size: .92rem; text-align: center; }
    .line-icon { width: 26px; height: 26px; }
    body { padding-bottom: 115px; }
  }
</style>
</head>
<body>

<header class="site-header">
  <div class="header-bar">
    <a class="brand-link" href="/?pick" aria-label="Change country"><span class="brand">studyevent<span class="gold">z</span></span><span class="brand-flag" aria-hidden="true">__COUNTRY_FLAG__</span></a>
    <nav class="nav">
      <a href="events.html">Events</a>
      <a href="about.html">About Us</a>
      <a href="contact.html">Contact Us</a>
      <a href="privacy.html">Privacy</a>
    </nav>
  </div>
</header>

<section class="about-hero">
  <div class="about-hero-inner">
    <p class="hero-th-title" lang="th">แจ้งเพิ่มกิจกรรม</p>
    <h1>Submit an Event</h1>
    <p class="intro th" lang="th">กรอกรายละเอียดด้านล่าง เราจะตรวจสอบและเพิ่มลงในรายการของเรา ฟรี ไม่มีค่าใช้จ่าย</p>
    <p class="intro">Fill in the details below. We'll review and add it to the listings. Free for organizers.</p>
  </div>
</section>

<main class="about-content">
  <form id="submit-form" class="submit-form" novalidate>

    <div class="form-section-title">รายละเอียดกิจกรรม / Event details</div>

    <div class="field-group">
      <label class="field-label" for="f-organizer">
        <span class="th" lang="th">ผู้จัด</span> / Organizer <span class="req">*</span>
      </label>
      <input type="text" id="f-organizer" name="organizer" required maxlength="300"
             placeholder="e.g. IDP Education, BRIT Education UK, Hands On Education Consultants">
    </div>

    <div class="field-group">
      <label class="field-label" for="f-event-name">
        <span class="th" lang="th">ชื่อกิจกรรม</span> / Event name <span class="req">*</span>
      </label>
      <input type="text" id="f-event-name" name="event_name" required maxlength="500"
             placeholder="e.g. UK Study Day: Last Ticket to UK!">
    </div>

    <div class="field-row">
      <div class="field-group">
        <label class="field-label" for="f-event-date">
          <span class="th" lang="th">วันที่</span> / Date <span class="req">*</span>
        </label>
        <input type="date" id="f-event-date" name="event_date" required>
      </div>
      <div class="field-group">
        <label class="field-label" for="f-event-time">
          <span class="th" lang="th">เวลา</span> / Time
        </label>
        <input type="text" id="f-event-time" name="event_time" maxlength="50"
               placeholder="e.g. 14:00 - 16:00">
      </div>
    </div>

    <div class="field-group">
      <label class="field-label" for="f-location">
        <span class="th" lang="th">สถานที่</span> / Location
      </label>
      <input type="text" id="f-location" name="location" maxlength="300"
             placeholder='e.g. "Bangkok, Thailand" or "Online"'>
    </div>

    <div class="field-group">
      <label class="field-label" for="f-url">
        <span class="th" lang="th">ลิงก์ลงทะเบียน</span> / Registration URL <span class="req">*</span>
      </label>
      <input type="url" id="f-url" name="registration_url" required maxlength="1000"
             placeholder="https://...">
      <p class="field-hint">A landing page where attendees can find more details or register.</p>
    </div>

    <div class="form-section-title">ข้อมูลผู้แจ้ง / Submitter info <small style="font-weight:400;text-transform:none">— optional</small></div>

    <div class="field-row">
      <div class="field-group">
        <label class="field-label" for="f-name">
          <span class="th" lang="th">ชื่อ</span> / Your name
        </label>
        <input type="text" id="f-name" name="submitter_name" maxlength="200">
      </div>
      <div class="field-group">
        <label class="field-label" for="f-email">
          <span class="th" lang="th">อีเมล</span> / Email
        </label>
        <input type="email" id="f-email" name="submitter_email" maxlength="300"
               placeholder="we'll only email if we have a question">
      </div>
    </div>

    <div class="field-group">
      <label class="field-label" for="f-notes">
        <span class="th" lang="th">หมายเหตุเพิ่มเติม</span> / Notes
      </label>
      <textarea id="f-notes" name="notes" maxlength="2000"
                placeholder="Anything else we should know?"></textarea>
    </div>

    <button type="submit" id="submit-btn" class="submit-btn">
      ส่ง / Submit
    </button>
    <div id="form-msg" class="form-msg" role="status" aria-live="polite"></div>

  </form>
</main>

__NOTIFY_BANNER__

<script>
const SUBMIT_URL = "__SUBMIT_URL__";
const SITE_KEY   = "__SITE_KEY__";

const form = document.getElementById('submit-form');
const btn  = document.getElementById('submit-btn');
const msg  = document.getElementById('form-msg');

function showMsg(kind, text) {
  msg.className = 'form-msg ' + kind;
  msg.textContent = text;
}

function clearFieldErrors() {
  form.querySelectorAll('input, textarea').forEach(el => el.classList.remove('error'));
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  clearFieldErrors();
  msg.className = 'form-msg';
  msg.textContent = '';

  if (!SUBMIT_URL) {
    showMsg('err', 'Submission endpoint is not configured yet. Please email us at info@studyeventz.com.');
    return;
  }

  const body = {
    country:          "__COUNTRY_CODE__",
    organizer:        form.organizer.value.trim(),
    event_name:       form.event_name.value.trim(),
    event_date:       form.event_date.value.trim(),
    event_time:       form.event_time.value.trim(),
    location:         form.location.value.trim(),
    registration_url: form.registration_url.value.trim(),
    submitter_name:   form.submitter_name.value.trim(),
    submitter_email:  form.submitter_email.value.trim(),
    notes:            form.notes.value.trim(),
  };

  btn.disabled = true;
  btn.textContent = 'Sending…';

  try {
    const url = SUBMIT_URL + (SUBMIT_URL.includes('?') ? '&' : '?') + 'k=' + encodeURIComponent(SITE_KEY);
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      form.style.display = 'none';
      showMsg('ok', 'Thanks! We\'ve received your submission and will review it shortly. ขอบคุณค่ะ');
    } else if (data && data.error === 'validation_failed' && data.fields) {
      // Highlight broken fields
      Object.keys(data.fields).forEach(f => {
        const el = form.querySelector(`[name="${f}"]`);
        if (el) el.classList.add('error');
      });
      showMsg('err', 'Please check the highlighted fields and try again.');
    } else {
      showMsg('err', 'Could not submit. Please try again or email info@studyeventz.com.');
    }
  } catch (err) {
    showMsg('err', 'Network error — please try again or email info@studyeventz.com.');
  } finally {
    btn.disabled = false;
    btn.textContent = 'ส่ง / Submit';
  }
});
</script>
</body>
</html>
"""


def build_submit_html(country: "Country") -> None:
    """Write <country.code>/submit.html — a localised event-submission form."""
    html = localize(SUBMIT_HTML, country)
    # Both old (/track) and new (/i) path names are supported on the Worker;
    # derive the matching submit endpoint by swapping the last segment.
    if not INGEST_URL:
        submit_url = ""
    elif INGEST_URL.endswith("/i"):
        submit_url = INGEST_URL[:-2] + "/s"
    else:
        submit_url = INGEST_URL.replace("/track", "/submit")
    for ph, val in {
        "__SITE_URL__":       SITE_URL,
        "__COUNTRY_SITE__":   country.site_url,
        "__COUNTRY_CODE__":   country.code,
        "__COUNTRY_NAME__":   country.name_en,
        "__COUNTRY_FLAG__":   country.flag,
        "__NOTIFY_BANNER__":  render_notify_banner(country),
        "__SUBMIT_URL__":     submit_url,
        "__SITE_KEY__":       SITE_KEY,
        "__EMAIL__":          country.contact_email,
    }.items():
        html = html.replace(ph, val)
    country.root.mkdir(parents=True, exist_ok=True)
    country.submit_out.write_text(html, encoding="utf-8")


def build_html(country: "Country") -> tuple[int, str]:
    """Render <country.code>/events.html. Returns (count, mode)."""
    images = discover_character_images()
    if images:
        characters = images
        mode = "png"
        # og:image is an absolute URL; images[] are absolute paths beginning with /
        og_image = f"{SITE_URL}{images[0]}"
    else:
        characters = [{"svg": s} for s in CHARACTER_SVGS]
        mode = "svg-fallback"
        og_image = country.events_url

    # Load freshly-written country events.json so the JSON-LD reflects this build
    try:
        events_data = json.loads(country.json_out.read_text(encoding="utf-8")).get("events", [])
    except Exception:
        events_data = []
    json_ld = build_event_json_ld(events_data, country)

    local_chip = (
        f'<button class="chip" data-filter="local">{country.local_filter_label} '
        f'<span class="count" data-count="local">0</span></button>'
    ) if country.local_filter_label else ""

    replacements = {
        "__PAGE_TITLE__":      country.title,
        "__META_DESC_EN__":    country.meta_desc_en,
        "__META_DESC_TH__":    country.meta_desc_native,
        "__EVENTS_PAGE__":     country.events_url,
        "__SITE_URL__":        SITE_URL,
        "__COUNTRY_CODE__":    country.code,
        "__COUNTRY_NAME__":    country.name_en,
        "__COUNTRY_NATIVE__":  country.name_native,
        "__COUNTRY_FLAG__":    country.flag,
        "__COUNTRY_LANG__":    country.primary_lang,
        "__TIMEZONE__":        country.timezone,
        "__OG_IMAGE__":        og_image,
        "__NOTIFY_BANNER__":   render_notify_banner(country),
        "__INGEST_URL__":      INGEST_URL,
        "__SITE_KEY__":        SITE_KEY,
        "__LINE_HANDLE__":     country.line_handle,
        "__LOCAL_FILTER_CHIP__": local_chip,
        "__LOCAL_MATCH__":     country.local_filter_match,
        "__JSON_LD__":         json_ld,
        "__CHARACTERS_JSON__": json.dumps(characters),
    }
    # Localise native (Thai) copy first, then substitute placeholders so the
    # injected banner and event data are not re-processed by the translator.
    html = localize(HTML, country)
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)
    country.root.mkdir(parents=True, exist_ok=True)
    country.html_out.write_text(html, encoding="utf-8")
    return len(characters), mode


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>studyeventz — Study Abroad Events</title>
<meta name="description" content="studyeventz aggregates study abroad events — fairs, webinars and information sessions. Pick your market.">
<link rel="canonical" href="__SITE_URL__/">

<link rel="stylesheet" href="/assets/fonts/noto-sans-thai.css">

<style>
  :root {
    --teal: #0d7377; --teal-dark: #095a5d; --gold: #f4a825;
    --ink: #0d2233; --bg: #f5f7f9; --text: #1a2530;
    --muted: #5d6b78; --border: #e2e8ed;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         background: var(--teal-dark); color: #fff; min-height: 100vh;
         display: flex; flex-direction: column; align-items: center; justify-content: center;
         padding: 2rem 1.5rem; }
  [lang="th"] { font-family: "Noto Sans Thai", "Sukhumvit Set", "Leelawadee UI",
                              -apple-system, BlinkMacSystemFont, sans-serif; }

  .picker { max-width: 540px; width: 100%; text-align: center; }
  .brand { font-size: 2.2rem; font-weight: 800; letter-spacing: -.02em;
           color: #fff; margin-bottom: .5rem; }
  .brand .gold { color: var(--gold); }
  .tagline { color: rgba(255,255,255,.85); font-size: 1.05rem; margin-bottom: 2.5rem; }
  .tagline-th { color: var(--gold); font-size: 1.15rem; margin-top: .35rem;
                font-weight: 500; }

  .picker-prompt { font-size: .78rem; font-weight: 600; text-transform: uppercase;
                   letter-spacing: .12em; color: rgba(255,255,255,.7);
                   margin-bottom: 1rem; }

  .country-grid { display: grid; gap: .8rem; }
  .country-tile { background: rgba(255,255,255,.08);
                  border: 1px solid rgba(255,255,255,.15);
                  border-radius: 12px;
                  padding: 1.3rem 1.5rem;
                  display: flex; align-items: center; gap: 1.1rem;
                  color: #fff; text-decoration: none;
                  transition: background .15s, transform .15s, border-color .15s; }
  .country-tile:hover { background: rgba(255,255,255,.13);
                        border-color: rgba(244, 168, 37, .5);
                        transform: translateY(-1px); }
  .tile-flag { font-size: 2.4rem; line-height: 1; flex-shrink: 0; }
  .tile-text { text-align: left; flex: 1; }
  .tile-name { font-size: 1.15rem; font-weight: 700; }
  .tile-native { color: var(--gold); font-size: .95rem; font-weight: 500; margin-top: .1rem; }
  .tile-arrow { font-size: 1.4rem; color: var(--gold); opacity: .8; }

  .coming-soon { background: transparent;
                 border: 1px dashed rgba(255,255,255,.2);
                 color: rgba(255,255,255,.55);
                 cursor: default; }
  .coming-soon:hover { background: transparent; transform: none;
                       border-color: rgba(255,255,255,.2); }
  .coming-soon .tile-arrow { display: none; }

  .meta { color: rgba(255,255,255,.5); font-size: .78rem;
          text-align: center; margin-top: 2.5rem; }

  @media (max-width: 540px) {
    .brand { font-size: 1.85rem; }
    .tile-flag { font-size: 2rem; }
    .tile-name { font-size: 1.05rem; }
  }
</style>
</head>
<body>

<main class="picker">
  <div class="brand">studyevent<span class="gold">z</span></div>
  <p class="tagline">An independent guide to study abroad events.</p>
  <p class="tagline-th" lang="th">คู่มืออิสระสำหรับค้นหากิจกรรมเรียนต่อต่างประเทศ</p>

  <p class="picker-prompt">เลือกตลาด / Choose your market</p>
  <div class="country-grid" id="country-grid">
__COUNTRY_TILES__
  </div>

  <p class="meta">More markets coming soon.</p>
</main>

<script>
  // Auto-redirect returning visitors to their last-chosen country.
  // First-time visitors see the picker. The "Change country" link points to
  // /?pick — any query string forces the picker so it stays reachable.
  try {
    const saved = localStorage.getItem('studyeventz_country');
    if (!location.search && saved && /^[a-z\-]+$/.test(saved)) {
      // Confirm we actually built that country (anti-stale-cache check)
      const tile = document.querySelector(`[data-country="${saved}"]`);
      if (tile) location.replace(`/${saved}/events.html`);
    }
  } catch (e) {}

  document.querySelectorAll('.country-tile[data-country]').forEach(el => {
    el.addEventListener('click', () => {
      try { localStorage.setItem('studyeventz_country', el.dataset.country); } catch (e) {}
    });
  });
</script>
</body>
</html>
"""


def build_index_html() -> None:
    """Write the root index.html country picker."""
    tiles: list[str] = []
    for c in COUNTRIES:
        tiles.append(
            f"""    <a class="country-tile" data-country="{c.code}" href="/{c.code}/events.html">
      <span class="tile-flag" aria-hidden="true">{c.flag}</span>
      <span class="tile-text">
        <span class="tile-name">{c.name_en}</span>
        <span class="tile-native" lang="{c.primary_lang}">{c.name_native}</span>
      </span>
      <span class="tile-arrow" aria-hidden="true">→</span>
    </a>"""
        )
    # A placeholder "more soon" tile so the grid feels less empty with 1 market
    if len(COUNTRIES) == 1:
        tiles.append(
            """    <div class="country-tile coming-soon" aria-disabled="true">
      <span class="tile-flag" aria-hidden="true">🌏</span>
      <span class="tile-text">
        <span class="tile-name">Vietnam, India and more</span>
        <span class="tile-native">coming soon</span>
      </span>
    </div>"""
        )
    html = INDEX_HTML.replace("__COUNTRY_TILES__", "\n".join(tiles))
    html = html.replace("__SITE_URL__", SITE_URL)
    INDEX_OUT.write_text(html, encoding="utf-8")


# Legacy redirect shims at the old root paths so inbound links don't 404.
LEGACY_REDIRECT_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Redirecting…</title>
<meta http-equiv="refresh" content="0; url=/__DEFAULT_COUNTRY__/__PAGE__">
<link rel="canonical" href="__SITE_URL__/__DEFAULT_COUNTRY__/__PAGE__">
<meta name="robots" content="noindex">
<script>
  try {
    const saved = localStorage.getItem('studyeventz_country');
    const country = (saved && /^[a-z\-]+$/.test(saved)) ? saved : '__DEFAULT_COUNTRY__';
    location.replace('/' + country + '/__PAGE__');
  } catch (e) {
    location.replace('/__DEFAULT_COUNTRY__/__PAGE__');
  }
</script>
</head>
<body>
<p>Redirecting to <a href="/__DEFAULT_COUNTRY__/__PAGE__">studyeventz</a>…</p>
</body>
</html>
"""


def build_legacy_redirects() -> None:
    """Write root-level redirect shims for the old single-country URLs.
    Default to the first COUNTRIES entry; JS swaps to the user's saved choice."""
    default = COUNTRIES[0].code
    for page in LEGACY_PAGES:
        html = (LEGACY_REDIRECT_HTML
                .replace("__DEFAULT_COUNTRY__", default)
                .replace("__PAGE__", page)
                .replace("__SITE_URL__", SITE_URL))
        (ROOT / page).write_text(html, encoding="utf-8")
    # Also stale data/events.json — replace with a small note
    legacy_data = ROOT / "data" / "events.json"
    if legacy_data.exists():
        legacy_data.write_text(
            json.dumps({
                "note": "This file has moved to /<country>/data/events.json — see /index.html",
                "countries": [c.code for c in COUNTRIES],
            }, indent=2),
            encoding="utf-8",
        )


def ensure_pngquant() -> bool:
    """Return True if pngquant is on PATH, installing via brew if needed."""
    if shutil.which("pngquant"):
        return True
    if shutil.which("brew") is None:
        print(
            "ERROR: pngquant not found and Homebrew is unavailable to install it. "
            "Install pngquant manually and retry.",
            file=sys.stderr,
        )
        return False
    print("pngquant not found — installing via Homebrew …", flush=True)
    result = subprocess.run(["brew", "install", "pngquant"])
    if result.returncode != 0:
        print("ERROR: 'brew install pngquant' failed.", file=sys.stderr)
        return False
    return shutil.which("pngquant") is not None


def _compress_png(png: Path) -> tuple[int, int, int]:
    """Run pngquant on one PNG. Returns (before_bytes, after_bytes, exit_code)."""
    before = png.stat().st_size
    # --quality=70: max quality 70 (best-effort, may exit 99 if it can't hit it)
    # --skip-if-larger: keep original if compression made it bigger
    # --force --ext .png: overwrite the same filename atomically
    result = subprocess.run(
        [
            "pngquant",
            "--quality=70",
            "--skip-if-larger",
            "--force",
            "--ext", ".png",
            str(png),
        ],
        capture_output=True,
        text=True,
    )
    after = png.stat().st_size
    # Exit codes:
    #   0  = success
    #   98 = cannot save (typically: result would be larger than input — original kept by --skip-if-larger)
    #   99 = couldn't meet quality target — original kept
    # Treat all three as success; anything else is a real failure.
    if result.returncode not in (0, 98, 99):
        print(
            f"  {png.name}: pngquant failed (exit {result.returncode}): {result.stderr.strip()}",
            file=sys.stderr,
        )
    return before, after, result.returncode


def optimize_images() -> None:
    """Run pngquant on every PNG under assets/characters/ and assets/logos/,
    overwriting in place. SVGs are left alone (pngquant only handles PNGs)."""
    targets: list[tuple[str, Path, list[Path]]] = []
    if CHARACTERS_DIR.exists():
        pngs = sorted(CHARACTERS_DIR.rglob("*.png"), key=lambda p: _natural_key(p.name))
        targets.append(("assets/characters/", CHARACTERS_DIR, pngs))
    if LOGOS_DIR.exists():
        pngs = sorted(LOGOS_DIR.rglob("*.png"), key=lambda p: _natural_key(p.name))
        targets.append(("assets/logos/", LOGOS_DIR, pngs))

    total_pngs = sum(len(pngs) for _, _, pngs in targets)
    if total_pngs == 0:
        print("No PNGs found in assets/characters/ or assets/logos/ — nothing to optimize.")
        return
    if not ensure_pngquant():
        sys.exit(1)

    print(f"Optimizing {total_pngs} PNG(s) with pngquant (target quality 70) …")
    grand_before = grand_after = 0
    for label, root, pngs in targets:
        if not pngs:
            continue
        print(f"\n  [{label}] {len(pngs)} file(s):")
        section_before = section_after = 0
        for png in pngs:
            before, after, code = _compress_png(png)
            rel = png.relative_to(root)
            pct = (1 - after / before) * 100 if before else 0.0
            if code == 99:
                note = " (kept original — couldn't reach quality target)"
            elif code == 98:
                note = " (already optimized — no further gain)"
            else:
                note = ""
            print(f"    {rel}: {before // 1024} KB → {after // 1024} KB ({pct:+.0f}%){note}")
            section_before += before
            section_after += after
        if section_before:
            pct = (1 - section_after / section_before) * 100
            print(f"    Subtotal: {section_before // 1024} KB → {section_after // 1024} KB ({pct:+.0f}%)")
        grand_before += section_before
        grand_after += section_after
    if grand_before:
        pct = (1 - grand_after / grand_before) * 100
        print(f"\n  Grand total: {grand_before // 1024} KB → {grand_after // 1024} KB ({pct:+.0f}%)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--optimize",
        action="store_true",
        help="Run pngquant on assets/characters/ before building (overwrites in place).",
    )
    args = ap.parse_args()

    if args.optimize:
        optimize_images()

    grand_total_events = 0
    for c in COUNTRIES:
        n = export_events_json(c)
        char_count, mode = build_html(c)
        build_about_html(c)
        build_contact_html(c)
        build_submit_html(c)
        build_privacy_html(c)
        grand_total_events += n
        print(f"[{c.code}] {n} events, {char_count} characters ({mode})")

    build_index_html()
    build_legacy_redirects()
    write_seo_files()
    print(f"Wrote {INDEX_OUT}")
    print(f"Wrote legacy redirect shims for: {', '.join(LEGACY_PAGES)}")
    print(f"Wrote {SITEMAP_OUT} and {ROBOTS_OUT}")
    print(f"\nTotal events across {len(COUNTRIES)} country(ies): {grand_total_events}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
