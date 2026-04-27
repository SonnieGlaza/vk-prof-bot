import contextvars
import io
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime

import openpyxl
import requests
import vk_api
from vk_api.bot_longpoll import CHAT_START_ID, VkBotLongPoll, VkBotEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.upload import VkUpload

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

STATS_DEBUG = (os.environ.get("STATS_DEBUG") or "").strip().lower() in ("1", "true", "yes")
# Секретная фраза (см. _stats_secret_matches после определения _strip_command_text)
_STATS_EXPORT_SECRET_RAW = (os.environ.get("STATS_EXPORT_SECRET") or "").strip()

# SQLite: на Railway смонтируй том (например /data) и задай SQLITE_PATH=/data/career_bot.db
_DB_DEFAULT = os.path.join(_BASE, "career_bot.db")
DB_PATH = (os.environ.get("SQLITE_PATH") or os.environ.get("DB_PATH") or _DB_DEFAULT).strip() or _DB_DEFAULT
OPG_PATH = os.path.join(_BASE, "opg_questions.json")
JOVASHI_PATH = os.path.join(_BASE, "jovashi_questions.json")
YOVASHI_PATH = os.path.join(_BASE, "yovashi_questions.json")
KETTELL_PATH = os.path.join(_BASE, "kettell_questions.json")
RAVEN_PATH = os.path.join(_BASE, "raven_questions.json")
EN60_PATH = os.path.join(_BASE, "en60_questions.json")
EN57_PATH = os.path.join(_BASE, "en57_questions.json")

REMINDER_CHECK_EVERY_SEC = 60
REMINDER_AFTER_INACTIVE_MIN = 20
REMINDER_REPEAT_MIN = 60

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


def _peer_id_for_send(fallback_from_id: int) -> int:
    p = _REPLY_PEER_ID.get()
    return p if p is not None else fallback_from_id

TEST_DDO = "ddo"
TEST_OPG = "opg"
TEST_JOVASHI = "jovashi"
TEST_YOVASHI = "yovashi"
TEST_KETTELL = "kettell"
TEST_RAVEN = "raven"
TEST_EN60 = "en60"
TEST_EN57 = "en57"
LEGACY_HOLLAND = "holland"

LABEL_OPG = "ОПГ (опросник прогрессивной готовности)"
LABEL_PROF_TABLE = (
    "Таблица для ориентировочного определения предпочтительности типа будущей профессии"
)
LABEL_KETTELL = "Кеттелл"
LABEL_RAVEN = "Равен"
LABEL_EN60 = "ЭН - 60"
LABEL_EN57 = "ЭН - 57"
LABEL_YOVASHI = "Йоваши (проф. склонности, модиф. Резапкиной)"

# Подписи на кнопках клавиатуры (лимит ВК)
KB_OPG = "ОПГ"
KB_PROF_TABLE = "Таблица (ОПТ проф.)"
KB_YOVASHI = "Йоваши"
KB_KETTELL = "Кеттелл"
KB_RAVEN = "Равен"
KB_EN60 = "ЭН - 60"
KB_EN57 = "ЭН - 57"

# --- ДДО (Е.А. Климов) ---
PROFESSION_TYPES = {
    "Ч-П": "Человек-Природа",
    "Ч-Т": "Человек-Техника",
    "Ч-Ч": "Человек-Человек",
    "Ч-З": "Человек-Знаковая система",
    "Ч-Х": "Человек-Художественный образ",
}

