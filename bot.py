import contextvars
import io
import json
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import openpyxl
import requests
import vk_api
from vk_api.bot_longpoll import CHAT_START_ID, VkBotLongPoll, VkBotEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.upload import VkUpload

from db_backend import (
    DB_PATH,
    USE_PG,
    backend_label,
    db_connect,
    ensure_column,
    insert_returning_id,
    pg_ddl_init,
    table_column_names,
)

_BASE = os.path.dirname(os.path.abspath(__file__))

VK_TOKEN = (os.environ.get("VK_TOKEN") or "").strip()
_GROUP_ID_RAW = (os.environ.get("VK_GROUP_ID") or "").strip()
GROUP_ID = 0
if _GROUP_ID_RAW:
    try:
        GROUP_ID = int(_GROUP_ID_RAW)
    except ValueError:
        GROUP_ID = 0
GROUP_ID_ABS = abs(GROUP_ID) if GROUP_ID else 0
# Дополнительные VK user id, кому разрешена /stats (через запятую), если API менеджеров недоступен
_STATS_ADMIN_IDS_RAW = (os.environ.get("STATS_ADMIN_IDS") or "").strip()
STATS_ADMIN_IDS: set[int] = set()
for _part in _STATS_ADMIN_IDS_RAW.split(","):
    _p = _part.strip()
    if not _p:
        continue
    try:
        STATS_ADMIN_IDS.add(int(_p))
    except ValueError:
        pass

_MAX_STATS_ADMIN_IDS_RAW = (os.environ.get("MAX_STATS_ADMIN_IDS") or "").strip()
MAX_STATS_ADMIN_IDS: set[int] = set()
for _part in _MAX_STATS_ADMIN_IDS_RAW.split(","):
    _p = _part.strip()
    if not _p:
        continue
    try:
        MAX_STATS_ADMIN_IDS.add(int(_p))
    except ValueError:
        pass

STATS_DEBUG = (os.environ.get("STATS_DEBUG") or "").strip().lower() in ("1", "true", "yes")
# Секретная фраза (см. _stats_secret_matches после определения _strip_command_text)
_STATS_EXPORT_SECRET_RAW = (os.environ.get("STATS_EXPORT_SECRET") or "").strip()

# SQLite: SQLITE_PATH=/data/career_bot.db (том Railway). PostgreSQL: DATABASE_URL из сервиса БД.
KLIMOV_SELF_PATH = os.path.join(_BASE, "klimov_self_table_questions.json")
OPG_PATH = os.path.join(_BASE, "opg_questions.json")
JOVASHI_PATH = os.path.join(_BASE, "jovashi_questions.json")
YOVASHI_PATH = os.path.join(_BASE, "yovashi_questions.json")
KETTELL_PATH = os.path.join(_BASE, "kettell_questions.json")
KETTELL_16PF_C_YOUTH_PATH = os.path.join(_BASE, "kettell_16pf_c_youth.json")
KOT_PATH = os.path.join(_BASE, "kot_questions.json")
KOT_QUESTION_IMAGE_PATHS: dict[int, str] = {
    16: os.path.join(_BASE, "assets", "kot_question_17.png"),  # вопрос 17 (шаг 16)
    28: os.path.join(_BASE, "assets", "kot_question_29.png"),  # вопрос 29 (шаг 28)
    31: os.path.join(_BASE, "assets", "kot_question_32.png"),  # вопрос 32 (шаг 31)
    48: os.path.join(_BASE, "assets", "kot_question_49.png"),  # вопрос 49 (шаг 48)
}
EN60_PATH = os.path.join(_BASE, "en60_questions.json")
EN57_PATH = os.path.join(_BASE, "en57_questions.json")
HOLLAND_RIASEC_PATH = os.path.join(_BASE, "holland_riasec_questions.json")

REMINDER_CHECK_EVERY_SEC = 60
REMINDER_AFTER_INACTIVE_MIN = 60
REMINDER_REPEAT_MIN = 24 * 60

# Long Poll: VK держит соединение до `wait` секунд; в vk_api таймаут запроса = wait + 10.
LONGPOLL_WAIT_SEC = int(os.environ.get("VK_LONGPOLL_WAIT", "50"))
LONGPOLL_RETRY_SLEEP_SEC = float(os.environ.get("VK_LONGPOLL_RETRY_SLEEP", "2"))

_LONGPOLL_TRANSIENT = (
    requests.exceptions.ReadTimeout,
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)

# peer_id для vk.messages.send (диалог или беседа); в reminder-потоке не задан
_REPLY_PEER_ID: contextvars.ContextVar[int | None] = contextvars.ContextVar("reply_peer_id", default=None)

# Защита от двойной доставки одного входящего (два Long Poll, перезапуск, разные поля id/cmid)
_LONGPOLL_SEEN_MSG: dict[str, float] = {}
_LONGPOLL_DEDUP_MEM_SEC = 30.0
_LP_DEDUP_TBL = "longpoll_incoming_dedup"
_LP_DEDUP_CLEAN_EVERY = 200
_lp_dedup_cleanup_counter = 0


def _longpoll_dedup_keys(peer_id: int, msg: dict) -> list[str]:
    """Несколько ключей на одно событие: иногда дубли приходят с разными id при одном cmid (и наоборот)."""
    p = int(peer_id)
    out: list[str] = []
    if msg.get("conversation_message_id") is not None:
        out.append(f"{p}:c:{int(msg['conversation_message_id'])}")
    if msg.get("id") is not None:
        out.append(f"{p}:i:{int(msg['id'])}")
    return out


