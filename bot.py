import json
import os
import sqlite3
import threading
import time
from datetime import datetime

import requests
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor

_BASE = os.path.dirname(os.path.abspath(__file__))

VK_TOKEN = (os.environ.get("VK_TOKEN") or "").strip()
_GROUP_ID_RAW = (os.environ.get("VK_GROUP_ID") or "").strip()
GROUP_ID = 0
if _GROUP_ID_RAW:
    try:
        GROUP_ID = int(_GROUP_ID_RAW)
    except ValueError:
        GROUP_ID = 0

# SQLite: на Railway смонтируй том (например /data) и задай SQLITE_PATH=/data/career_bot.db
_DB_DEFAULT = os.path.join(_BASE, "career_bot.db")
DB_PATH = (os.environ.get("SQLITE_PATH") or os.environ.get("DB_PATH") or _DB_DEFAULT).strip() or _DB_DEFAULT
JOVASHI_PATH = os.path.join(_BASE, "jovashi_questions.json")

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

TEST_DDO = "ddo"
# Внутренние id сохранены для совместимости с уже сохранёнными сессиями в БД
TEST_HOLLAND = "holland"
TEST_JOVASHI = "jovashi"
LABEL_OPG = "ОПГ (опросник прогресс. готовности)"
LABEL_PROF_TABLE = (
    "Таблица для ориентиаровочн. определ. предпочтительности типа будущей профессии"
)
LABEL_KETTELL = "Кеттелл"
LABEL_RAVEN = "Равен"
LABEL_EN60 = "ЭН - 60"
LABEL_EN57 = "ЭН - 57"

# Подписи на кнопках клавиатуры (лимит ВК)
KB_OPG = "ОПГ"
KB_PROF_TABLE = "Таблица (ОПТ проф.)"
KB_KETTELL = "Кеттелл"
KB_RAVEN = "Равен"
KB_EN60 = "ЭН - 60"
KB_EN57 = "ЭН - 57"

STUB_TESTS = frozenset({TEST_KETTELL, TEST_RAVEN, TEST_EN60, TEST_EN57})
STUB_LABELS = {
    TEST_KETTELL: LABEL_KETTELL,
    TEST_RAVEN: LABEL_RAVEN,
    TEST_EN60: LABEL_EN60,
    TEST_EN57: LABEL_EN57,
}

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

# --- ОПГ: тот же блок вопросов, что ранее (RIASEC «одно из двух») ---
HOLLAND_TYPES = {
    "R": "Реалистический (практический) тип",
    "I": "Исследовательский тип",
    "A": "Артистический тип",
    "S": "Социальный тип",
    "E": "Предпринимающий тип",
    "C": "Конвенциональный (офисно-деловой) тип",
}