QUESTIONS_DDO = [
    {
        "q": "1. Какое занятие тебе нравится больше?",
        "options": {
            "1": ("Ухаживать за животными", {"Ч-П": 1}),
            "2": ("Обслуживать машины, приборы (следить, регулировать)", {"Ч-Т": 1}),
        },
    },
    {
        "q": "2. Что бы ты выбрал(а)?",
        "options": {
            "1": ("Помогать больным", {"Ч-Ч": 1}),
            "2": ("Составлять таблицы, схемы, программы для вычислительных машин", {"Ч-З": 1}),
        },
    },
    {
        "q": "3. Какая работа ближе?",
        "options": {
            "1": ("Следить за качеством книжных иллюстраций, плакатов, художественных открыток, грампластинок", {"Ч-Х": 1}),
            "2": ("Следить за состоянием, развитием растений", {"Ч-П": 1}),
        },
    },
    {
        "q": "4. Что интереснее?",
        "options": {
            "1": ("Обрабатывать материалы (дерево, ткань, металл, пластмассу и т.п.)", {"Ч-Т": 1}),
            "2": ("Доводить товары до потребителя, рекламировать, продавать", {"Ч-Ч": 1}),
        },
    },
    {
        "q": "5. Что предпочитаешь?",
        "options": {
            "1": ("Обсуждать научно-популярные книги, статьи", {"Ч-З": 1}),
            "2": ("Обсуждать художественные книги (или пьесы, концерты)", {"Ч-Х": 1}),
        },
    },
    {
        "q": "6. Что бы ты выбрал(а)?",
        "options": {
            "1": ("Выращивать молодняк (животных какой-либо породы)", {"Ч-П": 1}),
            "2": ("Тренировать товарищей (или младших) в выполнении каких-либо действий (трудовых, учебных, спортивных)", {"Ч-Ч": 1}),
        },
    },
    {
        "q": "7. Что больше нравится?",
        "options": {
            "1": ("Копировать рисунки, изображения (или настраивать музыкальные инструменты)", {"Ч-Х": 1}),
            "2": ("Управлять каким-либо грузовым (подъемным или транспортным) средством – подъемным краном, трактором, тепловозом и др.", {"Ч-Т": 1}),
        },
    },
    {
        "q": "8. Какое занятие выберешь?",
        "options": {
            "1": ("Сообщать, разъяснять людям нужные им сведения (в справочном бюро, на экскурсии и т.д.)", {"Ч-Ч": 1}),
            "2": ("Оформлять выставки, витрины (или участвовать в подготовке пьес, концертов)", {"Ч-Х": 1}),
        },
    },
    {
        "q": "9. Что ближе?",
        "options": {
            "1": ("Ремонтировать вещи, изделия (одежду, технику), жилище", {"Ч-Т": 1}),
            "2": ("Искать и исправлять ошибки в текстах, таблицах, рисунках", {"Ч-З": 1}),
        },
    },
    {
        "q": "10. Что интереснее?",
        "options": {
            "1": ("Лечить животных", {"Ч-П": 1}),
            "2": ("Выполнять вычисления, расчеты", {"Ч-З": 1}),
        },
    },
    {
        "q": "11. Что бы ты выбрал(а)?",
        "options": {
            "1": ("Выводить новые сорта растений", {"Ч-П": 1}),
            "2": ("Конструировать, проектировать новые виды промышленных изделий (машины, одежду, дома, продукты питания и т.п.)", {"Ч-Т": 1}),
        },
    },
    {
        "q": "12. Какая работа ближе?",
        "options": {
            "1": ("Разбирать споры, ссоры между людьми, убеждать, разъяснять, наказывать, поощрять", {"Ч-Ч": 1}),
            "2": ("Разбираться в чертежах, схемах, таблицах (проверять, уточнять, приводить в порядок)", {"Ч-З": 1}),
        },
    },
    {
        "q": "13. Что предпочитаешь?",
        "options": {
            "1": ("Наблюдать, изучать работу кружков художественной самодеятельности", {"Ч-Х": 1}),
            "2": ("Наблюдать, изучать жизнь микробов", {"Ч-П": 1}),
        },
    },
    {
        "q": "14. Что интереснее?",
        "options": {
            "1": ("Обслуживать, налаживать медицинские приборы, аппараты", {"Ч-Т": 1}),
            "2": ("Оказывать людям медицинскую помощь при ранениях, ушибах, ожогах и т.п.", {"Ч-Ч": 1}),
        },
    },
    {
        "q": "15. Что бы ты выбрал(а)?",
        "options": {
            "1": ("Составлять точные описания-отчеты о наблюдаемых явлениях, событиях, измеряемых объектах и др.", {"Ч-З": 1}),
            "2": ("Художественно описывать, изображать события (наблюдаемые и представляемые)", {"Ч-Х": 1}),
        },
    },
    {
        "q": "16. Что ближе?",
        "options": {
            "1": ("Делать лабораторные анализы в больнице", {"Ч-П": 1}),
            "2": ("Принимать, осматривать больных, беседовать с ними, назначать лечение", {"Ч-Ч": 1}),
        },
    },
    {
        "q": "17. Что больше нравится?",
        "options": {
            "1": ("Красить или расписывать стены помещений, поверхность изделий", {"Ч-Х": 1}),
            "2": ("Осуществлять монтаж или сборку машин, приборов", {"Ч-Т": 1}),
        },
    },
    {
        "q": "18. Какое занятие выберешь?",
        "options": {
            "1": ("Организовывать культпоходы сверстников или младших в театры, музеи, экскурсии, туристические походы и т.п.", {"Ч-Ч": 1}),
            "2": ("Играть на сцене, принимать участие в концертах", {"Ч-Х": 1}),
        },
    },
    {
        "q": "19. Что ближе?",
        "options": {
            "1": ("Изготовлять по чертежам детали, изделия (машины, одежду), строить здания", {"Ч-Т": 1}),
            "2": ("Заниматься черчением, копировать чертежи, карты", {"Ч-З": 1}),
        },
    },
    {
        "q": "20. Что интереснее?",
        "options": {
            "1": ("Вести борьбу с болезнями растений, с вредителями леса, сада", {"Ч-П": 1}),
            "2": ("Работать на клавишных машинах (пишущей машинке, телетайпе, наборной машине и др.)", {"Ч-З": 1}),
        },
    },
]