def _longpoll_should_handle_message(peer_id: int, msg: dict) -> bool:
    keys = _longpoll_dedup_keys(peer_id, msg)
    if not keys:
        return True
    now = time.time()
    cutoff = now - _LONGPOLL_DEDUP_MEM_SEC
    for k_mem, t in list(_LONGPOLL_SEEN_MSG.items()):
        if t < cutoff:
            del _LONGPOLL_SEEN_MSG[k_mem]
    for k in keys:
        if k in _LONGPOLL_SEEN_MSG:
            return False
    with db_connect() as conn:
        cur = conn.cursor()
        ts = int(now)
        try:
            for k in keys:
                cur.execute(
                    f"""
                    INSERT OR IGNORE INTO {_LP_DEDUP_TBL} (dedup_key, seen_at)
                    VALUES (?, ?)
                    """,
                    (k, ts),
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return False
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[longpoll dedup insert] {e}")
            return False
    for k in keys:
        _LONGPOLL_SEEN_MSG[k] = now
    global _lp_dedup_cleanup_counter
    _lp_dedup_cleanup_counter += 1
    if _lp_dedup_cleanup_counter % _LP_DEDUP_CLEAN_EVERY == 0:
        try:
            old = int(now) - 86400 * 2
            with db_connect() as conn2:
                conn2.execute(f"DELETE FROM {_LP_DEDUP_TBL} WHERE seen_at < ?", (old,))
                conn2.commit()
        except Exception as e:
            print(f"[longpoll dedup cleanup] {e}")
    return True


def _peer_id_for_send(fallback_from_id: int) -> int:
    p = _REPLY_PEER_ID.get()
    return p if p is not None else fallback_from_id

TEST_KLIMOV_SELF = "klimov_self"
TEST_OPG = "opg"
TEST_JOVASHI = "jovashi"
TEST_YOVASHI = "yovashi"
TEST_KETTELL = "kettell"
TEST_KETTELL_16PF_C_YOUTH = "kettell_16pf_c"
TEST_KOT = "kot"
TEST_EN60 = "en60"
TEST_EN57 = "en57"
TEST_HOLLAND_RIASEC = "holland_riasec"
LEGACY_HOLLAND = "holland"

LABEL_OPG = "ОПГ (опросник профессиональной готовности)"
LABEL_PROF_TABLE = (
    "Таблица для ориентировочного определения предпочтительности типа будущей профессии"
)
LABEL_KETTELL = "Кеттелл 16PF"
LABEL_KETTELL_16PF_C_YOUTH = "Кеттелл 16PF/C (молодёжь)"
LABEL_KOT = "КОТ (краткий ориентировочный тест)"
LABEL_EN60 = "ЭН - 60"
LABEL_EN57 = "ЭН - 57"
LABEL_HOLLAND = "Голланд (RIASEC, пары профессий)"
LABEL_YOVASHI = "Йовайши (проф. склонности, модиф. Резапкиной)"
LABEL_KLIMOV_SELF = "ДДО"

# Подписи на кнопках клавиатуры (лимит ВК)
KB_KLIMOV_SELF = "ДДО"
KB_OPG = "ОПГ"
KB_PROF_TABLE = "Таблица (ОПТ проф.)"
KB_YOVASHI = "Йовайши"
KB_KETTELL = "Кеттелл 16PF"
KB_KETTELL_16PF_C_YOUTH = "16PF/C (мол.)"
KB_KOT = "КОТ"
KB_EN60 = "ЭН - 60"
KB_EN57 = "ЭН - 57"
KB_HOLLAND = "Голланд"

# Текст после любого успешно завершённого теста (ниже блока с результатами)
POST_TEST_REFERRAL_TRUDVSEM = (
    "Если Вам нужен полный разбор после прохождения теста, Вы можете подать заявление на профориентацию через "
    "портал «Работа России» (раздел про профориентацию: https://trudvsem.ru/information-pages/service-professional-orientation) "
    "или обратиться лично к специалисту-профконсультанту в Кадровый центр «Работа России» города Ижевска "
    "и Завьяловского района."
)

CAREER_HINTS_DDO = {
    "Ч-П": "🌿 Человек-Природа\n\nПрофессии, связанные с природой и живыми системами: биолог, эколог, ветеринар, агроном, лесник.\n\nРекомендация: биология, география, экология.",
    "Ч-Т": "🔧 Человек-Техника\n\nРабота с техникой, механизмами, производством: инженер, техник, программист, монтажник.\n\nРекомендация: математика, физика, информатика, черчение.",
    "Ч-Ч": "👥 Человек — другие люди\n\nОбщение, обучение, сервис, помощь: педагог, врач, психолог, менеджер по персоналу, юрист.\n\nРекомендация: развивайте коммуникацию и эмпатию.",
    "Ч-З": "📊 Человек-Знаковая система\n\nТексты, цифры, схемы, документы: бухгалтер, аналитик, переводчик, экономист, программист.\n\nРекомендация: математика, языки, внимание к деталям.",
    "Ч-Х": "🎨 Человек-Художественный образ\n\nТворчество и эстетика: дизайнер, художник, актёр, музыкант, режиссёр.\n\nРекомендация: искусство, литература, творческие практики.",
}

CAREER_HINTS_KLIMOV_SELF = {
    "П": CAREER_HINTS_DDO["Ч-П"],
    "Т": CAREER_HINTS_DDO["Ч-Т"],
    "З": CAREER_HINTS_DDO["Ч-З"],
    "Х": CAREER_HINTS_DDO["Ч-Х"],
    "Ч": CAREER_HINTS_DDO["Ч-Ч"],
}


def _load_questions(path: str):
    with open(path, encoding="utf-8") as _f:
        return json.load(_f)


# --- ОПГ и др.: полные ключи столбцов бланка Климова (Ч-П … Ч-Ч).
PROFESSION_TYPES = {
    "Ч-П": "Человек-Природа",
    "Ч-Т": "Человек-Техника",
    "Ч-Ч": "Человек — другие люди",
    "Ч-З": "Человек-Знаковая система",
    "Ч-Х": "Человек-Художественный образ",
}

# --- Таблица самооценки Климова: 30 утверждений, согласие начисляет 1 или 2 балла в столбец П/Т/З/Х/Ч.
KLIMOV_SELF_TYPES = {
    "П": "Человек — природа",
    "Т": "Человек — техника",
    "З": "Человек — знаковая система",
    "Х": "Человек — художественный образ",
    "Ч": "Человек — человек",
}

QUESTIONS_KLIMOV_SELF = _load_questions(KLIMOV_SELF_PATH)


def _klimov_self_max_by_category() -> dict[str, int]:
    m = {k: 0 for k in KLIMOV_SELF_TYPES}
    for q in QUESTIONS_KLIMOV_SELF:
        for opt in q["options"].values():
            if isinstance(opt, (list, tuple)) and len(opt) >= 2 and isinstance(opt[1], dict):
                for k, v in opt[1].items():
                    if k in m:
                        m[k] += int(v)
    return m


KLIMOV_SELF_MAX_BY_CATEGORY = _klimov_self_max_by_category()
KLIMOV_SELF_DISPLAY_ORDER = ["П", "Т", "З", "Х", "Ч"]


def _ddo_interpret_band(n: int, cap: int | None = None) -> str:
    """Ориентир по ключу; cap — максимум баллов по столбцу в текущей версии."""
    c = int(cap) if cap is not None and int(cap) > 0 else 10
    r = (n / c) if c else 0.0
    if r >= 0.9:
        return "ярко выраженная склонность"
    if r >= 0.7:
        return "выраженная склонность"
    if r >= 0.4:
        return "склонность на среднем уровне"
    if r >= 0.2:
        return "склонность не выражена"
    return "объект труда активно отвергается (ориентир по ключу)"


# --- ОПГ: 45 «вопросов» в боте (три подшага на высказывание); в JSON 45×3 карточки.
OPG_SPHERES_ORDER = ["Ч-З", "Ч-Т", "Ч-П", "Ч-Х", "Ч-Ч"]
OPG_ITEM_COUNT = 45
OPG_MAX_PER_DIM = OPG_ITEM_COUNT // len(OPG_SPHERES_ORDER) * 2  # 10 пунктов в столбце × 2 балла
OPG_META_KEY = "__opg_meta"
OPG_FLOW_KEY = "__opg_flow"
OPG_FLOW_VER = 3

QUESTIONS_OPG = _load_questions(OPG_PATH)


def _opg_flat_len() -> int:
    return len(QUESTIONS_OPG)


def _opg_effective_question_count(test_id: str) -> int:
    tid = normalize_test_id(test_id)
    if tid == TEST_OPG:
        return OPG_ITEM_COUNT
    return len(questions_for(tid))


def _opg_ensure_flow(scores: dict) -> dict:
    f = scores.get(OPG_FLOW_KEY)
    if not isinstance(f, dict):
        f = {}
        scores[OPG_FLOW_KEY] = f
    if int(f.get("ver", 0) or 0) != OPG_FLOW_VER:
        f["ver"] = OPG_FLOW_VER
    part = int(f.get("part", 0) or 0)
    if part not in (0, 1, 2):
        part = 0
        f["part"] = 0
    return f


def _opg_flat_index(step: int, scores: dict) -> int:
    f = _opg_ensure_flow(scores)
    part = int(f.get("part", 0) or 0)
    return int(step) * 3 + part


def _opg_scores_storable(scores: dict) -> dict:
    """Без служебного __opg_flow — не пишем в итог сессии / test_results."""
    return {k: v for k, v in scores.items() if k != OPG_FLOW_KEY}


def _opg_sphere_for_item(n: int) -> str:
    return OPG_SPHERES_ORDER[(int(n) - 1) % len(OPG_SPHERES_ORDER)]


def _opg_score_keys():
    keys = []
    for s in OPG_SPHERES_ORDER:
        for dim in ("skill", "att", "wish"):
            keys.append(f"{s}_{dim}")
    return keys


def _opg_sphere_subtotals(scores: dict) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for s in OPG_SPHERES_ORDER:
        out[s] = {
            "skill": int(scores.get(f"{s}_skill", 0) or 0),
            "att": int(scores.get(f"{s}_att", 0) or 0),
            "wish": int(scores.get(f"{s}_wish", 0) or 0),
        }
    return out


def _opg_finish_totals(scores: dict) -> tuple[list[tuple[str, int]], dict[str, dict[str, int]]]:
    """Топ сфер по желанию (как в методичке) и таблица У/О/Ж по сферам."""
    meta = scores.get(OPG_META_KEY)
    if not isinstance(meta, dict):
        meta = {}
    subs = _opg_sphere_subtotals(scores)
    for item_no_s, st in meta.items():
        try:
            item_no = int(item_no_s)
        except (TypeError, ValueError):
            continue
        if not isinstance(st, dict):
            continue
        sp = _opg_sphere_for_item(item_no)
        if int(st.get("skill", -1)) != 0:
            continue
        for dim in ("att", "wish"):
            subs[sp][dim] -= int(st.get(dim, 0) or 0)
            if subs[sp][dim] < 0:
                subs[sp][dim] = 0
    ranked = sorted(((s, subs[s]["wish"]) for s in OPG_SPHERES_ORDER), key=lambda x: x[1], reverse=True)
    return ranked, subs


CAREER_HINTS_OPG = {
    "Ч-З": CAREER_HINTS_DDO["Ч-З"],
    "Ч-Т": CAREER_HINTS_DDO["Ч-Т"],
    "Ч-П": CAREER_HINTS_DDO["Ч-П"],
    "Ч-Х": CAREER_HINTS_DDO["Ч-Х"],
    "Ч-Ч": CAREER_HINTS_DDO["Ч-Ч"],
}

# --- Таблица ОПТ: вопросы из JSON (модификация Резапкиной) ---
QUESTIONS_JOVASHI = _load_questions(JOVASHI_PATH)

# --- Йоваши: отдельная формулировка вопросов, та же логика подсчёта сфер ---
QUESTIONS_YOVASHI = _load_questions(YOVASHI_PATH)

QUESTIONS_KETTELL = _load_questions(KETTELL_PATH)
QUESTIONS_KETTELL_16PF_C_YOUTH = _load_questions(KETTELL_16PF_C_YOUTH_PATH)
# Порядок первичных факторов 16PF и ориентировочный максимум сырой суммы в этом боте
# (каждый пункт блока: ответ «а» +1, «б» +0.5 к фактору блока).
# Взрослая форма A: блоки по 12+12+…+11; молодёжная C: 15 блоков по 7 пунктов, фактор Q4 в форме не представлен.
PF16_ORDER = ["A", "B", "C", "E", "F", "G", "H", "I", "L", "M", "N", "O", "Q1", "Q2", "Q3", "Q4"]
PF16_BLOCK_MAX_ADULT: dict[str, float] = {
    "A": 12.0,
    "B": 12.0,
    "C": 12.0,
    "E": 12.0,
    "F": 12.0,
    "G": 12.0,
    "H": 12.0,
    "I": 12.0,
    "L": 12.0,
    "M": 12.0,
    "N": 12.0,
    "O": 11.0,
    "Q1": 11.0,
    "Q2": 11.0,
    "Q3": 11.0,
    "Q4": 11.0,
}
PF16_BLOCK_MAX_YOUTH: dict[str, float] = {c: 7.0 for c in PF16_ORDER if c != "Q4"}
PF16_BLOCK_MAX_YOUTH["Q4"] = 0.0

KETTELL_TRAITS = {
    "A": "Общительность, открытость (Warmth)",
    "B": "Умственная одарённость, логика (Reasoning)",
    "C": "Эмоциональная устойчивость (Stability)",
    "E": "Доминантность, напористость (Dominance)",
    "F": "Экспрессивность, бодрость (Liveliness)",
    "G": "Сознательность, долг (Rule-consciousness)",
    "H": "Общественная смелость (Social boldness)",
    "I": "Чувствительность (Sensitivity)",
    "L": "Настороженность (Vigilance)",
    "M": "Погружённость в образы (Abstractedness)",
    "N": "Сдержанность (Privateness)",
    "O": "Тревожность, самообвинение (Apprehension)",
    "Q1": "Открытость изменениям (Openness to change)",
    "Q2": "Самодостаточность (Self-reliance)",
    "Q3": "Самоконтроль, перфекционизм (Perfectionism)",
    "Q4": "Напряжённость (Tension)",
}

QUESTIONS_KOT = _load_questions(KOT_PATH)
CAREER_HINTS_KETTELL = {
    "B": "🧩 Блок включает задания на условные связи; для интерпретации IQ-шкалы нужен официальный ключ.",
    "C": "⚖️ Устойчивость помогает в ролях с ответственностью и стрессом; низкие значения — смотреть контекст и самочувствие.",
    "E": "📣 Напористость — лидерство и переговоры; при низкой — роли поддержки и экспертизы.",
    "F": "🎭 Бодрость — презентации, event, активные проекты.",
    "G": "📋 Сознательность — процессы, compliance, контроль качества.",
    "H": "🚀 Смелость в общении — новые контакты, продажи, публичность.",
    "I": "💭 Чувствительность — творчество, помощь профессиям, но важен баланс нагрузки.",
    "L": "🔍 Настороженность — аналитика, риски; высокая осторожность в доверии.",
    "M": "🌙 Образность — идеи, концепции; дополняй структурой и дедлайнами.",
    "N": "🤐 Сдержанность — самостоятельная работа; не путать с изоляцией.",
    "O": "😰 Тревожность — стоит обсуждать с психологом при сильной выраженности.",
    "Q1": "💡 Открытость новому — стартапы, R&D, смена форматов.",
    "Q2": "🛖 Самодостаточность — автономные роли; низкие — сильнее от командной среды.",
    "Q3": "✅ Перфекционизм — точные профессии; следи за балансом выгорания.",
    "Q4": "⚡ Напряжённость — мониторинг отдыха и стресса.",
}

QUESTIONS_EN60 = _load_questions(EN60_PATH)
QUESTIONS_EN57 = _load_questions(EN57_PATH)
QUESTIONS_HOLLAND_RIASEC = _load_questions(HOLLAND_RIASEC_PATH)

# Голланд RIASEC: шесть типов; максимум совпадений по ключу (в т.ч. двойной зачёт, напр. 1в → R+I)
HOLLAND_ORDER = ["R", "I", "S", "C", "E", "A"]
HOLLAND_MAX = {"R": 15, "I": 15, "S": 15, "C": 15, "E": 15, "A": 14}
HOLLAND_DIMENSIONS = {
    "R": "Реалистический",
    "I": "Интеллектуальный (исследовательский)",
    "S": "Социальный",
    "C": "Конвенциальный",
    "E": "Предприимчивый",
    "A": "Артистический",
}

CAREER_HINTS_HOLLAND = {
    "R": (
        "🛠️ Реалистический\n\n"
        "Предпочитает работать с вещами и техникой, а не с людьми. Ориентирован на конкретику, "
        "практические навыки и стабильность; ценит чёткие указания и традиционные ценности.\n\n"
        "Близкие типы: интеллектуальный и конвенциальный. Противоположный: социальный."
    ),
    "I": (
        "🔬 Интеллектуальный (исследовательский)\n\n"
        "Ориентирован на идеи и объекты, любознателен, методичен, часто комфортен в одиночной работе. "
        "Сильны аналитика, самостоятельность мышления, интерес к науке и исследованию.\n\n"
        "Близкие типы: реалистический и артистический. Противоположный: предприимчивый."
    ),
    "S": (
        "🤝 Социальный\n\n"
        "Нуждается в контактах, предпочитает людей вещам; ответственен, эмпатичен, сильна вербальная сфера. "
        "Тянет к обучению, лечению, консультированию, помощи.\n\n"
        "Близкие типы: артистический и предприимчивый. Противоположный: реалистический."
    ),
    "C": (
        "📋 Конвенциальный\n\n"
        "Предпочитает структурированную, регламентированную деятельность, работу с символами, "
        "цифрами, документами; дисциплина, аккуратность, практичность.\n\n"
        "Близкие типы: реалистический и предприимчивый. Противоположный: артистический."
    ),
    "E": (
        "📈 Предприимчивый\n\n"
        "Энергия, инициатива, влияние на людей, организация и риск; высокие притязания, важно материальное благополучие; "
        "меньше склонен к длительному рутинному усидчивому труду в одиночестве.\n\n"
        "Близкие типы: конвенциальный и социальный. Противоположный: исследовательский."
    ),
    "A": (
        "🎨 Артистический\n\n"
        "Творчество, воображение, самостоятельность в ценностях, чувствительность; плохо переносит жёсткий регламент; "
        "интерес к литературе, искусству, дизайну, сцене.\n\n"
        "Близкие типы: интеллектуальный и социальный. Противоположный: конвенциальный."
    ),
}
EN_LABELS = {"E": "Экстраверсия", "N": "Нейротизм / эмоциональная лабильность"}
EN_LIE_LABEL = "Шкала «ложь» / социальной желательности (L)"

JOVASHI_SPHERES = {
    "ЛЮДИ": "Сфера работы с людьми",
    "УМСТВЕННЫЙ": "Сфера умственного труда",
    "ИСКУССТВО": "Сфера эстетики и искусства",
    "ФИЗИЧЕСКИЙ": "Сфера физического труда и активности",
    "ЭКОНОМИКА": "Сфера планово-экономической деятельности",
    "ТЕХНИКА": "Сфера технических интересов",
}

CAREER_HINTS_JOVASHI = {
    "ЛЮДИ": "👥 Люди — обучение, сервис, помощь: педагог, психолог, врач, HR, экскурсовод.",
    "УМСТВЕННЫЙ": "🧠 Умственный труд — исследования, анализ: учёный, юрист, инженер-конструктор, лингвист.",
    "ИСКУССТВО": "🎨 Искусство — творчество: дизайнер, музыкант, актёр, писатель, режиссёр.",
    "ФИЗИЧЕСКИЙ": "🏃 Физическая активность — спорт, экспедиции, «полевые» профессии: тренер, спасатель, строитель.",
    "ЭКОНОМИКА": "📊 Планово-экономическая сфера — учёт, планирование: бухгалтер, экономист, менеджер по продажам/закупкам.",
    "ТЕХНИКА": "⚙️ Техника — оборудование, производство: электрик, программист, технолог, монтажник.",
}

CONSENT_VERSION = "udmurtia-career-v1"
CONSENT_TEXT = (
    "Согласие на обработку персональных данных\n\n"
    "Я, пользователь этого аккаунта мессенджера, на основании статьи 9 Федерального закона от 27 июля 2006 г. № 152-ФЗ «О персональных данных» "
    "в целях моей профессиональной ориентации даю согласие казённому учреждению Удмуртской Республики "
    "«Республиканский центр занятости населения» на обработку моих персональных данных: ФИО, населённого пункта "
    "(город, село, деревня), идентификатора аккаунта в мессенджере, ответов и результатов профориентационных опросников.\n\n"
    "Обработка может включать сбор, запись, систематизацию, накопление, хранение, уточнение, использование, "
    "обезличивание, блокирование, удаление и уничтожение данных — с использованием средств автоматизации и без них.\n\n"
    "Согласие действует 1 год со дня подтверждения. По окончании срока данные, ответы и результаты будут удалены. "
    "Отозвать согласие можно в любое время командой «Отозвать согласие».\n\n"
    "Чтобы продолжить, подтвердите согласие. После этого я попрошу указать населённый пункт.\n\n"
    "1 — Да, согласен(на)\n2 — Нет, не согласен(на)\n"
    "Нажмите кнопку или напишите 1 или 2.\n"
)

WELCOME_INTRO = "Привет! Я помогу пройти короткие опросники для профориентации и самопознания."

WELCOME_TEXT = (
    "Привет! Я помогу пройти короткие опросники для профориентации и самопознания.\n"
    "Сначала бот попросит ваши ФИО, затем — согласие на обработку данных и населённый пункт.\n\n"
    "Доступные тесты:\n"
    "• ДДО (30 вопросов): отметьте, согласны вы с утверждениями или нет. Узнайте, что вам ближе: природа, техника, знаки, искусство или работа с людьми.\n"
    "• ОПГ (45 вопросов): оцените умение, отношение и желание по шкале 0–2.\n"
    "• Йовайши (24 вопроса): узнайте свои профессиональные склонности.\n"
    "• Кеттелл 16PF (187 вопросов): опросник о личностных особенностях для взрослых.\n"
    "• Кеттелл 16PF/C (105 вопросов): версия для молодёжи.\n"
    "• КОТ: задания на логику, внимание и словарный запас.\n"
    "• ЭН-60 (60 вопросов): опросник для детей и подростков; оцениваются шкалы E, N и социальная желательность.\n"
    "• ЭН-57 (57 вопросов): личностный опросник для взрослых.\n\n"
    "Нажмите кнопку с названием теста или напишите его название в чат. Выбирайте ответы кнопками. Напишите «меню», чтобы вернуться к списку."
)


def normalize_test_id(test_id: str | None) -> str:
    if not test_id:
        return TEST_KLIMOV_SELF
    if test_id == LEGACY_HOLLAND:
        return TEST_HOLLAND_RIASEC
    return test_id


def _pf16_block_max_map(tid: str) -> dict[str, float]:
    t = normalize_test_id(tid)
    if t == TEST_KETTELL_16PF_C_YOUTH:
        return PF16_BLOCK_MAX_YOUTH
    return PF16_BLOCK_MAX_ADULT


def questions_for(test_id: str):
    tid = normalize_test_id(test_id)
    if tid == TEST_KLIMOV_SELF:
        return QUESTIONS_KLIMOV_SELF
    if tid == TEST_OPG:
        return QUESTIONS_OPG
    if tid == TEST_JOVASHI:
        return QUESTIONS_JOVASHI
    if tid == TEST_YOVASHI:
        return QUESTIONS_YOVASHI
    if tid == TEST_KETTELL:
        return QUESTIONS_KETTELL
    if tid == TEST_KETTELL_16PF_C_YOUTH:
        return QUESTIONS_KETTELL_16PF_C_YOUTH
    if tid == TEST_KOT:
        return QUESTIONS_KOT
    if tid == TEST_EN60:
        return QUESTIONS_EN60
    if tid == TEST_EN57:
        return QUESTIONS_EN57
    if tid == TEST_HOLLAND_RIASEC:
        return QUESTIONS_HOLLAND_RIASEC
    return QUESTIONS_KLIMOV_SELF


def empty_scores(test_id: str) -> dict:
    tid = normalize_test_id(test_id)
    if tid == TEST_KLIMOV_SELF:
        return {k: 0 for k in KLIMOV_SELF_TYPES}
    if tid == TEST_OPG:
        base = {k: 0 for k in _opg_score_keys()}
        base[OPG_META_KEY] = {}
        base[OPG_FLOW_KEY] = {"ver": OPG_FLOW_VER, "part": 0}
        return base
    if tid in (TEST_JOVASHI, TEST_YOVASHI):
        return {k: 0 for k in JOVASHI_SPHERES}
    if tid in (TEST_KETTELL, TEST_KETTELL_16PF_C_YOUTH):
        return {k: 0 for k in KETTELL_TRAITS}
    if tid == TEST_KOT:
        return {"LOGIC": 0}
    if tid == TEST_EN57:
        return {"E": 0, "N": 0, "L": 0}
    if tid == TEST_EN60:
        return {"E": 0, "N": 0, "L": 0}
    if tid == TEST_HOLLAND_RIASEC:
        return {k: 0 for k in HOLLAND_ORDER}
    return {}


def init_db():
    with db_connect() as conn:
        cur = conn.cursor()
        if USE_PG:
            pg_ddl_init(cur, _LP_DEDUP_TBL)
        else:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_progress (
                    user_id INTEGER PRIMARY KEY,
                    step INTEGER NOT NULL,
                    scores_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    last_activity_at INTEGER NOT NULL,
                    reminded_at INTEGER,
                    test_id TEXT NOT NULL DEFAULT 'klimov_self',
                    reminder_pending INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS test_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    finished_at INTEGER NOT NULL,
                    scores_json TEXT NOT NULL,
                    top3_json TEXT NOT NULL,
                    best_type TEXT NOT NULL,
                    test_id TEXT NOT NULL DEFAULT 'klimov_self'
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS test_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    test_id TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    completed_at INTEGER,
                    status TEXT NOT NULL DEFAULT 'in_progress',
                    final_scores_json TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS answer_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    test_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    answer_key TEXT NOT NULL,
                    question_text TEXT NOT NULL,
                    answer_label TEXT NOT NULL,
                    weights_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES test_sessions(id)
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_answer_log_session ON answer_log(session_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_answer_log_user ON answer_log(user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON test_sessions(user_id)")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_LP_DEDUP_TBL} (
                    dedup_key TEXT PRIMARY KEY,
                    seen_at INTEGER NOT NULL
                )
                """
            )
        ensure_column(conn, "user_progress", "test_id", "TEXT NOT NULL DEFAULT 'klimov_self'")
        ensure_column(conn, "user_progress", "reminder_pending", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "user_progress", "last_session_id", "INTEGER")
        ensure_column(conn, "test_results", "test_id", "TEXT NOT NULL DEFAULT 'klimov_self'")
        ensure_column(conn, "test_results", "best_type", "TEXT NOT NULL DEFAULT ''")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id BIGINT PRIMARY KEY,
                full_name TEXT,
                locality TEXT,
                consent_status TEXT NOT NULL,
                consent_at BIGINT,
                consent_expires_at BIGINT,
                onboarding_step TEXT NOT NULL,
                consent_version TEXT NOT NULL
            )
            """
        )
        cur.execute(
            "UPDATE user_progress SET test_id=? WHERE test_id=?",
            (TEST_HOLLAND_RIASEC, LEGACY_HOLLAND),
        )
        cur.execute(
            "UPDATE test_results SET test_id=? WHERE test_id=?",
            (TEST_HOLLAND_RIASEC, LEGACY_HOLLAND),
        )
        cur.execute(
            "UPDATE test_sessions SET test_id=? WHERE test_id=?",
            (TEST_HOLLAND_RIASEC, LEGACY_HOLLAND),
        )
        cur.execute(
            "UPDATE answer_log SET test_id=? WHERE test_id=?",
            (TEST_HOLLAND_RIASEC, LEGACY_HOLLAND),
        )
        _legacy_ddo_tid = "ddo"
        cur.execute(
            "UPDATE user_progress SET test_id=? WHERE test_id=?",
            (TEST_KLIMOV_SELF, _legacy_ddo_tid),
        )
        cur.execute(
            "UPDATE test_results SET test_id=? WHERE test_id=?",
            (TEST_KLIMOV_SELF, _legacy_ddo_tid),
        )
        cur.execute(
            "UPDATE test_sessions SET test_id=? WHERE test_id=?",
            (TEST_KLIMOV_SELF, _legacy_ddo_tid),
        )
        cur.execute(
            "UPDATE answer_log SET test_id=? WHERE test_id=?",
            (TEST_KLIMOV_SELF, _legacy_ddo_tid),
        )
        # Старый идентификатор текстового «логического» теста до замены на КОТ
        _legacy_logic_tid = "ra" + "ven"
        cur.execute(
            "UPDATE user_progress SET test_id=? WHERE test_id=?",
            (TEST_KOT, _legacy_logic_tid),
        )
        cur.execute(
            "UPDATE test_results SET test_id=? WHERE test_id=?",
            (TEST_KOT, _legacy_logic_tid),
        )
        cur.execute(
            "UPDATE test_sessions SET test_id=? WHERE test_id=?",
            (TEST_KOT, _legacy_logic_tid),
        )
        cur.execute(
            "UPDATE answer_log SET test_id=? WHERE test_id=?",
            (TEST_KOT, _legacy_logic_tid),
        )
        # Старые суммы ДДО (ключи Ч-П …) несовместимы с новой таблицей самооценки (П … Ч).
        cur.execute(
            "SELECT user_id, scores_json, status FROM user_progress WHERE test_id=?",
            (TEST_KLIMOV_SELF,),
        )
        _empty_klimov = json.dumps({k: 0 for k in KLIMOV_SELF_TYPES}, ensure_ascii=False)
        for _uid, _sj, _st in cur.fetchall():
            try:
                _d = json.loads(_sj)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(_d, dict):
                continue
            if not any(x in _d for x in PROFESSION_TYPES):
                continue
            if (_st or "") == "in_progress":
                cur.execute(
                    "UPDATE user_progress SET scores_json=?, step=0 WHERE user_id=?",
                    (_empty_klimov, int(_uid)),
                )
            else:
                cur.execute("DELETE FROM user_progress WHERE user_id=?", (int(_uid),))
        conn.commit()


def now_ts():
    return int(time.time())


def _one_year_after(timestamp: int) -> int:
    date = datetime.fromtimestamp(timestamp, timezone.utc)
    try:
        expiry = date.replace(year=date.year + 1)
    except ValueError:
        expiry = date.replace(year=date.year + 1, day=28)
    return int(expiry.timestamp())


def get_user_profile(user_id: int) -> dict | None:
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT full_name, locality, consent_status, consent_at, consent_expires_at, onboarding_step, consent_version "
            "FROM user_profiles WHERE user_id=?",
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "full_name": row[0],
        "locality": row[1],
        "consent_status": row[2],
        "consent_at": row[3],
        "consent_expires_at": row[4],
        "onboarding_step": row[5],
        "consent_version": row[6],
    }


def accept_user_consent(user_id: int) -> None:
    accepted_at = now_ts()
    expires_at = _one_year_after(accepted_at)
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO user_profiles(
                  user_id, full_name, locality, consent_status, consent_at, consent_expires_at,
                  onboarding_step, consent_version
              ) VALUES (?, NULL, NULL, 'accepted', ?, ?, 'locality', ?)
              ON CONFLICT(user_id) DO UPDATE SET
                  consent_status='accepted', consent_at=excluded.consent_at,
                  consent_expires_at=excluded.consent_expires_at, consent_version=excluded.consent_version,
                  onboarding_step=CASE WHEN user_profiles.full_name IS NOT NULL
                      AND user_profiles.full_name <> '' THEN 'locality' ELSE 'consent' END""",
            (user_id, accepted_at, expires_at, CONSENT_VERSION),
        )
        conn.commit()


def refuse_user_consent(user_id: int) -> None:
    """Отказ от обработки персональных данных: тесты остаются доступны,
    в выгрузках доносит consent_status='refused'."""
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO user_profiles(
                  user_id, full_name, locality, consent_status, consent_at, consent_expires_at,
                  onboarding_step, consent_version
              ) VALUES (?, NULL, NULL, 'refused', 0, 0, 'locality', ?)
              ON CONFLICT(user_id) DO UPDATE SET
                  consent_status='refused',
                  onboarding_step='locality',
                  consent_version=excluded.consent_version""",
            (user_id, CONSENT_VERSION),
        )
        conn.commit()