QUESTIONS_HOLLAND = [
    {"q": "1. Что ближе?", "options": {"1": ("Ремонтировать технику, работать руками", {"R": 1}), "2": ("Вести финансовую отчётность по шаблону", {"C": 1})}},
    {"q": "2. Что ближе?", "options": {"1": ("Разбираться в причинах явлений, ставить опыты", {"I": 1}), "2": ("Обучать и поддерживать людей", {"S": 1})}},
    {"q": "3. Что ближе?", "options": {"1": ("Рисовать, писать, выступать", {"A": 1}), "2": ("Продвигать идею, вести переговоры", {"E": 1})}},
    {"q": "4. Что ближе?", "options": {"1": ("Собирать узлы по инструкции", {"R": 1}), "2": ("Искать закономерности в данных", {"I": 1})}},
    {"q": "5. Что ближе?", "options": {"1": ("Оформлять документы, следовать регламенту", {"C": 1}), "2": ("Организовывать людей и ресурсы под цель", {"E": 1})}},
    {"q": "6. Что ближе?", "options": {"1": ("Помогать людям в трудной ситуации", {"S": 1}), "2": ("Создавать эстетический образ, дизайн", {"A": 1})}},
    {"q": "7. Что ближе?", "options": {"1": ("Работать на производстве, с инструментом", {"R": 1}), "2": ("Консультировать по выбору профессии или здоровья", {"S": 1})}},
    {"q": "8. Что ближе?", "options": {"1": ("Анализировать тексты, таблицы, модели", {"I": 1}), "2": ("Вести проекты и договариваться с партнёрами", {"E": 1})}},
    {"q": "9. Что ближе?", "options": {"1": ("Планировать бюджет и сроки по чек-листам", {"C": 1}), "2": ("Импровизировать в творческой задаче", {"A": 1})}},
    {"q": "10. Что ближе?", "options": {"1": ("Настраивать оборудование, машины", {"R": 1}), "2": ("Писать код или научные заметки", {"I": 1})}},
    {"q": "11. Что ближе?", "options": {"1": ("Работать в команде над социальным проектом", {"S": 1}), "2": ("Запускать инициативы, искать клиентов", {"E": 1})}},
    {"q": "12. Что ближе?", "options": {"1": ("Систематизировать архивы и базы", {"C": 1}), "2": ("Монтировать, строить, возиться с механизмом", {"R": 1})}},
    {"q": "13. Что ближе?", "options": {"1": ("Исследовать, читать специальную литературу", {"I": 1}), "2": ("Учить или тренировать группу", {"S": 1})}},
    {"q": "14. Что ближе?", "options": {"1": ("Выступать, презентовать идею публике", {"A": 1}), "2": ("Следить за точностью цифр и форм", {"C": 1})}},
    {"q": "15. Что ближе?", "options": {"1": ("Работать в поле, мастерской, на объекте", {"R": 1}), "2": ("Предпринимать: риск, конкуренция, сделки", {"E": 1})}},
    {"q": "16. Что ближе?", "options": {"1": ("Решать логические и аналитические задачи", {"I": 1}), "2": ("Заботиться о благополучии других", {"S": 1})}},
    {"q": "17. Что ближе?", "options": {"1": ("Делать красивые вещи или перформансы", {"A": 1}), "2": ("Соблюдать стандарты и процедуры", {"C": 1})}},
    {"q": "18. Что ближе?", "options": {"1": ("Управлять командой и отвечать за результат", {"E": 1}), "2": ("Копать глубже в теме «как это устроено»", {"I": 1})}},
    {"q": "19. Что ближе?", "options": {"1": ("Работать с живыми материалами и инструментом", {"R": 1}), "2": ("Общаться и мотивировать людей", {"S": 1})}},
    {"q": "20. Что ближе?", "options": {"1": ("Продавать, убеждать, вести переговоры", {"E": 1}), "2": ("Оформлять отчёты, вести учёт", {"C": 1})}},
    {"q": "21. Что ближе?", "options": {"1": ("Свободное творческое выражение", {"A": 1}), "2": ("Строгая проверка фактов и гипотез", {"I": 1})}},
    {"q": "22. Что ближе?", "options": {"1": ("Практические задачи «сделай руками»", {"R": 1}), "2": ("Эмпатия и поддержка в общении", {"S": 1})}},
    {"q": "23. Что ближе?", "options": {"1": ("Лидерство и ответственность за прибыль/проект", {"E": 1}), "2": ("Работа по регламенту и шаблонам", {"C": 1})}},
    {"q": "24. Что ближе?", "options": {"1": ("Эксперименты, лаборатория, наука", {"I": 1}), "2": ("Театр, музыка, визуальное искусство", {"A": 1})}},
]

CAREER_HINTS_HOLLAND = {
    "R": "🔩 Реалистический тип — практика, техника, мастерство. Инженер, механик, технолог, повар, военный специалист.",
    "I": "🔬 Исследовательский тип — анализ, наука, глубина. Учёный, врач-диагност, программист-алгоритмист, аналитик.",
    "A": "🎭 Артистический тип — творчество, свобода формы. Дизайнер, художник, журналист, актёр, маркетинг-креатив.",
    "S": "🤝 Социальный тип — люди, забота, обучение. Учитель, психолог, HR, социальный работник, врач.",
    "E": "📈 Предпринимающий тип — влияние, цели, бизнес. Менеджер, предприниматель, продажи, продюсер.",
    "C": "📋 Конвенциональный тип — порядок, данные, правила. Бухгалтер, экономист, делопроизводитель, логистика.",
}