CAREER_HINTS_DDO = {
    "Ч-П": "🌿 Человек-Природа\n\nТебе подходят профессии, связанные с природой: биолог, агроном, ветеринар, эколог, геолог, лесник, флорист.\n\nРекомендация: изучай биологию, химию, экологию.",
    "Ч-Т": "🔧 Человек-Техника\n\nТебе подходят технические профессии: инженер, программист, механик, электрик, строитель, технолог.\n\nРекомендация: развивайся в математике, физике, информатике.",
    "Ч-Ч": "👥 Человек-Человек\n\nТебе подходят социальные профессии: врач, учитель, психолог, менеджер, юрист, журналист.\n\nРекомендация: развивай коммуникативные навыки, изучай психологию.",
    "Ч-З": "📊 Человек-Знаковая система\n\nТебе подходят профессии, связанные с обработкой информации: бухгалтер, экономист, программист, переводчик, аналитик.\n\nРекомендация: изучай математику, языки, программирование.",
    "Ч-Х": "🎨 Человек-Художественный образ\n\nТебе подходят творческие профессии: дизайнер, художник, музыкант, актер, писатель, архитектор.\n\nРекомендация: развивай творческие способности, изучай искусство.",
}

def _load_questions(path: str):
    with open(path, encoding="utf-8") as _f:
        return json.load(_f)


# --- ОПГ (опросник прогрессивной готовности): 24 вопроса, 4 варианта ---
OPG_DIMENSIONS = {
    "ПОЗ": "Познавательная готовность (знания о профессиях)",
    "ЭМО": "Эмоциональная готовность (переживания, мотивация)",
    "ДЕЯ": "Деятельностная готовность (действия, план, практика)",
    "КОМ": "Коммуникативная готовность (разговоры, поддержка)",
}

QUESTIONS_OPG = _load_questions(OPG_PATH)

CAREER_HINTS_OPG = {
    "ПОЗ": "📚 Познавательная готовность — собирай факты: профессии, требования, вузы, рынок труда; консультируйся с педагогом.",
    "ЭМО": "💚 Эмоциональная готовность — нормализуй тревогу, опирайся на ценности; при сильном стрессе обратись к школьному психологу.",
    "ДЕЯ": "🎯 Деятельностная готовность — малые шаги: профориентация, проекты, практика; фиксируй цели на неделю/месяц.",
    "КОМ": "🤝 Коммуникативная готовность — обсуждай планы с близкими и специалистами; учись формулировать запросы о помощи.",
}

# --- Таблица ОПТ: вопросы из JSON (модификация Резапкиной) ---
QUESTIONS_JOVASHI = _load_questions(JOVASHI_PATH)

# --- Йоваши: отдельная формулировка вопросов, та же логика подсчёта сфер ---
QUESTIONS_YOVASHI = _load_questions(YOVASHI_PATH)

QUESTIONS_KETTELL = _load_questions(KETTELL_PATH)
KETTELL_TRAITS = {
    "ПР": "Прагматичность / ориентация на факты и порядок",
    "ЭМ": "Эмоциональная чувствительность",
    "КО": "Самоконтроль и выдержка",
    "ОБ": "Общительность и контактность",
}
CAREER_HINTS_KETTELL = {
    "ПР": "📐 Прагматичность — сильные стороны в анализе, планировании, точных задачах. Подумай об инженерии, IT, экономике, управлении качеством.",
    "ЭМ": "💭 Эмоциональная чувствительность — важны поддержка, ритм отдыха и ясные рамки. Подойдут гуманитарные и творческие направления, психология, медицина (с учётом нагрузки).",
    "КО": "⚖️ Самоконтроль — плюс для дисциплинированных профессий: юриспруденция, медицина, авиация, финансы.",
    "ОБ": "🗣 Общительность — продажи, обучение, HR, журналистика, event-менеджмент.",
}

QUESTIONS_RAVEN = _load_questions(RAVEN_PATH)

QUESTIONS_EN60 = _load_questions(EN60_PATH)
QUESTIONS_EN57 = _load_questions(EN57_PATH)
EN_LABELS = {"E": "Экстраверсия", "N": "Нейротизм / эмоциональная лабильность"}

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

WELCOME_TEXT = (
    "Привет! Я помогу пройти короткие опросники для профориентации и самопознания. "
    "Ответы анонимны на стороне бота; будь честным(ой) — так результат полезнее.\n\n"
    "Доступные тесты:\n"
    "• ДДО (дифференциально-диагностический опросник Климова) — 20 пар занятий, выбери одно; "
    "покажет склонность к типам «человек–природа», «человек–техника» и др.\n"
    "• ОПГ (опросник прогрессивной готовности) — 24 вопроса, 4 варианта; оценивает готовность к выбору профессии "
    "(знания, эмоции, действия, общение).\n"
    "• ОПТ (Таблица для ориентировочного определения предпочтительности типа будущей профессии) — 24 вопроса, "
    "3 варианта; сферы интересов: люди, техника, искусство и др.\n"
    "• Йоваши (проф. склонности, модиф. Резапкиной) — 24 вопроса, 3 варианта; выявление преобладающих склонностей "
    "человека к определённым типам профессиональной деятельности.\n"
    "• Кеттелл — 18 вопросов, 3 варианта; методика Кеттелла — многофакторный опросник личности\n"
    "• Равен — 12 задач на логику; Прогрессивные матрицы Равена оценивают невербальный интеллект\n"
    "• ЭН - 60 — 30 утверждений «да/нет»; Опросник Айзенка (шкалы E — экстраверсия, N — нейротизм); "
    "удобнее для детей и подростков.\n"
    "• ЭН - 57 — 24 утверждения «да/нет»; Опросник Айзенка (шкалы E — экстраверсия, N — нейротизм); "
    "формат ориентирован на взрослых.\n\n"
    "Можно начать тест кнопкой внизу или командой в чат: ддо, опг, таблица (или опт), йоваши, кеттелл, равен, "
    "эн-60, эн-57. Слово «меню» или «привет» снова покажет это сообщение.\n\n"
    "Для админа: скачать все ответы в Excel — напиши одно слово «выгрузка» или «/stats». "
    "Нужно указать твой id ВК в переменной STATS_ADMIN_IDS на сервере бота (Railway)."
)