def save_user_locality(user_id: int, locality: str) -> None:
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_profiles SET locality=?, onboarding_step='complete' "
            "WHERE user_id=? AND ("
            "(consent_status='accepted' AND consent_expires_at>?) OR consent_status='refused')",
            (locality, user_id, now_ts()),
        )
        conn.commit()


def save_user_full_name(user_id: int, full_name: str) -> None:
    with db_connect() as conn:
        cur = conn.cursor()
        # ФИО — первый шаг: строка профиля создаётся, если её ещё нет,
        # иначе заполняется только имя (согласие/населённый пункт не сбрасываются).
        cur.execute(
            "INSERT INTO user_profiles("
            "user_id, full_name, locality, consent_status, consent_at, consent_expires_at, onboarding_step, consent_version"
            ") VALUES (?, ?, NULL, '', 0, 0, 'consent', ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "full_name=excluded.full_name, "
            "onboarding_step=CASE "
            "WHEN user_profiles.consent_status='accepted' THEN "
            "(CASE WHEN user_profiles.locality IS NOT NULL AND user_profiles.locality <> '' "
            "THEN 'complete' ELSE 'locality' END) ELSE 'consent' END",
            (user_id, full_name, CONSENT_VERSION),
        )
        conn.commit()


def delete_user_personal_data(user_id: int) -> None:
    with db_connect() as conn:
        cur = conn.cursor()
        for table in ("answer_log", "test_results", "user_progress", "test_sessions"):
            cur.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
        if user_id < 0 and "max_user_id" in table_column_names(conn, "max_user_map"):
            cur.execute("DELETE FROM max_user_map WHERE max_user_id=?", (-user_id,))
        cur.execute("DELETE FROM user_profiles WHERE user_id=?", (user_id,))
        conn.commit()


def purge_expired_personal_data() -> int:
    timestamp = now_ts()
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id FROM user_profiles WHERE consent_status='accepted' "
            "AND (consent_expires_at<=? OR consent_version<>?)",
            (timestamp, CONSENT_VERSION),
        )
        expired_ids = [int(row[0]) for row in cur.fetchall()]
    for user_id in expired_ids:
        delete_user_personal_data(user_id)
    return len(expired_ids)


def user_profile_is_complete(user_id: int) -> bool:
    profile = get_user_profile(user_id)
    if profile and profile.get("consent_status") == "accepted" and (
        int(profile.get("consent_expires_at") or 0) <= now_ts()
        or profile.get("consent_version") != CONSENT_VERSION
    ):
        delete_user_personal_data(user_id)
        return False
    return bool(
        profile
        and profile.get("consent_status") in ("accepted", "refused")
        and profile.get("onboarding_step") == "complete"
        and profile.get("full_name")
        and profile.get("locality")
    )


def save_progress(
    user_id: int,
    test_id: str,
    step: int,
    scores: dict,
    status: str = "in_progress",
    reminder_pending: int = 0,
    last_session_id: int | None = None,
):
    ts = now_ts()
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_progress (user_id, step, scores_json, status, started_at, last_activity_at, reminded_at, test_id, reminder_pending, last_session_id)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
            step=excluded.step,
            scores_json=excluded.scores_json,
            status=excluded.status,
            last_activity_at=excluded.last_activity_at,
            test_id=excluded.test_id,
            reminder_pending=excluded.reminder_pending,
            last_session_id=COALESCE(excluded.last_session_id, user_progress.last_session_id)
            """,
            (
                user_id,
                step,
                json.dumps(scores, ensure_ascii=False),
                status,
                ts,
                ts,
                test_id,
                reminder_pending,
                last_session_id,
            ),
        )
        conn.commit()


def touch_progress(user_id: int):
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_progress SET last_activity_at=?, reminder_pending=0 WHERE user_id=?",
            (now_ts(), user_id),
        )
        conn.commit()


def set_reminded(user_id: int):
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_progress SET reminded_at=? WHERE user_id=?",
            (now_ts(), user_id),
        )
        conn.commit()


def set_reminder_pending(user_id: int, pending: int):
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_progress SET reminder_pending=? WHERE user_id=?",
            (1 if pending else 0, user_id),
        )
        conn.commit()


def get_progress(user_id: int):
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT step, scores_json, status, test_id, reminder_pending, last_session_id
            FROM user_progress
            WHERE user_id=?
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        step, scores_json, status, test_id, reminder_pending, last_session_id = row
        scores = json.loads(scores_json)
        tid = normalize_test_id(test_id or TEST_KLIMOV_SELF)
        rp = int(reminder_pending or 0)
        lsid = int(last_session_id) if last_session_id is not None else None
        if tid == TEST_KLIMOV_SELF and scores:
            need = set(KLIMOV_SELF_TYPES.keys())
            sk = set(scores.keys())
            nq = len(QUESTIONS_KLIMOV_SELF)
            dirty = False
            if sk != need:
                merged = empty_scores(TEST_KLIMOV_SELF)
                for k in need:
                    merged[k] = int(scores.get(k, 0) or 0)
                scores = merged
                dirty = True
            if step > nq or step < 0:
                step = 0
                dirty = True
            if dirty:
                save_progress(
                    user_id=user_id,
                    test_id=TEST_KLIMOV_SELF,
                    step=step,
                    scores=scores,
                    status=status,
                    reminder_pending=rp,
                    last_session_id=lsid,
                )
        if tid == TEST_OPG and scores:
            base_need = set(_opg_score_keys()) | {OPG_META_KEY}
            meta = scores.get(OPG_META_KEY)
            legacy = any(x in scores for x in ("ПОЗ", "ЭМО", "ДЕЯ", "КОМ"))
            if legacy or not base_need <= set(scores.keys()) or not isinstance(meta, dict):
                scores = empty_scores(TEST_OPG)
                step = 0
                save_progress(
                    user_id=user_id,
                    test_id=TEST_OPG,
                    step=step,
                    scores=scores,
                    status=status,
                    reminder_pending=rp,
                    last_session_id=lsid,
                )
            else:
                flow = _opg_ensure_flow(scores)
                fv = int(flow.get("ver", 0) or 0)
                istep = int(step)
                flat_n = _opg_flat_len()
                expected_flat = OPG_ITEM_COUNT * 3
                if flat_n != expected_flat:
                    raise RuntimeError(
                        f"opg_questions.json: ожидалось {expected_flat} карточек, загружено {flat_n}"
                    )
                if fv < OPG_FLOW_VER:
                    # Новая редакция высказываний / длина — начинаем заново (старые суммы не сопоставимы).
                    scores = empty_scores(TEST_OPG)
                    step = 0
                    flow = _opg_ensure_flow(scores)
                    flow["ver"] = OPG_FLOW_VER
                    flow["part"] = 0
                    save_progress(
                        user_id=user_id,
                        test_id=TEST_OPG,
                        step=step,
                        scores=scores,
                        status="in_progress",
                        reminder_pending=rp,
                        last_session_id=lsid,
                    )
                elif status == "completed" and istep == OPG_ITEM_COUNT:
                    flow["ver"] = OPG_FLOW_VER
                    flow["part"] = 0
                else:
                    if istep > OPG_ITEM_COUNT or (istep == OPG_ITEM_COUNT and status != "completed"):
                        scores = empty_scores(TEST_OPG)
                        step = 0
                        save_progress(
                            user_id=user_id,
                            test_id=TEST_OPG,
                            step=step,
                            scores=scores,
                            status=status,
                            reminder_pending=rp,
                            last_session_id=lsid,
                        )
        if tid == TEST_HOLLAND_RIASEC and scores and not set(scores.keys()) <= set(HOLLAND_ORDER):
            scores = empty_scores(TEST_HOLLAND_RIASEC)
            step = 0
            save_progress(
                user_id=user_id,
                test_id=TEST_HOLLAND_RIASEC,
                step=step,
                scores=scores,
                status=status,
                reminder_pending=rp,
                last_session_id=lsid,
            )
        if tid == TEST_KOT and step >= len(QUESTIONS_KOT):
            scores = empty_scores(TEST_KOT)
            step = 0
            save_progress(
                user_id=user_id,
                test_id=TEST_KOT,
                step=step,
                scores=scores,
                status=status,
                reminder_pending=rp,
                last_session_id=lsid,
            )
        return {
            "step": step,
            "scores": scores,
            "status": status,
            "test_id": tid,
            "reminder_pending": rp,
            "last_session_id": lsid,
        }


def complete_progress(user_id: int):
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE user_progress
            SET status='completed', last_activity_at=?
            WHERE user_id=?
            """,
            (now_ts(), user_id),
        )
        conn.commit()


def abandon_progress(user_id: int, session_id: int | None = None):
    """Сбрасывает незавершённый тест (например, пользователь отказался продолжать после напоминания)."""
    ts = now_ts()
    with db_connect() as conn:
        cur = conn.cursor()
        if session_id:
            cur.execute(
                """
                UPDATE test_sessions
                SET status='abandoned', completed_at=?
                WHERE id=? AND user_id=? AND status='in_progress'
                """,
                (ts, session_id, user_id),
            )
        cur.execute("DELETE FROM user_progress WHERE user_id=?", (user_id,))
        conn.commit()


def create_test_session(user_id: int, test_id: str) -> int:
    ts = now_ts()
    with db_connect() as conn:
        cur = conn.cursor()
        sid = insert_returning_id(
            cur,
            """
            INSERT INTO test_sessions (user_id, test_id, started_at, status)
            VALUES (?, ?, ?, 'in_progress')
            """,
            (user_id, test_id, ts),
        )
        conn.commit()
        return int(sid)


def log_answer_row(
    session_id: int,
    user_id: int,
    test_id: str,
    step_index: int,
    answer_key: str,
    question_text: str,
    answer_label: str,
    weights: dict,
):
    ts = now_ts()
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO answer_log (
                session_id, user_id, test_id, step_index, answer_key,
                question_text, answer_label, weights_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                user_id,
                test_id,
                step_index,
                answer_key,
                question_text,
                answer_label,
                json.dumps(weights, ensure_ascii=False),
                ts,
            ),
        )
        conn.commit()