# --- Таблица ОПТ: вопросы из JSON (модификация Резапкиной) ---
with open(JOVASHI_PATH, encoding="utf-8") as _f:
    QUESTIONS_JOVASHI = json.load(_f)

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
    "Привет! Это бот для прохождения профориентации.\n\n"
    "Доступные методики:\n"
    "• ДДО — дифференциально-диагностический опросник Е.А. Климова (20 вопросов, выбор из двух занятий).\n"
    f"• {LABEL_OPG} — 24 вопроса, выбор из двух вариантов.\n"
    f"• {LABEL_PROF_TABLE} — 24 вопроса, три варианта ответа.\n"
    f"• {LABEL_KETTELL}, {LABEL_RAVEN}, {LABEL_EN60}, {LABEL_EN57} — скоро.\n\n"
    "Команды (можно писать в чат):\n"
    "— ддо — начать ДДО;\n"
    "— опг — начать ОПГ;\n"
    "— таблица — начать таблицу для ориентировочного определения предпочтительности типа будущей профессии;\n"
    "— привет, старт или меню — это сообщение и кнопки.\n\n"
    "Отвечай на вопросы кнопками под сообщением. Удачи!"
)


def questions_for(test_id: str):
    if test_id == TEST_DDO:
        return QUESTIONS_DDO
    if test_id == TEST_HOLLAND:
        return QUESTIONS_HOLLAND
    if test_id == TEST_JOVASHI:
        return QUESTIONS_JOVASHI
    return QUESTIONS_DDO


def empty_scores(test_id: str) -> dict:
    if test_id == TEST_DDO:
        return {k: 0 for k in PROFESSION_TYPES}
    if test_id == TEST_HOLLAND:
        return {k: 0 for k in HOLLAND_TYPES}
    if test_id == TEST_JOVASHI:
        return {k: 0 for k in JOVASHI_SPHERES}
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
                test_id TEXT NOT NULL DEFAULT 'ddo'
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
        _ensure_column(conn, "user_progress", "test_id", "TEXT NOT NULL DEFAULT 'ddo'")
        _ensure_column(conn, "test_results", "test_id", "TEXT NOT NULL DEFAULT 'ddo'")
        _ensure_column(conn, "test_results", "best_type", "TEXT NOT NULL DEFAULT ''")
        conn.commit()


def now_ts():
    return int(time.time())