def normalize_test_id(test_id: str | None) -> str:
    if not test_id:
        return TEST_DDO
    if test_id == LEGACY_HOLLAND:
        return TEST_OPG
    return test_id


def questions_for(test_id: str):
    tid = normalize_test_id(test_id)
    if tid == TEST_DDO:
        return QUESTIONS_DDO
    if tid == TEST_OPG:
        return QUESTIONS_OPG
    if tid == TEST_JOVASHI:
        return QUESTIONS_JOVASHI
    if tid == TEST_YOVASHI:
        return QUESTIONS_YOVASHI
    if tid == TEST_KETTELL:
        return QUESTIONS_KETTELL
    if tid == TEST_RAVEN:
        return QUESTIONS_RAVEN
    if tid == TEST_EN60:
        return QUESTIONS_EN60
    if tid == TEST_EN57:
        return QUESTIONS_EN57
    return QUESTIONS_DDO


def empty_scores(test_id: str) -> dict:
    tid = normalize_test_id(test_id)
    if tid == TEST_DDO:
        return {k: 0 for k in PROFESSION_TYPES}
    if tid == TEST_OPG:
        return {k: 0 for k in OPG_DIMENSIONS}
    if tid in (TEST_JOVASHI, TEST_YOVASHI):
        return {k: 0 for k in JOVASHI_SPHERES}
    if tid == TEST_KETTELL:
        return {k: 0 for k in KETTELL_TRAITS}
    if tid == TEST_RAVEN:
        return {"LOGIC": 0}
    if tid in (TEST_EN60, TEST_EN57):
        return {k: 0 for k in EN_LABELS}
    return {}


def db_connect():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def _ensure_column(conn, table: str, column: str, ddl_suffix: str):
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    names = {row[1] for row in cur.fetchall()}
    if column not in names:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_suffix}")


def init_db():
    with db_connect() as conn:
        cur = conn.cursor()
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
                test_id TEXT NOT NULL DEFAULT 'ddo',
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
                test_id TEXT NOT NULL DEFAULT 'ddo'
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
        _ensure_column(conn, "user_progress", "test_id", "TEXT NOT NULL DEFAULT 'ddo'")
        _ensure_column(conn, "user_progress", "reminder_pending", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "user_progress", "last_session_id", "INTEGER")
        _ensure_column(conn, "test_results", "test_id", "TEXT NOT NULL DEFAULT 'ddo'")
        _ensure_column(conn, "test_results", "best_type", "TEXT NOT NULL DEFAULT ''")
        cur.execute(
            "UPDATE user_progress SET test_id=? WHERE test_id=?",
            (TEST_OPG, LEGACY_HOLLAND),
        )
        conn.commit()


def now_ts():
    return int(time.time())


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
        tid = normalize_test_id(test_id or TEST_DDO)
        rp = int(reminder_pending or 0)
        lsid = int(last_session_id) if last_session_id is not None else None
        if tid == TEST_OPG and scores and not set(scores.keys()) <= set(OPG_DIMENSIONS):
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
        cur.execute(
            """
            INSERT INTO test_sessions (user_id, test_id, started_at, status)
            VALUES (?, ?, ?, 'in_progress')
            """,
            (user_id, test_id, ts),
        )
        conn.commit()
        return int(cur.lastrowid)


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


def answer_log_row_count() -> int:
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM answer_log")
        return int(cur.fetchone()[0])


def _test_title_for_export(test_id: str) -> str:
    return {
        TEST_DDO: "ДДО (Климов)",
        TEST_OPG: LABEL_OPG,
        TEST_JOVASHI: LABEL_PROF_TABLE,
        TEST_YOVASHI: LABEL_YOVASHI,
        TEST_KETTELL: LABEL_KETTELL,
        TEST_RAVEN: LABEL_RAVEN,
        TEST_EN60: LABEL_EN60,
        TEST_EN57: LABEL_EN57,
    }.get(test_id, test_id)