def complete_test_session(session_id: int, user_id: int, scores: dict, status: str = "completed"):
    ts = now_ts()
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE test_sessions
            SET completed_at=?, status=?, final_scores_json=?
            WHERE id=? AND user_id=?
            """,
            (ts, status, json.dumps(scores, ensure_ascii=False), session_id, user_id),
        )
        conn.commit()


_STATS_ADMIN_CACHE: dict[int, tuple[float, bool]] = {}
_STATS_ADMIN_TTL_SEC = 120.0


def is_stats_admin(vk, user_id: int) -> bool:
    if getattr(vk, "platform", "vk") == "max":
        return -int(user_id) in MAX_STATS_ADMIN_IDS
    if user_id in STATS_ADMIN_IDS:
        return True
    if user_id <= 0 or GROUP_ID_ABS <= 0:
        return False
    peer = _REPLY_PEER_ID.get()
    if peer is not None and peer >= CHAT_START_ID:
        try:
            data = vk.messages.getConversationMembers(peer_id=peer)
            for item in data.get("items", []):
                mid = item.get("member_id")
                if mid == user_id:
                    if item.get("is_owner") or item.get("is_admin"):
                        return True
                    return False
        except Exception as e:
            print(f"[is_stats_admin chat peer={peer}] {e}")
        return False
    now = time.time()
    cached = _STATS_ADMIN_CACHE.get(user_id)
    if cached and now - cached[0] < _STATS_ADMIN_TTL_SEC:
        return cached[1]
    ok = False
    try:
        data = vk.groups.getMembers(group_id=GROUP_ID_ABS, filter="managers", count=200)
        ok = user_id in set(data.get("items", []))
    except Exception as e:
        print(f"[is_stats_admin] {e}")
        ok = False
    _STATS_ADMIN_CACHE[user_id] = (now, ok)
    return ok


MSK_TZ = timezone(timedelta(hours=3))


def _msk_day_start_end_ts() -> tuple[int, int]:
    """Границы текущих календарных суток в часовом поясе Москвы (unix)."""
    now = datetime.now(MSK_TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def _parse_stats_export_period(text: str) -> tuple[str, int | None, int | None]:
    """
    Период выгрузки из текста сообщения.
    Возвращает (метка, since_ts, until_ts); until — невключительно (SQL: created_at/finished_at >= since AND < until).
    None, None — без фильтра (всё время).
    Окна «неделя / месяц / квартал» — скользящие интервалы от текущего момента по московскому времени.
    """
    lines = [
        _strip_command_text(line)
        for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if _strip_command_text(line)
    ]
    blob = " ".join(lines).lower()
    if not blob:
        return "all", None, None

    def _rolling_since_until_sec(days: int) -> tuple[int, int]:
        now = datetime.now(MSK_TZ)
        until_excl = int(now.timestamp()) + 1
        since = until_excl - int(days) * 86400
        return since, until_excl

    day_tokens = (
        "сегодня",
        "за сегодня",
        "за день",
        "за текущие сутки",
        "только за сегодня",
        "today",
        "for today",
    )
    if any(tok in blob for tok in day_tokens):
        a, b = _msk_day_start_end_ts()
        return "today_msk", a, b

    all_time_markers = (
        "отчет все время",
        "отчёт все время",
        "отчет за все время",
        "отчёт за всё время",
        "отчет за всё время",
        "отчёт за все время",
    )
    if any(p in blob for p in all_time_markers):
        return "all", None, None

    quarter_markers = (
        "отчет квартал",
        "отчёт квартал",
        "отчет за квартал",
        "отчёт за квартал",
        "за квартал",
        "за 3 месяца",
        "отчет за 3 месяца",
        "отчёт за 3 месяца",
    )
    if any(p in blob for p in quarter_markers):
        s, u = _rolling_since_until_sec(90)
        return "quarter_90d", s, u

    month_markers = (
        "отчет месяц",
        "отчёт месяц",
        "отчет за месяц",
        "отчёт за месяц",
    )
    if any(p in blob for p in month_markers) or blob.strip() == "за месяц":
        s, u = _rolling_since_until_sec(30)
        return "month_30d", s, u

    week_markers = (
        "отчет неделя",
        "отчёт неделя",
        "отчет за неделю",
        "отчёт за неделю",
    )
    if any(p in blob for p in week_markers) or blob.strip() == "за неделю":
        s, u = _rolling_since_until_sec(7)
        return "week_7d", s, u

    return "all", None, None


def test_results_row_count_filtered(since: int | None, until: int | None) -> int:
    with db_connect() as conn:
        cur = conn.cursor()
        if since is None or until is None:
            cur.execute("SELECT COUNT(*) FROM test_results")
        else:
            cur.execute(
                "SELECT COUNT(*) FROM test_results WHERE finished_at >= ? AND finished_at < ?",
                (since, until),
            )
        return int(cur.fetchone()[0])


def incomplete_sessions_row_count_filtered(since: int | None, until: int | None) -> int:
    with db_connect() as conn:
        cur = conn.cursor()
        if since is None or until is None:
            cur.execute("SELECT COUNT(*) FROM test_sessions WHERE status != 'completed'")
        else:
            cur.execute(
                """
                SELECT COUNT(*) FROM test_sessions
                WHERE status != 'completed' AND started_at >= ? AND started_at < ?
                """,
                (since, until),
            )
        return int(cur.fetchone()[0])


def answer_log_row_count_filtered(since: int | None, until: int | None) -> int:
    with db_connect() as conn:
        cur = conn.cursor()
        if since is None or until is None:
            cur.execute("SELECT COUNT(*) FROM answer_log")
        else:
            cur.execute(
                "SELECT COUNT(*) FROM answer_log WHERE created_at >= ? AND created_at < ?",
                (since, until),
            )
        return int(cur.fetchone()[0])


def _top3_from_scores(scores: dict) -> list:
    if not scores:
        return []
    pairs: list[tuple] = []
    for k, v in scores.items():
        if k == OPG_META_KEY or k == OPG_FLOW_KEY:
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            pairs.append((k, int(v)))
    return sorted(pairs, key=lambda x: x[1], reverse=True)[:3]


def _vk_user_link(user_id: int) -> str:
    """Внешняя ссылка на профиль: VK или MAX (шаблоны можно переопределить env)."""
    uid = int(user_id)
    if uid < 0:
        template = (os.getenv("MAX_PROFILE_URL_TEMPLATE") or "https://max.ru/user/{id}").strip()
        return template.format(id=abs(uid))
    template = (os.getenv("VK_PROFILE_URL_TEMPLATE") or "https://vk.com/id{id}").strip()
    return template.format(id=uid)


def _user_short_link(user_id: int) -> str:
    """Короткий текст ссылки без схемы (для ячейки Excel)."""
    return _vk_user_link(user_id).split("://", 1)[-1]


def _loads_json_blob(raw, default=None):
    if default is None:
        default = {}
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    s = str(raw).strip()
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return default


def _upload_stats_document(vk, bio: io.BytesIO, fname: str, peer_id: int):
    """Загружает статистику платформенным способом и возвращает вложение."""
    if getattr(vk, "platform", "vk") == "max":
        return vk.upload_document(bio, fname, peer_id)
    bio.seek(0)
    upload = VkUpload(vk)
    try:
        saved = upload.document_message(bio, title=fname, peer_id=peer_id)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "VK upload: пустой или неверный ответ (обычно нет права «Документы» у ключа сообщества "
            "или сбой docs.getMessagesUploadServer). Перевыпустите ключ с правами: сообщения + документы."
        ) from e
    except Exception as e:
        err = str(e)
        if "Expecting value" in err or "JSONDecodeError" in err:
            raise RuntimeError(
                "VK upload: не удалось разобрать ответ сервера (проверьте права ключа: сообщения + документы)."
            ) from e
        raise
    if isinstance(saved, list) and saved:
        saved = saved[0]
    att = saved.get("doc") if isinstance(saved, dict) and "doc" in saved else saved
    if not isinstance(att, dict) or att.get("owner_id") is None or att.get("id") is None:
        raise RuntimeError(f"VK upload: неожиданный ответ {saved!r}")
    return f"doc{att['owner_id']}_{att['id']}"


def _fetch_vk_user_names(vk, user_ids: list[int]) -> dict[int, str]:
    """Имена из VK/MAX: MAX-пользователи в общей БД имеют отрицательный core ID."""
    out: dict[int, str] = {}
    if not user_ids:
        return out
    ids = sorted({int(u) for u in user_ids if u})
    max_ids = sorted({-uid for uid in ids if uid < 0})
    for offset in range(0, len(max_ids), 900):
        part = max_ids[offset : offset + 900]
        if not part:
            continue
        placeholders = ",".join("?" for _ in part)
        try:
            with db_connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"SELECT max_user_id, first_name, last_name FROM max_user_map "
                    f"WHERE max_user_id IN ({placeholders})",
                    tuple(part),
                )
                for max_uid, first_name, last_name in cur.fetchall():
                    out[-int(max_uid)] = f"{first_name or ''} {last_name or ''}".strip()
        except Exception as e:
            print(f"[_fetch_max_user_names] {e}")

    vk_ids = [uid for uid in ids if uid > 0]
    if not vk or not vk_ids:
        return out
    chunk = 900
    for i in range(0, len(vk_ids), chunk):
        part = vk_ids[i : i + chunk]
        try:
            rows = vk.users.get(user_ids=part)
        except Exception as e:
            print(f"[_fetch_vk_user_names] {e}")
            continue
        for u in rows or []:
            uid = u.get("id")
            if uid is None:
                continue
            fn = (u.get("first_name") or "").strip()
            ln = (u.get("last_name") or "").strip()
            out[int(uid)] = f"{fn} {ln}".strip()
    return out


def _fetch_user_profiles(user_ids: list[int]) -> dict[int, dict[str, object]]:
    """Профили онбординга: принявшие согласие (действующее) и отказавшиеся —
    оба попадают в выгрузки, колонка согласия покажет Да/Нет."""
    profiles: dict[int, dict[str, object]] = {}
    ids = sorted({int(user_id) for user_id in user_ids if user_id})
    for offset in range(0, len(ids), 900):
        part = ids[offset : offset + 900]
        if not part:
            continue
        placeholders = ",".join("?" for _ in part)
        with db_connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT user_id, full_name, locality, consent_at, consent_expires_at, consent_status FROM user_profiles "
                f"WHERE user_id IN ({placeholders}) AND ("
                "(consent_status='accepted' AND consent_expires_at>? AND consent_version=?) "
                "OR consent_status='refused')",
                (*part, now_ts(), CONSENT_VERSION),
            )
            for user_id, full_name, locality, consent_at, consent_expires_at, consent_status in cur.fetchall():
                profiles[int(user_id)] = {
                    "full_name": full_name or "",
                    "locality": locality or "",
                    "consent_at": consent_at,
                    "consent_expires_at": consent_expires_at,
                    "consent_status": consent_status or "",
                }
    return profiles


def _format_profile_consent(profile: dict[str, object] | None) -> str:
    if not profile:
        return "Нет подтверждения"
    if str(profile.get("consent_status") or "") == "refused":
        return "Нет (отказ от обработки)"
    if not profile.get("consent_at"):
        return "Нет подтверждения"
    try:
        accepted_at = datetime.fromtimestamp(int(profile["consent_at"]), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        expires_at = datetime.fromtimestamp(int(profile["consent_expires_at"]), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OSError):
        return "Подтверждено; дата не определена"
    return f"Да — {accepted_at}; действует до {expires_at}; версия {CONSENT_VERSION}"


def _export_result_summary(tid: str, scores: dict, top3: list) -> str:
    """Краткий текст итогов для Excel (без эмодзи)."""
    if not scores:
        return ""
    lines: list[str] = []
    if tid == TEST_KOT:
        total = len(QUESTIONS_KOT)
        correct = scores.get("LOGIC", 0)
        pct = round(100 * correct / total) if total else 0
        return f"Верных ответов: {correct} из {total} ({pct}%)."
    if tid == TEST_EN57:
        e = scores.get("E", 0)
        n = scores.get("N", 0)
        l = scores.get("L", 0)
        return (
            f"{EN_LABELS['E']}: {e} из 24.\n"
            f"{EN_LABELS['N']}: {n} из 24.\n"
            f"{EN_LIE_LABEL}: {l} из 9 (высокий балл — возможная пристрастность к «социально желательным» ответам)."
        )
    if tid == TEST_EN60:
        e = scores.get("E", 0)
        n = scores.get("N", 0)
        l = scores.get("L", 0)
        return (
            f"{EN_LABELS['E']}: {e} из 24.\n"
            f"{EN_LABELS['N']}: {n} из 24.\n"
            f"{EN_LIE_LABEL}: {l} из 12."
        )
    if tid == TEST_HOLLAND_RIASEC:
        lines = ["Шесть типов Голланда (суммы по ключу, с двойным зачётом при совпадениях в таблице):"]
        for code in HOLLAND_ORDER:
            mx = HOLLAND_MAX[code]
            v = int(scores.get(code, 0))
            lines.append(f"{HOLLAND_DIMENSIONS[code]}: {v} из {mx}.")
        return "\n".join(lines)
    if tid == TEST_KLIMOV_SELF:
        if any(k in scores for k in PROFESSION_TYPES):
            return (
                "Архив: сохранён результат старого варианта (пары занятий, ключи Ч-П …). "
                "Текущая методика — 30 утверждений самооценки (П … Ч). Пройдите тест заново для нового ключа.\n"
                + json.dumps(scores, ensure_ascii=False)
            )
        d_top = top3
        if not d_top:
            d_top = sorted(
                ((k, int(scores.get(k, 0) or 0)) for k in KLIMOV_SELF_DISPLAY_ORDER),
                key=lambda x: x[1],
                reverse=True,
            )[:3]
        out_lines: list[str] = [
            "ДДО (самооценка по Климову): топ-3 столбца по сумме баллов (максимум по столбцу см. ниже):"
        ]
        for i, (ptype, points) in enumerate(d_top, 1):
            mx = KLIMOV_SELF_MAX_BY_CATEGORY.get(ptype, 10)
            band = _ddo_interpret_band(int(points), mx)
            out_lines.append(
                f"{i}. {KLIMOV_SELF_TYPES.get(ptype, ptype)} — {points} из {mx} ({band})"
            )
        out_lines.append("")
        out_lines.append("Все столбцы:")
        for pk in KLIMOV_SELF_DISPLAY_ORDER:
            n = int(scores.get(pk, 0) or 0)
            mx = KLIMOV_SELF_MAX_BY_CATEGORY.get(pk, 10)
            out_lines.append(
                f"• {KLIMOV_SELF_TYPES[pk]}: {n} из {mx} — {_ddo_interpret_band(n, mx)}"
            )
        return "\n".join(out_lines)
    if tid == TEST_OPG:
        ranked_wish, subs = _opg_finish_totals(scores)
        lines = [
            "ОПГ: топ сфер по «желанию» (с коррекцией при умении = 0):",
        ]
        for i, (sp, w) in enumerate(ranked_wish[:3], 1):
            lines.append(f"{i}. {PROFESSION_TYPES.get(sp, sp)} — {w} (ориентир. макс. по шкале {OPG_MAX_PER_DIM})")
        lines.append("")
        for sp in OPG_SPHERES_ORDER:
            d = subs[sp]
            lines.append(
                f"{PROFESSION_TYPES.get(sp, sp)}: умение {d['skill']}, отношение {d['att']}, желание {d['wish']} "
                f"(макс. по каждой шкале в столбце ≈ {OPG_MAX_PER_DIM})."
            )
        return "\n".join(lines)
    if not top3:
        return json.dumps(scores, ensure_ascii=False)
    if tid in (TEST_JOVASHI, TEST_YOVASHI):
        lines.append("Топ-3 сферы:")
        for i, (key, points) in enumerate(top3, 1):
            interp = _interpret_jovashi(points)
            lines.append(f"{i}. {JOVASHI_SPHERES.get(key, key)} — {points} б. ({interp})")
    elif tid in (TEST_KETTELL, TEST_KETTELL_16PF_C_YOUTH):
        mxmap = _pf16_block_max_map(tid)
        label = LABEL_KETTELL_16PF_C_YOUTH if tid == TEST_KETTELL_16PF_C_YOUTH else LABEL_KETTELL
        ranked = sorted(
            ((k, scores.get(k, 0)) for k in PF16_ORDER if mxmap.get(k, 0) > 0),
            key=lambda x: x[1],
            reverse=True,
        )
        lines = [
            f"Топ-3 по ориентировочной сумме ({label}):",
        ]
        for i, (key, points) in enumerate(ranked[:3], 1):
            mx = mxmap.get(key, 0)
            short = KETTELL_TRAITS.get(key, key).split("(")[0].strip()
            lines.append(f"{i}. {key} — {short}: {points:g} из {mx:g}")
        lines.append("")
        for code in PF16_ORDER:
            mx = mxmap.get(code, 0)
            if mx <= 0:
                continue
            v = scores.get(code, 0)
            lines.append(f"{code}: {v:g} из {mx:g}")
        lines.append("")
        if tid == TEST_KETTELL_16PF_C_YOUTH:
            lines.append(
                "Суммы ориентировочные: 15 блоков по 7 пунктов (16PF/C для молодёжи в боте), ответ «а» +1, «б» +0.5. "
                "Фактор Q4 в этой форме не представлен. Не заменяет официальный ключ и нормы Publisher."
            )
        else:
            lines.append(
                "Суммы ориентировочные: блоки как у формы A в боте, ответ «а» +1, «б» +0.5 к фактору блока. "
                "Это не заменяет официальный ключ, стены и нормы издателя 16PF."
            )
    else:
        lines.append(json.dumps(scores, ensure_ascii=False))
    return " ".join(lines) if len(lines) == 1 else "\n".join(lines)


def _test_title_for_export(test_id: str) -> str:
    return {
        TEST_KLIMOV_SELF: LABEL_KLIMOV_SELF,
        TEST_OPG: LABEL_OPG,
        TEST_JOVASHI: LABEL_PROF_TABLE,
        TEST_YOVASHI: LABEL_YOVASHI,
        TEST_KETTELL: LABEL_KETTELL,
        TEST_KETTELL_16PF_C_YOUTH: LABEL_KETTELL_16PF_C_YOUTH,
        TEST_KOT: LABEL_KOT,
        TEST_EN60: LABEL_EN60,
        TEST_EN57: LABEL_EN57,
        TEST_HOLLAND_RIASEC: LABEL_HOLLAND,
    }.get(test_id, test_id)


# ---------------------------------------------------------------------------
# Форматирование Excel-выгрузки: единый читабельный вид (шапка, перенос,
# объединение, ширина, заморозка), чтобы листать было удобно.
# ---------------------------------------------------------------------------
_HEADER_FILL = openpyxl.styles.PatternFill("solid", fgColor="305496")
_HEADER_FONT = openpyxl.styles.Font(bold=True, color="FFFFFF")
_THIN_SIDE = openpyxl.styles.Side(style="thin", color="B0B0B0")
_CELL_BORDER = openpyxl.styles.Border(left=_THIN_SIDE, right=_THIN_SIDE, top=_THIN_SIDE, bottom=_THIN_SIDE)
_LINK_FONT = openpyxl.styles.Font(color="0563C1", underline="single")


def _estimate_lines(text: str, width_chars: int) -> int:
    """Сколько строк займёт текст при переносе по ширине столбца."""
    total = 0
    for part in str(text or "").splitlines():
        total += max(1, (len(part) + max(1, width_chars) - 1) // max(1, width_chars))
    return max(1, total)


def _style_sheet_grid(ws, *, widths: dict[int, int], wrap_cols: list[int], freeze: str = "A2") -> None:
    """Шапка (фон+жирный+центр), границы, ширина колонок, перенос, заморозка."""
    from openpyxl.cell.cell import MergedCell
    from openpyxl.utils import get_column_letter

    for col_idx, w in widths.items():
        ws.column_dimensions[get_column_letter(col_idx)].width = w
    max_col = max(widths)
    for row_cells in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=max_col):
        for cell in row_cells:
            if isinstance(cell, MergedCell):
                continue
            cell.border = _CELL_BORDER
            if cell.row == 1:
                cell.fill = _HEADER_FILL
                cell.font = _HEADER_FONT
                cell.alignment = openpyxl.styles.Alignment(horizontal="center", vertical="center", wrap_text=True)
            elif cell.column in wrap_cols:
                cell.alignment = openpyxl.styles.Alignment(vertical="top", wrap_text=True)
            else:
                cell.alignment = openpyxl.styles.Alignment(vertical="top")
    ws.freeze_panes = freeze
    try:
        ws.auto_filter.ref = ws.dimensions
    except Exception:
        pass


def _apply_user_link(cell, user_id: int) -> None:
    """Ячейка-гиперссылка на профиль платформы."""
    cell.value = _user_short_link(user_id)
    cell.hyperlink = _vk_user_link(user_id)
    cell.font = _LINK_FONT


def _merge_user_columns(ws, *, uids: list[int], max_col: int) -> None:
    """Вертикально объединить колонки 1..max_col для подряд идущих строк одного пользователя."""
    if not uids:
        return
    group_start = 2
    for idx in range(len(uids) + 1):
        cur = uids[idx] if idx < len(uids) else None
        prev = uids[idx - 1] if idx > 0 else None
        if idx > 0 and cur != prev:
            end_row = min(group_start + idx - 1, ws.max_row)
            if end_row > group_start:
                for col in range(1, max_col + 1):
                    ws.merge_cells(start_row=group_start, start_column=col, end_row=end_row, end_column=col)
            group_start = group_start + idx


def _format_answer_result(raw) -> str:
    """Понятный текст «что дал ответ» (веса по факторам)."""
    data = _loads_json_blob(raw, {})
    if not isinstance(data, dict) or not data:
        return ""
    return "\n".join(f"{k}: {v:g}" for k, v in data.items())


def build_stats_excel_bytes(vk, since: int | None = None, until: int | None = None) -> bytes:
    headers = [
        "№ (новый пользователь — новый номер)",
        "Ссылка на пользователя",
        "Имя аккаунта / ФИО",
        "Населённый пункт",
        "Согласие на обработку ПДн",
        "Название теста",
        "Завершил",
        "Дата и время",
        "Итоги теста",
    ]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "сводка"
    ws.append(headers)
    with db_connect() as conn:
        cur = conn.cursor()
        if since is None or until is None:
            cur.execute(
                """
                SELECT id, user_id, test_id, finished_at, scores_json, top3_json
                FROM test_results
                ORDER BY finished_at ASC, id ASC
                """
            )
        else:
            cur.execute(
                """
                SELECT id, user_id, test_id, finished_at, scores_json, top3_json
                FROM test_results
                WHERE finished_at >= ? AND finished_at < ?
                ORDER BY finished_at ASC, id ASC
                """,
                (since, until),
            )
        result_rows = cur.fetchall()
        if since is None or until is None:
            cur.execute(
                """
                SELECT id, user_id, test_id, started_at, status, final_scores_json
                FROM test_sessions
                WHERE status != 'completed'
                ORDER BY started_at ASC, id ASC
                """
            )
        else:
            cur.execute(
                """
                SELECT id, user_id, test_id, started_at, status, final_scores_json
                FROM test_sessions
                WHERE status != 'completed' AND started_at >= ? AND started_at < ?
                ORDER BY started_at ASC, id ASC
                """,
                (since, until),
            )
        session_rows = cur.fetchall()
        session_ids = [int(r[0]) for r in session_rows]
        answer_counts: dict[int, int] = {}
        if session_ids:
            placeholders = ",".join("?" * len(session_ids))
            cur.execute(
                f"""
                SELECT session_id, COUNT(*) AS c
                FROM answer_log
                WHERE session_id IN ({placeholders})
                GROUP BY session_id
                """,
                session_ids,
            )
            answer_counts = {int(sid): int(c) for sid, c in cur.fetchall()}

        if since is None or until is None:
            cur.execute(
                """
                SELECT id, session_id, user_id, test_id, step_index, answer_key, question_text, answer_label,
                       weights_json, created_at
                FROM answer_log
                ORDER BY created_at ASC, id ASC
                """
            )
        else:
            cur.execute(
                """
                SELECT id, session_id, user_id, test_id, step_index, answer_key, question_text, answer_label,
                       weights_json, created_at
                FROM answer_log
                WHERE created_at >= ? AND created_at < ?
                ORDER BY created_at ASC, id ASC
                """,
                (since, until),
            )
        answer_rows = cur.fetchall()

    all_uids: list[int] = []
    for row in result_rows:
        all_uids.append(int(row[1]))
    for row in session_rows:
        all_uids.append(int(row[1]))
    for row in answer_rows:
        all_uids.append(int(row[2]))
    name_by_uid = _fetch_vk_user_names(vk, all_uids)
    profile_by_uid = _fetch_user_profiles(all_uids)
    display_name_by_uid: dict[int, str] = {}
    consent_text_by_uid: dict[int, str] = {}
    for profile_uid, profile in profile_by_uid.items():
        account_name = name_by_uid.get(profile_uid, "").strip()
        full_name = str(profile.get("full_name") or "").strip()
        if full_name and account_name and account_name.casefold() != full_name.casefold():
            display_name_by_uid[profile_uid] = f"{account_name} — ФИО: {full_name}"
        else:
            display_name_by_uid[profile_uid] = full_name or account_name
        consent_text_by_uid[profile_uid] = _format_profile_consent(profile)

    merged: list[tuple] = []
    for rid, uid, tid_raw, finished_at, scores_json, top3_json in result_rows:
        try:
            ts = int(finished_at)
        except (TypeError, ValueError):
            ts = 0
        merged.append(
            (ts, 0, int(rid), "result", uid, tid_raw, finished_at, scores_json, top3_json, None)
        )
    for sid, uid, tid_raw, started_at, sess_status, final_scores_json in session_rows:
        try:
            ts = int(started_at)
        except (TypeError, ValueError):
            ts = 0
        merged.append(
            (ts, 1, int(sid), "session", uid, tid_raw, started_at, sess_status, final_scores_json, sid)
        )
    merged.sort(key=lambda x: (x[0], x[1], x[2]))

    user_serial: dict[int, int] = {}
    next_serial = 1
    for _, _, _, kind, uid, tid_raw, t_field, payload_a, payload_b, sid_maybe in merged:
        uid = int(uid)
        tid = normalize_test_id((tid_raw or TEST_KLIMOV_SELF) if isinstance(tid_raw, str) else TEST_KLIMOV_SELF)
        if uid not in user_serial:
            user_serial[uid] = next_serial
            next_serial += 1
        no = user_serial[uid]
        display_name = display_name_by_uid.get(uid, name_by_uid.get(uid, ""))
        locality = str(profile_by_uid.get(uid, {}).get("locality", ""))
        consent_text = consent_text_by_uid.get(uid, "Нет подтверждения")
        test_name = _test_title_for_export(tid)
        if kind == "result":
            finished_txt = "Да"
            try:
                dt = datetime.utcfromtimestamp(int(t_field)).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, OSError):
                dt = ""
            scores = _loads_json_blob(payload_a, {})
            top3 = _loads_json_blob(payload_b, [])
            if not isinstance(top3, list):
                top3 = []
            summary = _export_result_summary(tid, scores, top3)
        else:
            sess_status = str(payload_a or "")
            if sess_status == "abandoned":
                finished_txt = "Нет — прервано"
                status_ru = "прервано"
            elif sess_status == "in_progress":
                finished_txt = "Нет"
                status_ru = "в процессе"
            else:
                finished_txt = "Нет"
                status_ru = sess_status or "неизвестно"
            try:
                dt = datetime.utcfromtimestamp(int(t_field)).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, OSError):
                dt = ""
            sid = int(sid_maybe) if sid_maybe is not None else 0
            n_ans = answer_counts.get(sid, 0)
            try:
                total_q = _opg_effective_question_count(tid)
            except Exception:
                total_q = 0
            parts = [f"Сессия {sid}, {status_ru}. Отвечено шагов: {n_ans}" + (f" из {total_q}." if total_q else ".")]
            if payload_b:
                scores = _loads_json_blob(payload_b, {})
                if isinstance(scores, dict) and scores:
                    extra = _export_result_summary(tid, scores, _top3_from_scores(scores))
                    if extra:
                        parts.append(f"Накоплено по ответам: {extra}")
            summary = "\n".join(parts)
        r = ws.max_row + 1
        ws.cell(row=r, column=1, value=no)
        _apply_user_link(ws.cell(row=r, column=2), uid)
        ws.cell(row=r, column=3, value=display_name)
        ws.cell(row=r, column=4, value=locality)
        ws.cell(row=r, column=5, value=consent_text)
        ws.cell(row=r, column=6, value=test_name)
        ws.cell(row=r, column=7, value=finished_txt)
        ws.cell(row=r, column=8, value=dt)
        ws.cell(row=r, column=9, value=summary)
        ws.row_dimensions[r].height = max(
            15,
            15 * max(
                _estimate_lines(summary, 100),
                _estimate_lines(display_name, 30),
                _estimate_lines(locality, 24),
                _estimate_lines(consent_text, 32),
            ),
        )

    _style_sheet_grid(
        ws,
        widths={1: 7, 2: 24, 3: 34, 4: 24, 5: 42, 6: 34, 7: 11, 8: 20, 9: 100},
        wrap_cols=[3, 4, 5, 6, 9],
    )

    ws_ans = wb.create_sheet("ответы")
    ans_headers = [
        "№ (новый пользователь — новый номер)",
        "Ссылка на пользователя",
        "Имя аккаунта / ФИО",
        "Населённый пункт",
        "Согласие на обработку ПДн",
        "Название теста",
        "Вопрос",
        "Ответ",
        "Результат",
    ]
    ws_ans.append(ans_headers)
    ans_sheet_uids: list[int] = []
    for ar in answer_rows:
        _aid, _sid, _uid, _tid_raw, _step, _akey, _qtxt, _alab, _wjson, _cat = ar
        _uid = int(_uid)
        _tid = normalize_test_id((_tid_raw or TEST_KLIMOV_SELF) if isinstance(_tid_raw, str) else TEST_KLIMOV_SELF)
        if _uid not in user_serial:
            user_serial[_uid] = next_serial
            next_serial += 1
        ans_sheet_uids.append(_uid)
        question = str(_qtxt or "")
        answer = str(_alab or "")
        if not answer:
            answer = str(_akey or "")
        result = _format_answer_result(_wjson)
        display_name = display_name_by_uid.get(_uid, name_by_uid.get(_uid, ""))
        locality = str(profile_by_uid.get(_uid, {}).get("locality", ""))
        consent_text = consent_text_by_uid.get(_uid, "Нет подтверждения")
        r = ws_ans.max_row + 1
        first_of_user = len(ans_sheet_uids) == 1 or ans_sheet_uids[-2] != _uid
        if first_of_user:
            ws_ans.cell(row=r, column=1, value=user_serial[_uid])
            _apply_user_link(ws_ans.cell(row=r, column=2), _uid)
            name_cell = ws_ans.cell(row=r, column=3, value=display_name)
            name_cell.alignment = openpyxl.styles.Alignment(vertical="top", wrap_text=True)
            ws_ans.cell(row=r, column=4, value=locality)
            ws_ans.cell(row=r, column=5, value=consent_text)
            ws_ans.cell(row=r, column=6, value=_test_title_for_export(_tid))
        ws_ans.cell(row=r, column=7, value=question)
        ws_ans.cell(row=r, column=8, value=answer)
        ws_ans.cell(row=r, column=9, value=result)
        ws_ans.row_dimensions[r].height = max(
            15,
            15 * max(
                _estimate_lines(question, 70),
                _estimate_lines(result, 45),
                _estimate_lines(display_name, 30),
                _estimate_lines(locality, 24),
                _estimate_lines(consent_text, 32),
                1,
            ),
        )

    _merge_user_columns(ws_ans, uids=ans_sheet_uids, max_col=6)
    _style_sheet_grid(
        ws_ans,
        widths={1: 7, 2: 24, 3: 34, 4: 24, 5: 42, 6: 34, 7: 70, 8: 24, 9: 45},
        wrap_cols=[3, 4, 5, 6, 7, 8, 9],
    )

    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def send_stats_export(
    vk,
    user_id: int,
    *,
    since: int | None = None,
    until: int | None = None,
    period_label: str = "all",
):
    period_label = period_label or "all"
    pl = period_label

    if pl == "today_msk":
        period_human = "за сегодня (МСК)"
        n_done = test_results_row_count_filtered(since, until)
        n_open = incomplete_sessions_row_count_filtered(since, until)
    elif pl in ("week_7d", "month_30d", "quarter_90d"):
        period_human = {
            "week_7d": "за последние 7 суток",
            "month_30d": "за последние 30 суток",
            "quarter_90d": "за последние 90 суток (квартал)",
        }[pl]
        n_done = test_results_row_count_filtered(since, until)
        n_open = incomplete_sessions_row_count_filtered(since, until)
    else:
        since, until = None, None
        period_human = "за всё время"
        n_done = test_results_row_count_filtered(None, None)
        n_open = incomplete_sessions_row_count_filtered(None, None)

    n_ans = answer_log_row_count_filtered(since, until)

    data = build_stats_excel_bytes(vk, since=since, until=until)
    suffix = {
        "today_msk": "today_msk",
        "week_7d": "week",
        "month_30d": "month",
        "quarter_90d": "quarter",
        "all": "all_time",
    }.get(pl, "all_time")
    fname = f"stats_answers_{suffix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    bio = io.BytesIO(data)
    bio.name = fname
    peer = _peer_id_for_send(user_id)
    print(f"[stats_export] peer_id={peer} size={len(data)} rows_ans={n_ans}")
    att_str = _upload_stats_document(vk, bio, fname, peer)
    note = (
        f"Excel ({period_human}): лист «сводка» — завершённые тесты ({n_done}) + незавершённые сессии ({n_open}); "
        f"лист «ответы» — все пошаговые ответы из журнала ({n_ans}).\n"
        "Колонки C–E — имя аккаунта с ФИО, населённый пункт и подтверждение согласия с датами действия. Колонка «№» — порядковый номер пользователя по первому появлению в хронологии.\n"
        "Команды: «отчет все время», «отчет квартал», «отчет месяц», «отчет неделя»; «сегодня» / «за сегодня» — только текущие сутки по МСК; "
        "«выгрузка» или /stats — всё время."
    )
    vk.messages.send(
        peer_id=peer,
        random_id=0,
        message=note,
        attachment=att_str,
    )


def handle_stats_command(vk, user_id: int, text: str) -> bool:
    if not is_stats_admin(vk, user_id):
        if getattr(vk, "platform", "vk") == "max":
            admin_help = (
                "Для MAX укажите ваш числовой ID в переменной MAX_STATS_ADMIN_IDS "
                "(в Railway Variables; ID берётся из MAX update/user). Затем Redeploy."
            )
        else:
            admin_help = (
                "Откройте Railway → ваш сервис → Variables и добавьте STATS_ADMIN_IDS = ваш числовой id ВК "
                "(только цифры, без пробелов). Id смотрите в ссылке vk.com/id… или через настройки. Затем Redeploy."
            )
        send_message(
            vk,
            user_id,
            "Выгрузка только для администраторов.\n\n"
            + admin_help
            + "\nКоманды выгрузки: «отчет все время», «отчет квартал» (90 суток), «отчет месяц» (30 суток), "
            "«отчет неделя» (7 суток); «выгрузка» или /stats — всё время; «сегодня» — только текущие сутки по МСК.",
        )
        return True
    try:
        period_label, since_ts, until_ts = _parse_stats_export_period(text)
        send_stats_export(
            vk,
            user_id,
            since=since_ts,
            until=until_ts,
            period_label=period_label,
        )
    except Exception as e:
        print(f"[handle_stats_command] {e!r}")
        hint = (
            "Не удалось отправить Excel.\n\n"
            "Чаще всего: у ключа сообщества нет права «Документы» (docs) — VK отвечает пустой страницей, "
            "отсюда «Expecting value…».\n"
            "Сообщество → Управление → Работа с API → Ключи → создать ключ с доступом: "
            "«Сообщения сообщества» + «Документы» (и «Управление сообществом»). "
            "Подставьте новый VK_TOKEN в Railway → Redeploy.\n\n"
            f"Технически: {e}"
        )
        send_message(vk, user_id, hint)
    return True


def save_result(user_id: int, test_id: str, scores: dict, top3: list):
    """Сохраняет результат; учитывает старые БД с колонкой best_area вместо/рядом с best_type."""
    best_type = top3[0][0] if top3 else "Не определено"
    ts = now_ts()
    scores_json = json.dumps(dict(scores), ensure_ascii=False)
    top3_json = json.dumps(top3, ensure_ascii=False)
    with db_connect() as conn:
        cur = conn.cursor()
        col_names = table_column_names(conn, "test_results")
        row = {
            "user_id": user_id,
            "finished_at": ts,
            "scores_json": scores_json,
            "top3_json": top3_json,
            "test_id": test_id,
            "best_type": best_type,
            "best_area": best_type,
        }
        insert_cols = [c for c in row if c in col_names]
        placeholders = ", ".join(["?"] * len(insert_cols))
        sql = f"INSERT INTO test_results ({', '.join(insert_cols)}) VALUES ({placeholders})"
        cur.execute(sql, [row[c] for c in insert_cols])
        conn.commit()


def users_for_reminder():
    ts = now_ts()
    inactive_threshold = ts - REMINDER_AFTER_INACTIVE_MIN * 60
    repeat_threshold = ts - REMINDER_REPEAT_MIN * 60
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT user_id
            FROM user_progress
            WHERE status='in_progress'
              AND test_id NOT IN (?, ?)
              AND last_activity_at <= ?
              AND (reminded_at IS NULL OR reminded_at <= ?)
              AND EXISTS (
                  SELECT 1 FROM user_profiles p
                  WHERE p.user_id=user_progress.user_id
                    AND p.consent_status='accepted'
                    AND p.onboarding_step='complete'
                    AND p.consent_expires_at > ?
              )
            """,
            (TEST_JOVASHI, TEST_HOLLAND_RIASEC, inactive_threshold, repeat_threshold, ts),
        )
        rows = cur.fetchall()
        return [r[0] for r in rows]