def save_progress(user_id: int, test_id: str, step: int, scores: dict, status: str = "in_progress"):
    ts = now_ts()
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_progress (user_id, step, scores_json, status, started_at, last_activity_at, reminded_at, test_id)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT(user_id) DO UPDATE SET
            step=excluded.step,
            scores_json=excluded.scores_json,
            status=excluded.status,
            last_activity_at=excluded.last_activity_at,
            test_id=excluded.test_id
            """,
            (user_id, step, json.dumps(scores, ensure_ascii=False), status, ts, ts, test_id),
        )
        conn.commit()


def touch_progress(user_id: int):
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_progress SET last_activity_at=? WHERE user_id=?",
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


def get_progress(user_id: int):
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT step, scores_json, status, test_id
            FROM user_progress
            WHERE user_id=?
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        step, scores_json, status, test_id = row
        scores = json.loads(scores_json)
        return {"step": step, "scores": scores, "status": status, "test_id": test_id or TEST_DDO}


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


def build_menu_keyboard():
    kb = VkKeyboard(one_time=False, inline=False)
    kb.add_button("ДДО", color=VkKeyboardColor.POSITIVE)
    kb.add_button(KB_OPG, color=VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button(KB_PROF_TABLE, color=VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button(KB_KETTELL, color=VkKeyboardColor.SECONDARY)
    kb.add_button(KB_RAVEN, color=VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button(KB_EN60, color=VkKeyboardColor.SECONDARY)
    kb.add_button(KB_EN57, color=VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button("Меню", color=VkKeyboardColor.SECONDARY)
    return kb.get_keyboard()


def keyboard_for_test(test_id: str):
    if test_id == TEST_JOVASHI:
        return build_answer_keyboard_jovashi()
    return build_answer_keyboard_binary()


def send_message(vk, user_id, message, keyboard=None):
    vk.messages.send(user_id=user_id, random_id=0, message=message, keyboard=keyboard)


def render_question(test_id: str, step: int) -> str:
    item = questions_for(test_id)[step]
    lines = [item["q"]]
    for key in sorted(item["options"].keys(), key=lambda x: int(x)):
        opt = item["options"][key]
        label = opt[0] if isinstance(opt[0], str) else str(opt[0])
        lines.append(f"{key}) {label}")
    if test_id == TEST_JOVASHI:
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


def finish_test(vk, user_id: int, test_id: str, scores: dict):
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

    if test_id == TEST_DDO:
        title = "📊 Твой результат по тесту ДДО (топ-3):"
        lines = [title]
        for i, (ptype, points) in enumerate(top3, 1):
            lines.append(f"{i}. {PROFESSION_TYPES[ptype]} — {points} баллов")
        lines.append(f"\n{CAREER_HINTS_DDO.get(best_key, 'Выбирай направление, которое откликается сильнее.')}")
        lines.append("\nХочешь пройти снова — выбери тест кнопкой или командой.")
    elif test_id == TEST_HOLLAND:
        lines = [f"📊 Твой результат по «{LABEL_OPG}» (топ-3):"]
        for i, (code, points) in enumerate(top3, 1):
            lines.append(f"{i}. {HOLLAND_TYPES[code]} — {points} баллов")
        lines.append(f"\n{CAREER_HINTS_HOLLAND.get(best_key, '')}")
        lines.append("\nКод RIASEC по убыванию: " + "".join(k for k, _ in sorted_types[:3] if _ > 0) + ".")
        lines.append("\nМожно пройти другой тест — открой «Меню» или нажми кнопку.")
    else:
        lines = [f"📊 Твой результат по «{LABEL_PROF_TABLE}» (топ-3 сферы):"]
        for i, (key, points) in enumerate(top3, 1):
            interp = _interpret_jovashi(points)
            lines.append(f"{i}. {JOVASHI_SPHERES[key]} — {points} баллов ({interp})")
        lines.append(f"\n{CAREER_HINTS_JOVASHI.get(best_key, '')}")
        lines.append("\nСравни несколько сильных сфер и подумай, какие профессии их объединяют.")

    send_message(vk, user_id, "\n".join(lines), keyboard=build_menu_keyboard())
    complete_progress(user_id)
    save_result(user_id, test_id, scores, top3)


def start_test(vk, user_id: int, test_id: str):
    qs = questions_for(test_id)
    scores = empty_scores(test_id)
    save_progress(user_id=user_id, test_id=test_id, step=0, scores=scores, status="in_progress")
    if test_id == TEST_DDO:
        intro = f"Тест ДДО (Климов) запущен.\nВопросов: {len(qs)}."
    elif test_id == TEST_HOLLAND:
        intro = f"«{LABEL_OPG}» запущен.\nВопросов: {len(qs)}."
    else:
        intro = f"«{LABEL_PROF_TABLE}» запущен.\nВопросов: {len(qs)}."
    send_message(vk, user_id, f"{intro}\n\n{render_question(test_id, 0)}", keyboard=keyboard_for_test(test_id))


def send_welcome(vk, user_id: int):
    send_message(vk, user_id, WELCOME_TEXT, keyboard=build_menu_keyboard())


def send_stub_notice(vk, user_id: int, label: str):
    send_message(
        vk,
        user_id,
        f"Методика «{label}» пока в разработке. Выбери другой тест в меню.",
        keyboard=build_menu_keyboard(),
    )


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
    touch_progress(user_id)
    valid = {"1", "2"} if test_id != TEST_JOVASHI else {"1", "2", "3"}
    if text not in valid:
        send_message(vk, user_id, f"Пожалуйста, используй кнопки {' / '.join(sorted(valid))}.", keyboard=keyboard_for_test(test_id))
        send_message(vk, user_id, render_question(test_id, progress["step"]), keyboard=keyboard_for_test(test_id))
        return
    step = progress["step"]
    scores = progress["scores"]
    qs = questions_for(test_id)
    if step >= len(qs):
        finish_test(vk, user_id, test_id, scores)
        return
    weights = _option_weights(qs[step]["options"][text])
    for ptype, value in weights.items():
        scores[ptype] = scores.get(ptype, 0) + value
    step += 1
    if step < len(qs):
        save_progress(user_id=user_id, test_id=test_id, step=step, scores=scores, status="in_progress")
        send_message(vk, user_id, render_question(test_id, step), keyboard=keyboard_for_test(test_id))
    else:
        save_progress(user_id=user_id, test_id=test_id, step=step, scores=scores, status="completed")
        finish_test(vk, user_id, test_id, scores)


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
                total = len(questions_for(test_id))
                step_display = progress["step"] + 1
                send_message(
                    vk,
                    uid,
                    f"⏰ Напоминание: ты на вопросе {step_display} из {total}.\nПродолжим? Выбери ответ кнопками.",
                    keyboard=keyboard_for_test(test_id),
                )
                set_reminded(uid)
        except Exception as e:
            print(f"[reminder_worker] error: {e}")
        time.sleep(REMINDER_CHECK_EVERY_SEC)


def _normalize_cmd(s: str) -> str:
    return s.strip().lower()


def dispatch_command(vk, user_id: int, text: str) -> bool:
    """Обрабатывает команды меню. Возвращает True, если сообщение обработано."""
    stripped = text.strip()
    t = _normalize_cmd(text)
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
        start_test(vk, user_id, TEST_HOLLAND)
        return True
    if t in ("таблица", "таблица опт", "опт"):
        start_test(vk, user_id, TEST_JOVASHI)
        return True
    if t in ("кеттелл", "kettell", "cattell"):
        send_stub_notice(vk, user_id, LABEL_KETTELL)
        return True
    if t in ("равен", "raven"):
        send_stub_notice(vk, user_id, LABEL_RAVEN)
        return True
    if t.replace(" ", "") in ("эн-60", "эн60", "en-60", "en60"):
        send_stub_notice(vk, user_id, LABEL_EN60)
        return True
    if t.replace(" ", "") in ("эн-57", "эн57", "en-57", "en57"):
        send_stub_notice(vk, user_id, LABEL_EN57)
        return True
    # Подписи с клавиатуры (с заглавной)
    if stripped == "ДДО":
        start_test(vk, user_id, TEST_DDO)
        return True
    if stripped == KB_OPG:
        start_test(vk, user_id, TEST_HOLLAND)
        return True
    if stripped == KB_PROF_TABLE:
        start_test(vk, user_id, TEST_JOVASHI)
        return True
    if stripped == KB_KETTELL:
        send_stub_notice(vk, user_id, LABEL_KETTELL)
        return True
    if stripped == KB_RAVEN:
        send_stub_notice(vk, user_id, LABEL_RAVEN)
        return True
    if stripped == KB_EN60:
        send_stub_notice(vk, user_id, LABEL_EN60)
        return True
    if stripped == KB_EN57:
        send_stub_notice(vk, user_id, LABEL_EN57)
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
    print(f"Бот запущен: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    while True:
        try:
            for event in longpoll.listen():
                if event.type != VkBotEventType.MESSAGE_NEW:
                    continue
                message = event.obj.message
                user_id = message["from_id"]
                raw = message.get("text", "")
                text_stripped = raw.strip()
                text_lower = text_stripped.lower()
                if dispatch_command(vk, user_id, raw):
                    continue
                if text_lower in ("1", "2", "3"):
                    handle_answer(vk, user_id, text_lower)
                else:
                    send_message(
                        vk,
                        user_id,
                        "Не понял команду. Напиши «меню» или выбери тест кнопкой внизу.",
                        keyboard=build_menu_keyboard(),
                    )
        except _LONGPOLL_TRANSIENT as e:
            print(
                f"[longpoll] сетевая ошибка ({type(e).__name__}): {e!s}. "
                f"Повтор через {LONGPOLL_RETRY_SLEEP_SEC} с…"
            )
            time.sleep(LONGPOLL_RETRY_SLEEP_SEC)
            continue


if __name__ == "__main__":
    main()