def build_stats_excel_bytes() -> bytes:
    headers = [
        "session_id",
        "user_id",
        "test_id",
        "test_name",
        "session_status",
        "session_started_at",
        "session_completed_at",
        "step_index",
        "answer_key",
        "question",
        "answer_text",
        "weights_json",
        "answer_recorded_at",
    ]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "answers"
    ws.append(headers)
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                a.session_id,
                a.user_id,
                a.test_id,
                a.step_index,
                a.answer_key,
                a.question_text,
                a.answer_label,
                a.weights_json,
                a.created_at,
                s.status,
                s.started_at,
                s.completed_at
            FROM answer_log a
            JOIN test_sessions s ON s.id = a.session_id
            ORDER BY a.id
            """
        )
        for row in cur.fetchall():
            (
                sid,
                uid,
                tid,
                step_i,
                akey,
                qtext,
                alabel,
                wjson,
                created,
                sess_status,
                started,
                completed,
            ) = row
            test_name = _test_title_for_export(tid)
            ws.append(
                [
                    sid,
                    uid,
                    tid,
                    test_name,
                    sess_status,
                    datetime.utcfromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S") if started else "",
                    datetime.utcfromtimestamp(completed).strftime("%Y-%m-%d %H:%M:%S") if completed else "",
                    step_i,
                    akey,
                    qtext,
                    alabel,
                    wjson,
                    datetime.utcfromtimestamp(created).strftime("%Y-%m-%d %H:%M:%S") if created else "",
                ]
            )
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def send_stats_export(vk, user_id: int):
    n_rows = answer_log_row_count()
    data = build_stats_excel_bytes()
    fname = f"stats_answers_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    bio = io.BytesIO(data)
    bio.name = fname
    upload = VkUpload(vk)
    peer = _peer_id_for_send(user_id)
    saved = upload.document_message(bio, title=fname, peer_id=peer)
    if isinstance(saved, list) and saved:
        saved = saved[0]
    if isinstance(saved, dict) and "doc" in saved:
        att = saved["doc"]
    else:
        att = saved
    att_str = f"doc{att['owner_id']}_{att['id']}"
    note = (
        f"Excel с ответами участников. Строк с ответами в базе: {n_rows}. "
        "Если 0 — данные пишутся только с версии бота с логированием; пройдите тест заново после обновления."
    )
    vk.messages.send(
        peer_id=peer,
        random_id=0,
        message=note,
        attachment=att_str,
    )


def handle_stats_command(vk, user_id: int) -> bool:
    if not is_stats_admin(vk, user_id):
        send_message(
            vk,
            user_id,
            "Выгрузка только для администраторов.\n\n"
            "Сделай так: открой Railway → твой сервис → Variables → добавь STATS_ADMIN_IDS = твой числовой id ВК "
            "(только цифры, без пробелов). Id смотри в ссылке на страницу vk.com/id… или через настройки. "
            "Сохрани и Redeploy. Потом напиши боту одно слово: выгрузка",
        )
        return True
    try:
        send_stats_export(vk, user_id)
    except Exception as e:
        print(f"[handle_stats_command] {e}")
        send_message(
            vk,
            user_id,
            f"Не удалось отправить файл. Проверь права токена (сообщения + документы). Ошибка: {e}",
        )
    return True


def save_result(user_id: int, test_id: str, scores: dict, top3: list):
    """Сохраняет результат; учитывает старые БД с колонкой best_area вместо/рядом с best_type."""
    best_type = top3[0][0] if top3 else "Не определено"
    ts = now_ts()
    scores_json = json.dumps(scores, ensure_ascii=False)
    top3_json = json.dumps(top3, ensure_ascii=False)
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(test_results)")
        col_names = {row[1] for row in cur.fetchall()}
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
              AND last_activity_at <= ?
              AND (reminded_at IS NULL OR reminded_at <= ?)
            """,
            (inactive_threshold, repeat_threshold),
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


def build_reminder_continue_keyboard():
    kb = VkKeyboard(one_time=False, inline=True)
    kb.add_button("Да", color=VkKeyboardColor.POSITIVE)
    kb.add_button("Нет", color=VkKeyboardColor.NEGATIVE)
    return kb.get_keyboard()