def build_answer_keyboard_binary():
    kb = VkKeyboard(one_time=False, inline=True)
    kb.add_button("1", color=VkKeyboardColor.PRIMARY)
    kb.add_button("2", color=VkKeyboardColor.PRIMARY)
    return kb.get_keyboard()


def build_answer_keyboard_jovashi():
    kb = VkKeyboard(one_time=False, inline=True)
    kb.add_button("1", color=VkKeyboardColor.PRIMARY)
    kb.add_button("2", color=VkKeyboardColor.PRIMARY)
    kb.add_button("3", color=VkKeyboardColor.PRIMARY)
    return kb.get_keyboard()


def build_answer_keyboard_quad():
    kb = VkKeyboard(one_time=False, inline=True)
    kb.add_button("1", color=VkKeyboardColor.PRIMARY)
    kb.add_button("2", color=VkKeyboardColor.PRIMARY)
    kb.add_button("3", color=VkKeyboardColor.PRIMARY)
    kb.add_button("4", color=VkKeyboardColor.PRIMARY)
    return kb.get_keyboard()


def build_answer_keyboard_five():
    kb = VkKeyboard(one_time=False, inline=True)
    for i in range(1, 6):
        kb.add_button(str(i), color=VkKeyboardColor.PRIMARY)
    return kb.get_keyboard()


def build_answer_keyboard_six():
    kb = VkKeyboard(one_time=False, inline=True)
    for i in range(1, 7):
        kb.add_button(str(i), color=VkKeyboardColor.PRIMARY)
    return kb.get_keyboard()


def build_answer_keyboard_many(n: int):
    """Кнопки 1..n в рядах по 5 (для задач с числовым выбором)."""
    kb = VkKeyboard(one_time=False, inline=True)
    for i in range(1, n + 1):
        kb.add_button(str(i), color=VkKeyboardColor.PRIMARY)
        if i % 5 == 0 and i < n:
            kb.add_line()
    return kb.get_keyboard()


def build_reminder_continue_keyboard():
    kb = VkKeyboard(one_time=False, inline=True)
    kb.add_button("Да", color=VkKeyboardColor.POSITIVE)
    kb.add_button("Нет", color=VkKeyboardColor.NEGATIVE)
    return kb.get_keyboard()


def build_consent_keyboard():
    import json as _json

    kb = VkKeyboard(inline=True)
    kb.add_button("1 — Да, согласен(на)", color=VkKeyboardColor.POSITIVE)
    kb.add_button("2 — Нет, не согласен(на)", color=VkKeyboardColor.NEGATIVE)
    raw = kb.get_keyboard()
    data = _json.loads(raw) if isinstance(raw, str) else dict(raw)
    # Для inline-клавиатуры VK запрещает поле one_time (ошибка 911): удаляем его.
    data.pop("one_time", None)
    return _json.dumps(data, ensure_ascii=False)