def build_menu_keyboard():
    kb = VkKeyboard(one_time=False, inline=False)
    kb.add_button("ДДО", color=VkKeyboardColor.POSITIVE)
    kb.add_button(KB_OPG, color=VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button(KB_PROF_TABLE, color=VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button(KB_YOVASHI, color=VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button(KB_KETTELL, color=VkKeyboardColor.POSITIVE)
    kb.add_button(KB_RAVEN, color=VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button(KB_EN60, color=VkKeyboardColor.POSITIVE)
    kb.add_button(KB_EN57, color=VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button("Меню", color=VkKeyboardColor.SECONDARY)
    return kb.get_keyboard()


def keyboard_for_test(test_id: str, step: int = 0):
    tid = normalize_test_id(test_id)
    qs = questions_for(tid)
    nopts = len(qs[step]["options"]) if step < len(qs) else 2
    if nopts >= 4:
        return build_answer_keyboard_quad()
    if nopts == 3:
        return build_answer_keyboard_jovashi()
    return build_answer_keyboard_binary()


def send_message(vk, user_id, message, keyboard=None):
    peer = _peer_id_for_send(user_id)
    vk.messages.send(peer_id=peer, random_id=0, message=message, keyboard=keyboard)


def render_question(test_id: str, step: int) -> str:
    tid = normalize_test_id(test_id)
    item = questions_for(tid)[step]
    lines = [item["q"]]
    keys = sorted(item["options"].keys(), key=lambda x: int(x))
    for key in keys:
        opt = item["options"][key]
        label = opt[0] if isinstance(opt[0], str) else str(opt[0])
        lines.append(f"{key}) {label}")
    if len(keys) == 4:
        lines.append("\nВыбери ответ кнопкой 1, 2, 3 или 4.")
    elif len(keys) == 3:
        lines.append("\nВыбери ответ кнопкой 1, 2 или 3.")
    else:
        lines.append("\nВыбери ответ кнопкой 1 или 2.")
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
        TEST_DDO: "ДДО (Климов)",
        TEST_OPG: LABEL_OPG,
        TEST_JOVASHI: LABEL_PROF_TABLE,
        TEST_YOVASHI: LABEL_YOVASHI,
        TEST_KETTELL: LABEL_KETTELL,
        TEST_RAVEN: LABEL_RAVEN,
        TEST_EN60: LABEL_EN60,
        TEST_EN57: LABEL_EN57,
    }.get(tid, "тест")


def finish_test(vk, user_id: int, test_id: str, scores: dict):
    tid = normalize_test_id(test_id)
    prog = get_progress(user_id)
    sid = prog.get("last_session_id") if prog else None
    if sid:
        complete_test_session(sid, user_id, scores, "completed")
    sorted_types = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    if not sorted_types:
        send_message(
            vk,
            user_id,
            "Не удалось посчитать результат. Открой меню и начни тест заново.",
            keyboard=build_menu_keyboard(),
        )
        return
    top3 = sorted_types[:3]
    best_key = top3[0][0]

    if tid == TEST_DDO:
        title = "📊 Твой результат по тесту ДДО (топ-3):"
        lines = [title]
        for i, (ptype, points) in enumerate(top3, 1):
            lines.append(f"{i}. {PROFESSION_TYPES[ptype]} — {points} баллов")
        lines.append(f"\n{CAREER_HINTS_DDO.get(best_key, 'Выбирай направление, которое откликается сильнее.')}")
        lines.append("\nХочешь пройти снова — выбери тест кнопкой или командой.")
    elif tid == TEST_OPG:
        lines = [f"📊 Твой результат по «{LABEL_OPG}» (топ-3):"]
        for i, (code, points) in enumerate(top3, 1):
            lines.append(f"{i}. {OPG_DIMENSIONS[code]} — {points} баллов")
        lines.append(f"\n{CAREER_HINTS_OPG.get(best_key, '')}")
        lines.append("\nРазвивай все четыре стороны готовности — они поддерживают друг друга.")
    elif tid == TEST_JOVASHI:
        lines = [f"📊 Твой результат по «{LABEL_PROF_TABLE}» (топ-3 сферы):"]
        for i, (key, points) in enumerate(top3, 1):
            interp = _interpret_jovashi(points)
            lines.append(f"{i}. {JOVASHI_SPHERES[key]} — {points} баллов ({interp})")
        lines.append(f"\n{CAREER_HINTS_JOVASHI.get(best_key, '')}")
        lines.append("\nСравни несколько сильных сфер и подумай, какие профессии их объединяют.")
    elif tid == TEST_YOVASHI:
        lines = [f"📊 Твой результат по «{LABEL_YOVASHI}» (топ-3 сферы):"]
        for i, (key, points) in enumerate(top3, 1):
            interp = _interpret_jovashi(points)
            lines.append(f"{i}. {JOVASHI_SPHERES[key]} — {points} баллов ({interp})")
        lines.append(f"\n{CAREER_HINTS_JOVASHI.get(best_key, '')}")
        lines.append("\nСравни несколько сильных сфер и подумай, какие профессии их объединяют.")
    elif tid == TEST_KETTELL:
        lines = [f"📊 Твой результат по «{LABEL_KETTELL}» (топ-3 черты):"]
        for i, (key, points) in enumerate(top3, 1):
            lines.append(f"{i}. {KETTELL_TRAITS[key]} — {points} баллов")
        lines.append(f"\n{CAREER_HINTS_KETTELL.get(best_key, '')}")
        lines.append("\nЭто упрощённый учебный срез, не замена полноценной методики Кеттелла.")
    elif tid == TEST_RAVEN:
        total = len(QUESTIONS_RAVEN)
        correct = scores.get("LOGIC", 0)
        pct = round(100 * correct / total) if total else 0
        lines = [
            f"📊 Результат «{LABEL_RAVEN}»: {correct} из {total} верных ({pct}%).",
            "",
            "Задачи учебные (аналог по формату), без оригинальных таблиц Равена. "
            "Используй как тренировку внимательности и логики.",
        ]
        if pct >= 75:
            lines.append("\nСильный результат — продолжай решать подобные задачи и разбирать ошибки.")
        elif pct >= 50:
            lines.append("\nСредний уровень — полезно тренировать ряды, условия и аккуратность в подсчётах.")
        else:
            lines.append("\nЕсть куда расти — разбирай каждую задачу и ищи закономерность.")
    elif tid in (TEST_EN60, TEST_EN57):
        e = scores.get("E", 0)
        n = scores.get("N", 0)
        max_e = (len(QUESTIONS_EN60) + 1) // 2 if tid == TEST_EN60 else (len(QUESTIONS_EN57) + 1) // 2
        max_n = len(QUESTIONS_EN60) // 2 if tid == TEST_EN60 else len(QUESTIONS_EN57) // 2
        label = LABEL_EN60 if tid == TEST_EN60 else LABEL_EN57
        age_note = (
            "Удобнее для детей и подростков."
            if tid == TEST_EN60
            else "Формат ориентирован на взрослых."
        )
        lines = [
            f"📊 Результат «{label}» (ориентир, не клиническая диагностика):",
            f"• {EN_LABELS['E']}: {e} из {max_e} по ответам «да» к соответствующим пунктам.",
            f"• {EN_LABELS['N']}: {n} из {max_n} по ответам «да» к соответствующим пунктам.",
            "",
            age_note,
            "",
            "Больше «да» по экстраверсии — склонность к активности и контактам; "
            "больше «да» по нейротизму — сильнее реакция на стресс и переживания. "
            "Обсуди сомнения со специалистом.",
        ]
    else:
        lines = ["Результат сохранён. Открой меню и выбери другой тест."]

    send_message(vk, user_id, "\n".join(lines), keyboard=build_menu_keyboard())
    complete_progress(user_id)
    save_result(user_id, tid, scores, top3)


def start_test(vk, user_id: int, test_id: str):
    tid = normalize_test_id(test_id)
    qs = questions_for(tid)
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
    intro = f"«{_label_for_test(tid)}» запущен.\nВопросов: {len(qs)}."
    send_message(vk, user_id, f"{intro}\n\n{render_question(tid, 0)}", keyboard=keyboard_for_test(tid, 0))


def send_welcome(vk, user_id: int):
    send_message(vk, user_id, WELCOME_TEXT, keyboard=build_menu_keyboard())


def _option_weights(option_val):
    """Возвращает словарь весов для варианта ответа (кортеж/список: текст, веса)."""
    if isinstance(option_val, (list, tuple)) and len(option_val) >= 2 and isinstance(option_val[1], dict):
        return option_val[1]
    raise ValueError("Некорректный формат варианта ответа")


def handle_answer(vk, user_id: int, text: str):
    progress = get_progress(user_id)
    if not progress or progress["status"] != "in_progress":
        send_message(
            vk,
            user_id,
            "Сейчас нет активного теста. Напиши «меню» или выбери тест кнопкой.",
            keyboard=build_menu_keyboard(),
        )
        return
    test_id = progress["test_id"]
    tid = normalize_test_id(test_id)
    touch_progress(user_id)
    step = progress["step"]
    qs = questions_for(tid)
    if step >= len(qs):
        finish_test(vk, user_id, tid, progress["scores"])
        return
    valid = set(qs[step]["options"].keys())
    if text not in valid:
        send_message(
            vk,
            user_id,
            f"Пожалуйста, используй кнопки {' / '.join(sorted(valid, key=lambda x: int(x)))}.",
            keyboard=keyboard_for_test(tid, step),
        )
        send_message(vk, user_id, render_question(tid, step), keyboard=keyboard_for_test(tid, step))
        return
    scores = progress["scores"]
    opt_val = qs[step]["options"][text]
    weights = _option_weights(opt_val)
    q_text = qs[step]["q"]
    ans_label = opt_val[0] if isinstance(opt_val[0], str) else str(opt_val[0])
    sid = progress.get("last_session_id")
    if sid:
        log_answer_row(sid, user_id, tid, step, text, q_text, ans_label, weights)
    for ptype, value in weights.items():
        scores[ptype] = scores.get(ptype, 0) + value
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
        send_message(vk, user_id, render_question(tid, step), keyboard=keyboard_for_test(tid, step))
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


def reminder_worker():
    vk_session = vk_api.VkApi(token=VK_TOKEN)
    vk = vk_session.get_api()
    while True:
        try:
            for uid in users_for_reminder():
                progress = get_progress(uid)
                if not progress or progress["status"] != "in_progress":
                    continue
                test_id = progress["test_id"]
                tid = normalize_test_id(test_id)
                qs = questions_for(tid)
                total = len(qs)
                step_display = progress["step"] + 1
                st = progress["step"]
                send_message(
                    vk,
                    uid,
                    f"⏰ Напоминание: ты на вопросе {step_display} из {total}.\n"
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


def _event_command_text_candidates(event) -> str:
    """Объединяет текст из message и коротких строк из сырого object (если клиент положил текст не в message.text)."""
    parts: list[str] = []
    seen: set[str] = set()
    msg = getattr(event, "message", None)
    if msg is None and hasattr(event, "obj"):
        o = event.obj
        msg = o.get("message") if isinstance(o, dict) else getattr(o, "message", None)
    if msg is not None:
        try:
            md = dict(msg) if not isinstance(msg, dict) else msg
        except Exception:
            md = {}
        if md:
            t = _message_command_text(md)
            if t.strip():
                parts.append(t)
                seen.add(t.strip())
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
    if _token_is_stats(blob):
        return True
    for part in blob.split():
        if _token_is_stats(part):
            return True
    low = blob.lower()
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
    tid = progress["test_id"]
    step = progress["step"]
    qs = questions_for(tid)
    if step >= len(qs):
        return False
    if low == "нет":
        abandon_progress(user_id, progress.get("last_session_id"))
        send_message(
            vk,
            user_id,
            "Тест остановлен. Можно выбрать другую методику.",
            keyboard=build_menu_keyboard(),
        )
        return True
    touch_progress(user_id)
    send_message(
        vk,
        user_id,
        render_question(tid, step),
        keyboard=keyboard_for_test(tid, step),
    )
    return True


def dispatch_command(vk, user_id: int, text: str) -> bool:
    """Обрабатывает команды меню. Возвращает True, если сообщение обработано."""
    stripped = _strip_command_text(text)
    t = _normalize_cmd(stripped)
    if _wants_stats_export(text):
        return handle_stats_command(vk, user_id)
    if t in ("привет", "старт", "start", "меню", "menu", "/start", "начать", "hello", "hi"):
        send_welcome(vk, user_id)
        return True
    if stripped == "Меню":
        send_welcome(vk, user_id)
        return True
    if t in ("ддо",):
        start_test(vk, user_id, TEST_DDO)
        return True
    if t in ("опг", "opg"):
        start_test(vk, user_id, TEST_OPG)
        return True
    if t in ("таблица", "таблица опт", "опт"):
        start_test(vk, user_id, TEST_JOVASHI)
        return True
    if t in ("йоваши", "yovashi", "iovashi", "jovashi"):
        start_test(vk, user_id, TEST_YOVASHI)
        return True
    if t in ("кеттелл", "kettell", "cattell"):
        start_test(vk, user_id, TEST_KETTELL)
        return True
    if t in ("равен", "raven"):
        start_test(vk, user_id, TEST_RAVEN)
        return True
    if t.replace(" ", "") in ("эн-60", "эн60", "en-60", "en60"):
        start_test(vk, user_id, TEST_EN60)
        return True
    if t.replace(" ", "") in ("эн-57", "эн57", "en-57", "en57"):
        start_test(vk, user_id, TEST_EN57)
        return True
    # Подписи с клавиатуры (с заглавной)
    if stripped == "ДДО":
        start_test(vk, user_id, TEST_DDO)
        return True
    if stripped == KB_OPG:
        start_test(vk, user_id, TEST_OPG)
        return True
    if stripped == KB_PROF_TABLE:
        start_test(vk, user_id, TEST_JOVASHI)
        return True
    if stripped == KB_YOVASHI:
        start_test(vk, user_id, TEST_YOVASHI)
        return True
    if stripped == KB_KETTELL:
        start_test(vk, user_id, TEST_KETTELL)
        return True
    if stripped == KB_RAVEN:
        start_test(vk, user_id, TEST_RAVEN)
        return True
    if stripped == KB_EN60:
        start_test(vk, user_id, TEST_EN60)
        return True
    if stripped == KB_EN57:
        start_test(vk, user_id, TEST_EN57)
        return True
    return False


def main():
    if not VK_TOKEN:
        raise SystemExit(
            "Не задан VK_TOKEN. Задай переменную окружения VK_TOKEN (ключ сообщества ВКонтакте), например в Railway → Variables."
        )
    if GROUP_ID <= 0:
        raise SystemExit(
            "Не задан или неверный VK_GROUP_ID. Укажи целое число — ID группы для VkBotLongPoll (в Variables на Railway)."
        )
    init_db()
    threading.Thread(target=reminder_worker, daemon=True).start()
    vk_session = vk_api.VkApi(token=VK_TOKEN)
    vk = vk_session.get_api()
    longpoll = VkBotLongPoll(vk_session, GROUP_ID, wait=LONGPOLL_WAIT_SEC)
    print(
        f"Бот запущен: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"group_id={GROUP_ID} | STATS_ADMIN_IDS={len(STATS_ADMIN_IDS)} | "
        f"secret={'да' if _STATS_EXPORT_SECRET_RAW else 'нет'}"
    )
    while True:
        try:
            for event in longpoll.listen():
                if event.type not in (
                    VkBotEventType.MESSAGE_NEW,
                    VkBotEventType.MESSAGE_REPLY,
                    VkBotEventType.MESSAGE_EDIT,
                ):
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
                raw_cmd = _event_command_text_candidates(event)
                if not _strip_command_text(raw_cmd):
                    raw_cmd = _message_command_text(msg)
                if STATS_DEBUG:
                    preview = (raw_cmd[:120] + "…") if len(raw_cmd) > 120 else raw_cmd
                    preview = preview.replace("\n", "\\n")
                    print(
                        f"[stats_debug] type={event.type} from_id={user_id} peer={peer_id} "
                        f"text_len={len(raw_cmd)} preview={preview!r}"
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
                            "Не понял команду. Напиши «меню» или выбери тест кнопкой внизу.",
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