def build_menu_keyboard():
    kb = VkKeyboard(one_time=False, inline=False)
    kb.add_button(KB_KLIMOV_SELF, color=VkKeyboardColor.POSITIVE)
    kb.add_button(KB_OPG, color=VkKeyboardColor.POSITIVE)
    kb.add_button(KB_YOVASHI, color=VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button(KB_KETTELL, color=VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button(KB_KETTELL_16PF_C_YOUTH, color=VkKeyboardColor.POSITIVE)
    kb.add_button(KB_KOT, color=VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button(KB_EN60, color=VkKeyboardColor.POSITIVE)
    kb.add_button(KB_EN57, color=VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button("Меню", color=VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button("Отозвать согласие", color=VkKeyboardColor.NEGATIVE)
    return kb.get_keyboard()


def keyboard_for_test(test_id: str, step: int = 0, scores: dict | None = None):
    tid = normalize_test_id(test_id)
    qs = questions_for(tid)
    if tid == TEST_OPG and scores is not None:
        idx = _opg_flat_index(step, scores)
        nopts = len(qs[idx]["options"]) if 0 <= idx < len(qs) else 2
    else:
        nopts = len(qs[step]["options"]) if step < len(qs) else 2
    if nopts > 6:
        return build_answer_keyboard_many(nopts)
    if nopts == 6:
        return build_answer_keyboard_six()
    if nopts == 5:
        return build_answer_keyboard_five()
    if nopts == 4:
        return build_answer_keyboard_quad()
    if nopts == 3:
        return build_answer_keyboard_jovashi()
    return build_answer_keyboard_binary()


def send_message(vk, user_id, message, keyboard=None, attachment=None):
    peer = _peer_id_for_send(user_id)
    kw: dict = dict(peer_id=peer, random_id=0, message=message, keyboard=keyboard)
    if attachment:
        kw["attachment"] = attachment
    vk.messages.send(**kw)


def upload_vk_photo(vk, user_id: int, image_path: str):
    """Загружает изображение для текущей платформы; при ошибке — None."""
    if not vk or not image_path or not os.path.isfile(image_path):
        return None
    try:
        if getattr(vk, "platform", "vk") == "max":
            return vk.upload_image(image_path, user_id)
        peer = _peer_id_for_send(user_id)
        up = VkUpload(vk)
        ph = up.photo_messages(image_path, peer_id=peer)
        if isinstance(ph, dict):
            return f"photo{ph['owner_id']}_{ph['id']}"
        if isinstance(ph, list) and ph:
            p = ph[0]
            return f"photo{p['owner_id']}_{p['id']}"
    except Exception as e:
        print(f"[upload_vk_photo] {image_path}: {e}")
    return None


def send_question_message(vk, user_id: int, test_id: str, step: int, keyboard=None, scores: dict | None = None):
    """Текст вопроса + вложение-картинка для отдельных шагов КОТ."""
    tid = normalize_test_id(test_id)
    text = render_question(tid, step, scores)
    att = None
    if tid == TEST_KOT:
        path = KOT_QUESTION_IMAGE_PATHS.get(step)
        if path:
            att = upload_vk_photo(vk, user_id, path)
    send_message(vk, user_id, text, keyboard=keyboard, attachment=att)


def _display_answer_label(raw: str) -> str:
    """Первая буква подписи варианта — строчная (латиница/кириллица), остальное без изменений."""
    s = raw if isinstance(raw, str) else str(raw)
    if not s:
        return s
    for i, ch in enumerate(s):
        if ch.isalpha():
            return s[:i] + ch.lower() + s[i + 1 :]
    return s


def render_question(test_id: str, step: int, scores: dict | None = None) -> str:
    tid = normalize_test_id(test_id)
    qs = questions_for(tid)
    if tid == TEST_OPG and scores is not None:
        idx = _opg_flat_index(step, scores)
        item = qs[idx] if 0 <= idx < len(qs) else qs[min(step, len(qs) - 1)]
    else:
        item = qs[step]
    lines = [item["q"]]
    keys = sorted(item["options"].keys(), key=lambda x: int(x))
    for key in keys:
        opt = item["options"][key]
        label = _display_answer_label(opt[0] if isinstance(opt[0], str) else str(opt[0]))
        lines.append(f"{key}) {label}")
    n = len(keys)
    lines.append("\nВыберите вариант кнопкой.")
    return "\n".join(lines)


def _interpret_jovashi(score: int) -> str:
    if score >= 10:
        return "ярко выражена"
    if score >= 7:
        return "средне выражена"
    if score >= 4:
        return "слабо выражена"
    return "практически не выражена"


def _label_for_test(test_id: str) -> str:
    tid = normalize_test_id(test_id)
    return {
        TEST_KLIMOV_SELF: LABEL_KLIMOV_SELF,
        TEST_OPG: LABEL_OPG,
        TEST_JOVASHI: LABEL_PROF_TABLE,
        TEST_YOVASHI: LABEL_YOVASHI,
        TEST_KETTELL: LABEL_KETTELL,
        TEST_KETTELL_16PF_C_YOUTH: LABEL_KETTELL_16PF_C_YOUTH,
        TEST_KOT: LABEL_KOT,
        TEST_EN60: LABEL_EN60,
        TEST_EN57: LABEL_EN57,
        TEST_HOLLAND_RIASEC: LABEL_HOLLAND,
    }.get(tid, "тест")


def finish_test(vk, user_id: int, test_id: str, scores: dict):
    tid = normalize_test_id(test_id)
    prog = get_progress(user_id)
    sid = prog.get("last_session_id") if prog else None
    store_scores = _opg_scores_storable(scores) if tid == TEST_OPG else scores
    if sid:
        complete_test_session(sid, user_id, store_scores, "completed")
    if tid == TEST_OPG:
        ranked_wish, subs = _opg_finish_totals(scores)
        top3 = ranked_wish[:3]
        best_key = top3[0][0]
        lines = [
            f"📊 Результат «{LABEL_OPG}» (ориентир по методике ОПГ):",
            "",
            "Топ-3 профессиональных сфер по сумме «желания» (включая строки с «умение» = 0 по правилам методички сумма скорректирована):",
        ]
        for i, (sp, w) in enumerate(top3, 1):
            lines.append(f"{i}. {PROFESSION_TYPES[sp]} — {w} баллов (ориентир. макс. {OPG_MAX_PER_DIM})")
        lines.append("")
        lines.append("По всем сферам Климова — три шкалы: умение / отношение / желание:")
        for sp in OPG_SPHERES_ORDER:
            d = subs[sp]
            lines.append(
                f"• {PROFESSION_TYPES[sp]}: умение {d['skill']}/{OPG_MAX_PER_DIM}, "
                f"отношение {d['att']}/{OPG_MAX_PER_DIM}, желание {d['wish']}/{OPG_MAX_PER_DIM}"
            )
        lines.append(
            "\nСопоставьте шкалы: благоприятнее, когда желание и отношение согласуются с умением (см. методичку)."
        )
        lines.append(f"\n{CAREER_HINTS_OPG.get(best_key, '')}")
        lines.append("")
        lines.append(POST_TEST_REFERRAL_TRUDVSEM)
        send_message(vk, user_id, "\n".join(lines), keyboard=build_menu_keyboard())
        complete_progress(user_id)
        save_result(user_id, tid, store_scores, top3)
        return

    sorted_types = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    if not sorted_types:
        send_message(
            vk,
            user_id,
            "Не удалось посчитать результат. Напишите «меню» и начните тест заново.",
            keyboard=build_menu_keyboard(),
        )
        return
    top3 = sorted_types[:3]
    best_key = top3[0][0]

    if tid == TEST_KLIMOV_SELF:
        title = (
            "📊 Результат по ДДО (ориентировочное определение типа будущей специальности по самооценке; "
            "столбцы П, Т, З, Х, Ч):"
        )
        lines = [title, ""]
        for pk in KLIMOV_SELF_DISPLAY_ORDER:
            n = int(scores.get(pk, 0) or 0)
            mx = KLIMOV_SELF_MAX_BY_CATEGORY.get(pk, 10)
            lines.append(f"• {KLIMOV_SELF_TYPES[pk]}: {n} из {mx} — {_ddo_interpret_band(n, mx)}")
        lines.append("")
        lines.append("Топ-3 по сумме баллов:")
        for i, (ptype, points) in enumerate(top3, 1):
            mx = KLIMOV_SELF_MAX_BY_CATEGORY.get(ptype, 10)
            lines.append(
                f"{i}. {KLIMOV_SELF_TYPES[ptype]} — {points} из {mx} ({_ddo_interpret_band(int(points), mx)})"
            )
        lines.append(
            f"\n{CAREER_HINTS_KLIMOV_SELF.get(best_key, 'Выберите направление, которое откликается сильнее.')}"
        )
        lines.append("\nХотите пройти снова — выберите тест кнопкой или командой.")
    elif tid == TEST_JOVASHI:
        lines = [f"📊 Ваш результат по «{LABEL_PROF_TABLE}» (топ-3 сферы):"]
        for i, (key, points) in enumerate(top3, 1):
            interp = _interpret_jovashi(points)
            lines.append(f"{i}. {JOVASHI_SPHERES[key]} — {points} баллов ({interp})")
        lines.append(f"\n{CAREER_HINTS_JOVASHI.get(best_key, '')}")
        lines.append("\nСравните несколько сильных сфер и подумайте, какие профессии их объединяют.")
    elif tid == TEST_YOVASHI:
        lines = [f"📊 Ваш результат по «{LABEL_YOVASHI}» (топ-3 сферы):"]
        for i, (key, points) in enumerate(top3, 1):
            interp = _interpret_jovashi(points)
            lines.append(f"{i}. {JOVASHI_SPHERES[key]} — {points} баллов ({interp})")
        lines.append(f"\n{CAREER_HINTS_JOVASHI.get(best_key, '')}")
        lines.append("\nСравните несколько сильных сфер и подумайте, какие профессии их объединяют.")
    elif tid in (TEST_KETTELL, TEST_KETTELL_16PF_C_YOUTH):
        mxmap = _pf16_block_max_map(tid)
        disp = LABEL_KETTELL_16PF_C_YOUTH if tid == TEST_KETTELL_16PF_C_YOUTH else LABEL_KETTELL
        ranked_finish = sorted(
            ((k, scores.get(k, 0)) for k in PF16_ORDER if mxmap.get(k, 0) > 0),
            key=lambda x: x[1],
            reverse=True,
        )
        top_show = ranked_finish[:3]
        lines = [
            f"📊 Результат «{disp}» (ориентир, не клиническая диагностика):",
            "",
            "Топ-3 фактора по ориентировочной сумме в боте:",
        ]
        for i, (key, points) in enumerate(top_show, 1):
            mx = mxmap.get(key, 0)
            lines.append(f"{i}. {KETTELL_TRAITS[key]} — {points:g} из {mx:g}")
        lines.append("")
        lines.append("Все первичные факторы (сырой балл / условный макс. в этом подсчёте):")
        for code in PF16_ORDER:
            mx = mxmap.get(code, 0)
            if mx <= 0:
                continue
            v = scores.get(code, 0)
            lines.append(f"• {code}: {v:g} из {mx:g}")
        bk = top_show[0][0] if top_show else best_key
        lines.append(f"\n{CAREER_HINTS_KETTELL.get(bk, '')}")
        if tid == TEST_KETTELL_16PF_C_YOUTH:
            lines.append(
                "\nПодсчёт — упрощённый: 15 блоков по 7 пунктов; фактор Q4 в форме не представлен. "
                "Используйте официальную обработку издателя для норм."
            )
        else:
            lines.append(
                "\nПодсчёт в боте — упрощённый учебный: блоки пунктов как в форме A, без официальной таблицы весов. "
                "Для решений о лицензии/кадрах используйте обработку по методике издателя."
            )
    elif tid == TEST_KOT:
        total = len(QUESTIONS_KOT)
        correct = scores.get("LOGIC", 0)
        pct = round(100 * correct / total) if total else 0
        lines = [
            f"📊 Результат «{LABEL_KOT}»: {correct} из {total} верных ({pct}%).",
            "",
            "КОТ — ориентировочный тест общих способностей (из учебных сборников). В боте нет чертежей из бланка: "
            "пункты с рисунками заменены на текстовые варианты для самопроверки.",
        ]
        if pct >= 75:
            lines.append("\nСильный результат — продолжайте тренировать внимание и логику на разных типах заданий.")
        elif pct >= 50:
            lines.append("\nСредний уровень — полезно разбирать ошибки и возвращаться к пунктам с вычислениями.")
        else:
            lines.append("\nЕсть куда расти: разбирайте каждое задание и закрепляйте термины и приёмы счёта.")
    elif tid == TEST_EN57:
        e = scores.get("E", 0)
        n = scores.get("N", 0)
        l = scores.get("L", 0)
        lines = [
            f"📊 Результат «{LABEL_EN57}» / EPI (ориентир, не клиническая диагностика):",
            f"• {EN_LABELS['E']}: {e} из 24.",
            f"• {EN_LABELS['N']}: {n} из 24.",
            f"• {EN_LIE_LABEL}: {l} из 9.",
            "",
            "Формат ориентирован на взрослых. Шкала L показывает меру «социально желательных» ответов; "
            "интерпретируйте осторожно.",
            "",
            "Больше баллов по E — склонность к активности и контактам; по N — сильнее реакция на стресс. "
            "Обсудите сомнения со специалистом.",
        ]
    elif tid == TEST_EN60:
        e = scores.get("E", 0)
        n = scores.get("N", 0)
        l = scores.get("L", 0)
        lines = [
            f"📊 Результат «{LABEL_EN60}» (ориентир, не клиническая диагностика):",
            f"• {EN_LABELS['E']}: {e} из 24.",
            f"• {EN_LABELS['N']}: {n} из 24.",
            f"• {EN_LIE_LABEL}: {l} из 12.",
            "",
            "Удобнее для детей и подростков. Счёт по ключу: «да» / «нет» на отмеченных пунктах.",
            "",
            "Больше баллов по E — склонность к активности и контактам; по N — сильнее чувствительность к стрессу. "
            "Высокий L может указывать на «социально желательные» ответы. Обсудите сомнения со специалистом.",
        ]
    elif tid == TEST_HOLLAND_RIASEC:
        lines = [
            f"📊 Результат «{LABEL_HOLLAND}» (шесть типов по ключу из методички):",
            "",
            "Топ-3 по сумме совпадений:",
        ]
        for i, (code, points) in enumerate(top3, 1):
            mx = HOLLAND_MAX.get(code, 15)
            lines.append(f"{i}. {HOLLAND_DIMENSIONS[code]} — {points} из ориентир. макс. {mx}")
        lines.append("")
        lines.append("Все типы (R, I, S, C, E, A):")
        for code in HOLLAND_ORDER:
            v = int(scores.get(code, 0))
            mx = HOLLAND_MAX[code]
            lines.append(f"• {HOLLAND_DIMENSIONS[code]}: {v} из {mx}")
        lines.append("")
        lines.append(
            "Где в таблице ключей один ответ отмечен в двух столбцах (например, вариант В в 1-м пункте), "
            "в боте начисляются оба балла — как при ручной отметке по методичке."
        )
        lines.append(f"\n{CAREER_HINTS_HOLLAND.get(best_key, '')}")
        lines.append(
            "\nЭто ориентир по предпочтениям в профессии, не заменяет полноценную профориентационную беседу."
        )
    else:
        lines = ["Результат сохранён. Откройте меню и выберите другой тест."]

    lines.append("")
    lines.append(POST_TEST_REFERRAL_TRUDVSEM)
    send_message(vk, user_id, "\n".join(lines), keyboard=build_menu_keyboard())
    complete_progress(user_id)
    save_result(user_id, tid, store_scores, top3)


def start_test(vk, user_id: int, test_id: str):
    tid = normalize_test_id(test_id)
    if not user_profile_is_complete(user_id):
        send_welcome(vk, user_id)
        return
    if tid in (TEST_JOVASHI, TEST_HOLLAND_RIASEC):
        send_message(
            vk,
            user_id,
            "Этот тест больше недоступен. Напишите «меню», чтобы выбрать другой.",
            keyboard=build_menu_keyboard(),
        )
        return
    scores = empty_scores(tid)
    session_id = create_test_session(user_id, tid)
    save_progress(
        user_id=user_id,
        test_id=tid,
        step=0,
        scores=scores,
        status="in_progress",
        last_session_id=session_id,
    )
    nq = _opg_effective_question_count(tid)
    intro = f"Начинаем тест «{_label_for_test(tid)}». Вопросов: {nq}."
    _kb_scores = scores if tid == TEST_OPG else None
    send_message(vk, user_id, intro, keyboard=keyboard_for_test(tid, 0, _kb_scores))
    send_question_message(
        vk,
        user_id,
        tid,
        0,
        keyboard=keyboard_for_test(tid, 0, _kb_scores),
        scores=_kb_scores,
    )


_CONSENT_PROMPTED_USERS: set[int] = set()


def _send_consent_prompt(vk, user_id: int) -> None:
    _CONSENT_PROMPTED_USERS.add(int(user_id))
    profile = get_user_profile(user_id)
    full_name = str((profile or {}).get("full_name") or "").strip()
    text = CONSENT_TEXT
    if full_name:
        # Подставляем ФИО, которое пользователь указал на предыдущем шаге.
        text = CONSENT_TEXT.replace("пользователь этого аккаунта мессенджера", full_name, 1)
    send_message(vk, user_id, text, keyboard=build_consent_keyboard())


def send_welcome(vk, user_id: int):
    profile = get_user_profile(user_id)
    if profile and profile.get("consent_status") == "accepted" and (
        int(profile.get("consent_expires_at") or 0) <= now_ts()
        or profile.get("consent_version") != CONSENT_VERSION
    ):
        delete_user_personal_data(user_id)
        profile = None
    if not profile or not profile.get("full_name"):
        send_message(vk, user_id, WELCOME_INTRO)
        send_message(vk, user_id, "Напишите ваши ФИО полностью (фамилия, имя и отчество, если есть).")
    elif profile.get("consent_status") not in ("accepted", "refused"):
        _send_consent_prompt(vk, user_id)
    elif not profile.get("locality"):
        send_message(vk, user_id, "Укажите ваш населённый пункт: город, село, деревню или посёлок.")
    else:
        send_message(vk, user_id, WELCOME_TEXT, keyboard=build_menu_keyboard())


def _valid_full_name(value: str) -> bool:
    parts = value.split()
    return (
        2 <= len(parts) <= 4
        and all(any(char.isalpha() for char in part) and all(char.isalpha() or char in "-'’" for char in part) for part in parts)
    )


def handle_onboarding_message(vk, user_id: int, text: str) -> bool:
    """Онбординг: ФИО → согласие на обработку данных → населённый пункт → тесты."""
    stripped = " ".join(_strip_command_text(text).split())
    command = _normalize_cmd(stripped)
    profile = get_user_profile(user_id)
    if profile and profile.get("consent_status") == "accepted" and (
        int(profile.get("consent_expires_at") or 0) <= now_ts()
        or profile.get("consent_version") != CONSENT_VERSION
    ):
        delete_user_personal_data(user_id)
        profile = None

    if profile and profile.get("onboarding_step") == "complete" and profile.get("consent_status") in ("accepted", "refused"):
        return False

    if command in (
        "привет", "старт", "start", "меню", "menu", "/start", "начать", "hello", "hi",
        "ддо", "климов", "самооценка", "климов30", "опг", "opg", "таблица", "таблица опт", "опт",
        "таблица (опт проф.)",
        "голланд", "holland", "riasec", "йовайши", "йоваши", "yovashi", "iovashi", "jovashi",
        "кеттелл", "kettell", "cattell", "16pf", "16пф", "16pfc", "16пфс", "16pf/c (мол.)",
        "кот", "kot", "эн-60", "эн60", "эн - 60", "en-60", "en60", "эн-57", "эн57", "эн - 57", "en-57", "en57",
    ):
        send_welcome(vk, user_id)
        return True

    # Шаг 1: ФИО (запрашивается раньше согласия и населённого пункта).
    if not profile or not profile.get("full_name"):
        full_name = stripped[:160]
        if (
            len(full_name) < 5
            or command in ("1", "2", "да", "нет", "меню", "/start", "отозвать согласие")
            or not _valid_full_name(full_name)
        ):
            send_message(vk, user_id, "Укажите ФИО: фамилию и имя, а если есть — отчество. Используйте буквы и дефис.")
            return True
        save_user_full_name(user_id, full_name)
        send_message(vk, user_id, "ФИО сохранены.")
        if profile and profile.get("consent_status") in ("accepted", "refused"):
            send_welcome(vk, user_id)
        else:
            _send_consent_prompt(vk, user_id)
        return True

    # Шаг 2: согласие на обработку персональных данных.
    if profile.get("consent_status") not in ("accepted", "refused"):
        if int(user_id) not in _CONSENT_PROMPTED_USERS:
            _send_consent_prompt(vk, user_id)
            return True
        if command in ("1", "да", "согласен", "согласна") or command.startswith("1 "):
            _CONSENT_PROMPTED_USERS.discard(int(user_id))
            accept_user_consent(user_id)
            send_message(vk, user_id, "Укажите ваш населённый пункт: город, село, деревню или посёлок.")
            return True
        if command in ("2", "нет", "не согласен", "не согласна") or command.startswith("2 "):
            _CONSENT_PROMPTED_USERS.discard(int(user_id))
            refuse_user_consent(user_id)
            send_message(vk, user_id, "Укажите ваш населённый пункт: город, село, деревню или посёлок.")
            return True
        _send_consent_prompt(vk, user_id)
        return True

    # Шаг 3: населённый пункт.
    if profile.get("onboarding_step") == "locality" or not profile.get("locality"):
        locality = stripped[:120]
        if (
            len(locality) < 3
            or command in ("1", "2", "да", "нет", "меню", "/start", "отозвать согласие")
            or not any(char.isalpha() for char in locality)
        ):
            send_message(vk, user_id, "Напишите населённый пункт словами, например: Ижевск или село Завьялово.")
            return True
        save_user_locality(user_id, locality)
        send_welcome(vk, user_id)
        return True

    send_welcome(vk, user_id)
    return True


def _option_weights(option_val):
    """Возвращает словарь весов для варианта ответа (кортеж/список: текст, веса)."""
    if isinstance(option_val, (list, tuple)) and len(option_val) >= 2 and isinstance(option_val[1], dict):
        return option_val[1]
    raise ValueError("Некорректный формат варианта ответа")


def handle_answer(vk, user_id: int, text: str):
    if not user_profile_is_complete(user_id):
        send_welcome(vk, user_id)
        return
    progress = get_progress(user_id)
    if not progress or progress["status"] != "in_progress":
        send_message(
            vk,
            user_id,
            "Активного теста нет. Напишите «меню» и выберите тест.",
            keyboard=build_menu_keyboard(),
        )
        return
    test_id = progress["test_id"]
    tid = normalize_test_id(test_id)
    if tid in (TEST_JOVASHI, TEST_HOLLAND_RIASEC):
        abandon_progress(user_id, progress.get("last_session_id"))
        send_message(
            vk,
            user_id,
            "Этот тест больше недоступен. Напишите «меню», чтобы выбрать другой.",
            keyboard=build_menu_keyboard(),
        )
        return
    touch_progress(user_id)
    step = progress["step"]
    qs = questions_for(tid)
    scores = progress["scores"]
    _kb_scores = scores if tid == TEST_OPG else None

    if tid == TEST_OPG:
        if step >= OPG_ITEM_COUNT:
            finish_test(vk, user_id, tid, scores)
            return
        flat_i = _opg_flat_index(step, scores)
    else:
        if step >= len(qs):
            finish_test(vk, user_id, tid, scores)
            return
        flat_i = step

    if flat_i >= len(qs):
        finish_test(vk, user_id, tid, scores)
        return

    valid = set(qs[flat_i]["options"].keys())
    if text not in valid:
        send_message(
            vk,
            user_id,
            f"Выберите один из вариантов: {' / '.join(sorted(valid, key=lambda x: int(x)))}.",
            keyboard=keyboard_for_test(tid, step, _kb_scores),
        )
        send_question_message(
            vk,
            user_id,
            tid,
            step,
            keyboard=keyboard_for_test(tid, step, _kb_scores),
            scores=_kb_scores,
        )
        return
    opt_val = qs[flat_i]["options"][text]
    weights = _option_weights(opt_val)
    q_text = qs[flat_i]["q"]
    ans_label = opt_val[0] if isinstance(opt_val[0], str) else str(opt_val[0])
    sid = progress.get("last_session_id")
    if sid:
        log_answer_row(
            sid,
            user_id,
            tid,
            flat_i if tid == TEST_OPG else step,
            text,
            q_text,
            ans_label,
            weights,
        )
    for ptype, value in weights.items():
        if ptype in scores:
            scores[ptype] = scores[ptype] + value
    if tid == TEST_OPG:
        item = qs[flat_i]
        oi = item.get("opg_item")
        od = item.get("opg_dim")
        if oi is not None and od in ("skill", "att", "wish"):
            meta = scores.get(OPG_META_KEY)
            if not isinstance(meta, dict):
                meta = {}
                scores[OPG_META_KEY] = meta
            key = str(int(oi))
            st = meta.get(key)
            if not isinstance(st, dict):
                st = {}
                meta[key] = st
            st[od] = int(next(iter(weights.values())))
        flow = _opg_ensure_flow(scores)
        part = int(flow.get("part", 0) or 0)
        if part < 2:
            flow["part"] = part + 1
            save_progress(
                user_id=user_id,
                test_id=tid,
                step=step,
                scores=scores,
                status="in_progress",
                last_session_id=sid,
            )
            send_question_message(
                vk,
                user_id,
                tid,
                step,
                keyboard=keyboard_for_test(tid, step, _kb_scores),
                scores=_kb_scores,
            )
        else:
            flow["part"] = 0
            step += 1
            if step < OPG_ITEM_COUNT:
                save_progress(
                    user_id=user_id,
                    test_id=tid,
                    step=step,
                    scores=scores,
                    status="in_progress",
                    last_session_id=sid,
                )
                send_question_message(
                    vk,
                    user_id,
                    tid,
                    step,
                    keyboard=keyboard_for_test(tid, step, _kb_scores),
                    scores=_kb_scores,
                )
            else:
                save_progress(
                    user_id=user_id,
                    test_id=tid,
                    step=step,
                    scores=scores,
                    status="completed",
                    last_session_id=sid,
                )
                finish_test(vk, user_id, tid, scores)
        return

    step += 1
    if step < len(qs):
        save_progress(
            user_id=user_id,
            test_id=tid,
            step=step,
            scores=scores,
            status="in_progress",
            last_session_id=sid,
        )
        send_question_message(vk, user_id, tid, step, keyboard=keyboard_for_test(tid, step))
    else:
        save_progress(
            user_id=user_id,
            test_id=tid,
            step=step,
            scores=scores,
            status="completed",
            last_session_id=sid,
        )
        finish_test(vk, user_id, tid, scores)


def personal_data_cleanup_worker():
    while True:
        try:
            purge_expired_personal_data()
        except Exception as e:
            print(f"[personal_data_cleanup] error: {e}")
        time.sleep(3600)


def reminder_worker():
    vk_session = vk_api.VkApi(token=VK_TOKEN)
    vk = vk_session.get_api()
    while True:
        try:
            for uid in users_for_reminder():
                if uid <= 0:
                    continue  # MAX использует отрицательные внутренние ID; напоминания отправляет его процесс.
                progress = get_progress(uid)
                if not progress or progress["status"] != "in_progress":
                    continue
                test_id = progress["test_id"]
                tid = normalize_test_id(test_id)
                total = _opg_effective_question_count(tid)
                step_display = progress["step"] + 1
                send_message(
                    vk,
                    uid,
                    f"⏰ Напоминание: Вы на вопросе {step_display} из {total}.\n"
                    "Продолжим? «Да» — вернёмся к опросу, «Нет» — выход в меню.",
                    keyboard=build_reminder_continue_keyboard(),
                )
                set_reminder_pending(uid, 1)
                set_reminded(uid)
        except Exception as e:
            print(f"[reminder_worker] error: {e}")
        time.sleep(REMINDER_CHECK_EVERY_SEC)


def _normalize_cmd(s: str) -> str:
    return s.strip().lower()


def _normalize_unicode_command(s: str) -> str:
    """NFKC + замена похожих на «/» символов (некоторые клиенты шлют не ASCII U+002F)."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    trans = str.maketrans(
        {
            "\u2044": "/",  # ⁄ fraction slash
            "\u2215": "/",  # ∕ division slash
            "\u29f8": "/",  # ⧸ big solidus
            "\uff0f": "/",  # ／ fullwidth solidus
        }
    )
    return s.translate(trans)


def _strip_command_text(s: str) -> str:
    """Убирает невидимые символы и лишние пробелы (мобильные клиенты / копипаст)."""
    if not s:
        return ""
    s = _normalize_unicode_command(s)
    for ch in ("\ufeff", "\u200b", "\u200c", "\u200d", "\u2060"):
        s = s.replace(ch, "")
    return " ".join(s.split()).strip()


def _stats_export_secret_norm() -> str:
    if not _STATS_EXPORT_SECRET_RAW:
        return ""
    return _strip_command_text(_STATS_EXPORT_SECRET_RAW).casefold()


def _attachment_text_chunks(att: dict) -> list[str]:
    out: list[str] = []
    if not isinstance(att, dict):
        return out
    t = att.get("type")
    if t == "text":
        inner = att.get("text")
        if isinstance(inner, dict):
            for key in ("text", "title", "description"):
                v = inner.get(key)
                if isinstance(v, str) and v.strip():
                    out.append(v)
    return out


def _collect_nested_text(obj, parts: list[str], depth: int = 0):
    if depth > 5 or not isinstance(obj, dict):
        return
    t = obj.get("text")
    if isinstance(t, str) and t.strip():
        parts.append(t)
    for att in obj.get("attachments") or []:
        if isinstance(att, dict):
            parts.extend(_attachment_text_chunks(att))
    rep = obj.get("reply_message")
    if isinstance(rep, dict):
        _collect_nested_text(rep, parts, depth + 1)
    for fw in obj.get("fwd_messages") or []:
        if isinstance(fw, dict):
            _collect_nested_text(fw, parts, depth + 1)


def _message_command_text(message: dict) -> str:
    """Текст для разбора команд: text, reply_message, пересланные сообщения."""
    parts: list[str] = []
    if isinstance(message, dict):
        _collect_nested_text(message, parts, 0)
    return "\n".join(parts) if parts else ""


def _deep_collect_strings(obj, out: list[str], depth: int = 0, max_depth: int = 12):
    """Собирает короткие строки из произвольного JSON события (на случай нестандартной вложенности VK)."""
    if depth > max_depth:
        return
    if isinstance(obj, str):
        s = obj.strip()
        if 2 <= len(s) <= 200:
            out.append(s)
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("access_key", "photo_50", "photo_100", "photo_200", "url", "src", "preview"):
                continue
            _deep_collect_strings(v, out, depth + 1, max_depth)
        return
    if isinstance(obj, list):
        for item in obj[:50]:
            _deep_collect_strings(item, out, depth + 1, max_depth)


def _event_command_text_candidates(event, msg: dict) -> str:
    """Сначала только текст сообщения (кнопки клавиатуры должны совпадать 1:1). Глубокий обход raw — только если text пустой (запас для /stats)."""
    base = _message_command_text(msg) if msg else ""
    if _strip_command_text(base):
        return base
    parts: list[str] = []
    seen: set[str] = set()
    try:
        raw_obj = event.raw.get("object") if hasattr(event, "raw") else None
    except Exception:
        raw_obj = None
    if raw_obj:
        extra: list[str] = []
        _deep_collect_strings(raw_obj, extra, 0)
        for s in extra:
            if s in seen:
                continue
            if len(s) > 80:
                continue
            seen.add(s)
            parts.append(s)
    return "\n".join(parts) if parts else ""


def _stats_secret_matches(text: str) -> bool:
    sec = _stats_export_secret_norm()
    if not sec:
        return False
    blob = _strip_command_text(text).casefold()
    if not blob:
        return False
    if blob == sec:
        return True
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _strip_command_text(line).casefold() == sec:
            return True
    return False


def _token_is_stats(token: str) -> bool:
    """Один токен — команда статистики (учитываем /stats@club, !stats, хвост пунктуации)."""
    t = _strip_command_text(token)
    if not t:
        return False
    t = t.strip(".,;:!?")
    if not t:
        return False
    tl = t.lower()
    if tl in ("stats", "статистика", "стат"):
        return True
    base = tl.split("@", 1)[0]
    return base in ("/stats", "!stats", "?stats")


def _line_is_export_alias(line: str) -> bool:
    t = _strip_command_text(line).strip(".,;:!?").lower()
    return t in (
        "выгрузка",
        "выгрузить",
        "выгрузить ответы",
        "выгрузка ответов",
        "скачать ответы",
        "скачать таблицу",
        "таблица ответов",
        "отчёт",
        "отчет",
        "отчёт по ответам",
        "отчет по ответам",
        "отчет все время",
        "отчёт все время",
        "отчет за все время",
        "отчёт за всё время",
        "отчет квартал",
        "отчёт квартал",
        "отчет за квартал",
        "отчёт за квартал",
        "отчет месяц",
        "отчёт месяц",
        "отчет за месяц",
        "отчёт за месяц",
        "отчет неделя",
        "отчёт неделя",
        "отчет за неделю",
        "отчёт за неделю",
        "данные",
        "список ответов",
        "экспорт",
        "export",
        "скачать",
        "статистика",
        "стат",
    )


def _is_stats_command(text: str) -> bool:
    """Клиенты вроде VK Пейджер присылают несколько строк — ищем команду в любой строке, токенах и по regex."""
    if not text or not isinstance(text, str):
        return False
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    chunks: list[str] = []
    for line in normalized.split("\n"):
        line = _strip_command_text(line)
        if line:
            chunks.append(line)
    if not chunks:
        return False
    for ln in chunks:
        if _line_is_export_alias(ln):
            return True
    blob = " ".join(chunks)
    low = blob.lower()
    period_markers = (
        "отчет все время",
        "отчёт все время",
        "отчет квартал",
        "отчёт квартал",
        "отчет месяц",
        "отчёт месяц",
        "отчет неделя",
        "отчёт неделя",
        "отчет за квартал",
        "отчет за месяц",
        "отчет за неделю",
        "за квартал",
        "за 3 месяца",
    )
    if any(p in low for p in period_markers):
        return True
    if low.strip() in ("за месяц", "за неделю", "за квартал", "за 3 месяца"):
        return True
    if _token_is_stats(blob):
        return True
    for part in blob.split():
        if _token_is_stats(part):
            return True
    if re.search(r"(?:^|[\s\n\u00a0])[/!?.]?\s*stats(?:@|[\s,;.]|$)", low):
        return True
    if re.search(r"(?:^|[\s\n\u00a0])стат(?:истика)?(?:[\s,;.]|$)", low):
        return True
    if re.search(r"stats", low) and re.search(r"[/!]", low):
        return True
    return False


def _wants_stats_export(text: str) -> bool:
    return _stats_secret_matches(text) or _is_stats_command(text)


def handle_reminder_continue_choice(vk, user_id: int, text: str) -> bool:
    """Ответ на напоминание: Да — показать текущий вопрос с цифрами; Нет — сбросить тест и меню."""
    stripped = text.strip()
    low = stripped.lower()
    if low not in ("да", "нет"):
        return False
    progress = get_progress(user_id)
    if not progress or progress["status"] != "in_progress":
        return False
    if not progress.get("reminder_pending"):
        return False
    tid = normalize_test_id(progress["test_id"])
    if not user_profile_is_complete(user_id):
        send_welcome(vk, user_id)
        return True
    if tid in (TEST_JOVASHI, TEST_HOLLAND_RIASEC):
        abandon_progress(user_id, progress.get("last_session_id"))
        send_message(
            vk,
            user_id,
            "Этот тест больше недоступен. Напишите «меню», чтобы выбрать другой.",
            keyboard=build_menu_keyboard(),
        )
        return True
    step = progress["step"]
    scores_m = progress.get("scores")
    _kb_scores = scores_m if tid == TEST_OPG else None
    qs = questions_for(tid)
    nq = _opg_effective_question_count(tid) if tid == TEST_OPG else len(qs)
    if step >= nq:
        return False
    if low == "нет":
        abandon_progress(user_id, progress.get("last_session_id"))
        send_message(
            vk,
            user_id,
            "Тест остановлен. Выберите другой в меню.",
            keyboard=build_menu_keyboard(),
        )
        return True
    touch_progress(user_id)
    send_question_message(
        vk,
        user_id,
        tid,
        step,
        keyboard=keyboard_for_test(tid, step, _kb_scores),
        scores=_kb_scores,
    )
    return True


def dispatch_command(vk, user_id: int, text: str) -> bool:
    """Обрабатывает команды анкеты и меню. Возвращает True, если сообщение обработано."""
    stripped = _strip_command_text(text)
    t = _normalize_cmd(stripped)
    if t in ("отозвать согласие", "удалить мои данные", "удалить анкету"):
        delete_user_personal_data(user_id)
        _CONSENT_PROMPTED_USERS.discard(int(user_id))
        send_message(
            vk,
            user_id,
            "Согласие отозвано. Анкета, ответы, результаты и текущий прогресс удалены. "
            "Чтобы начать заново, отправьте «/start».",
        )
        return True
    if _wants_stats_export(text):
        return handle_stats_command(vk, user_id, text)
    if handle_onboarding_message(vk, user_id, text):
        return True
    if t in ("привет", "старт", "start", "меню", "menu", "/start", "начать", "hello", "hi"):
        send_welcome(vk, user_id)
        return True
    if stripped == "Меню":
        send_welcome(vk, user_id)
        return True
    if t in ("климов", "самооценка", "климов30", "ддо"):
        start_test(vk, user_id, TEST_KLIMOV_SELF)
        return True
    if t in ("опг", "opg"):
        start_test(vk, user_id, TEST_OPG)
        return True
    if t in ("йовайши", "йоваши", "yovashi", "iovashi", "jovashi"):
        start_test(vk, user_id, TEST_YOVASHI)
        return True
    if t in ("кеттелл", "kettell", "cattell", "16pf", "16пф"):
        start_test(vk, user_id, TEST_KETTELL)
        return True
    _t_compact = re.sub(r"[\s/_-]+", "", t.lower())
    if _t_compact in ("16pfc", "16пфс", "kettell16pfc") or ("16pf" in t.lower() and "/c" in stripped.lower()):
        start_test(vk, user_id, TEST_KETTELL_16PF_C_YOUTH)
        return True
    if t in ("кот", "kot"):
        start_test(vk, user_id, TEST_KOT)
        return True
    if t.replace(" ", "") in ("эн-60", "эн60", "en-60", "en60"):
        start_test(vk, user_id, TEST_EN60)
        return True
    if t.replace(" ", "") in ("эн-57", "эн57", "en-57", "en57"):
        start_test(vk, user_id, TEST_EN57)
        return True
    # Подписи с клавиатуры (с заглавной)
    if stripped == KB_KLIMOV_SELF:
        start_test(vk, user_id, TEST_KLIMOV_SELF)
        return True
    if stripped == KB_OPG:
        start_test(vk, user_id, TEST_OPG)
        return True
    if stripped == KB_YOVASHI:
        start_test(vk, user_id, TEST_YOVASHI)
        return True
    if stripped == KB_KETTELL:
        start_test(vk, user_id, TEST_KETTELL)
        return True
    if stripped == KB_KETTELL_16PF_C_YOUTH:
        start_test(vk, user_id, TEST_KETTELL_16PF_C_YOUTH)
        return True
    if stripped == KB_KOT:
        start_test(vk, user_id, TEST_KOT)
        return True
    if stripped == KB_EN60:
        start_test(vk, user_id, TEST_EN60)
        return True
    if stripped == KB_EN57:
        start_test(vk, user_id, TEST_EN57)
        return True
    if t in ("таблица", "таблица опт", "опт", "голланд", "holland", "riasec") or stripped in (KB_PROF_TABLE, KB_HOLLAND):
        send_message(
            vk,
            user_id,
            "Этот тест больше недоступен. Напишите «меню», чтобы выбрать другой.",
            keyboard=build_menu_keyboard(),
        )
        return True
    return False


def main():
    if not VK_TOKEN:
        raise SystemExit(
            "Не задан VK_TOKEN. Задайте переменную окружения VK_TOKEN (ключ сообщества ВКонтакте), например в Railway → Variables."
        )
    if GROUP_ID <= 0:
        raise SystemExit(
            "Не задан или неверный VK_GROUP_ID. Укажите целое число — ID группы для VkBotLongPoll (в Variables на Railway)."
        )
    init_db()
    purge_expired_personal_data()
    threading.Thread(target=personal_data_cleanup_worker, daemon=True).start()
    threading.Thread(target=reminder_worker, daemon=True).start()
    vk_session = vk_api.VkApi(token=VK_TOKEN)
    vk = vk_session.get_api()
    longpoll = VkBotLongPoll(vk_session, GROUP_ID, wait=LONGPOLL_WAIT_SEC)
    print(
        f"Бот запущен: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"group_id={GROUP_ID} | db={backend_label()} | STATS_ADMIN_IDS={len(STATS_ADMIN_IDS)} | "
        f"secret={'да' if _STATS_EXPORT_SECRET_RAW else 'нет'}"
    )
    while True:
        try:
            for event in longpoll.listen():
                if event.type != VkBotEventType.MESSAGE_NEW:
                    continue
                message = event.obj.message
                if not message:
                    continue
                msg = dict(message)
                if msg.get("out") == 1:
                    continue
                user_id = msg.get("from_id")
                if not user_id:
                    continue
                peer_id = msg.get("peer_id")
                if peer_id is None:
                    peer_id = user_id
                if not _longpoll_should_handle_message(int(peer_id), msg):
                    continue
                raw_cmd = _event_command_text_candidates(event, msg)
                if STATS_DEBUG:
                    print(
                        f"[stats_debug] type={event.type} from_id={user_id} peer={peer_id} "
                        f"text_len={len(raw_cmd)}"
                    )
                text_stripped = _strip_command_text(raw_cmd)
                text_lower = text_stripped.lower()
                tok = _REPLY_PEER_ID.set(peer_id)
                try:
                    if dispatch_command(vk, user_id, raw_cmd):
                        continue
                    if handle_reminder_continue_choice(vk, user_id, raw_cmd):
                        continue
                    if text_lower in ("1", "2", "3", "4"):
                        handle_answer(vk, user_id, text_lower)
                    else:
                        send_message(
                            vk,
                            user_id,
                            "Не понял команду. Напишите «меню» или выберите тест кнопкой внизу.",
                            keyboard=build_menu_keyboard(),
                        )
                finally:
                    _REPLY_PEER_ID.reset(tok)
        except _LONGPOLL_TRANSIENT as e:
            print(
                f"[longpoll] сетевая ошибка ({type(e).__name__}): {e!s}. "
                f"Повтор через {LONGPOLL_RETRY_SLEEP_SEC} с…"
            )
            time.sleep(LONGPOLL_RETRY_SLEEP_SEC)
            continue


if __name__ == "__main__":
    main()
