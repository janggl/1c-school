import asyncio
import json
import os
import sys
import threading
import webbrowser
import aiohttp
from datetime import date, datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from types import SimpleNamespace

TOKEN_HELP_URL = "https://school.mos.ru/?backUrl=https%3A%2F%2Fschool.mos.ru%2Fv2%2Ftoken%2Frefresh"
BUG_REPORT_URL = "https://github.com/janggl/1c-school/issues/new"
CONFIG_FILE_NAME = "mesh_client_settings.json"


# -------------------------
# Подключение приложенной библиотеки
# -------------------------
def setup_schoolapi_imports() -> None:
    """Ищет папку SchoolAPI-main рядом с приложением и добавляет её в sys.path."""
    base_dir = Path(__file__).resolve().parent
    candidates = [
        base_dir / "SchoolAPI-main" / "SchoolAPI",
        base_dir.parent / "SchoolAPI-main" / "SchoolAPI",
        Path.cwd() / "SchoolAPI-main" / "SchoolAPI",
    ]

    for candidate in candidates:
        if candidate.exists():
            sys.path.insert(0, str(candidate))
            return

    raise RuntimeError(
        "Не найдена папка SchoolAPI-main/SchoolAPI. "
        "Распакуйте приложенную библиотеку рядом с app.py."
    )


setup_schoolapi_imports()

from student.student import Student  # type: ignore
from schedule.schedule import Schedule  # type: ignore
from marks.marks import Marks  # type: ignore
from homeworks.homeworks import Homeworks  # type: ignore
from notification.notification import Notification  # type: ignore
from errors.errors import TokenError, DnevnikError, LibError  # type: ignore


# -------------------------
# Патч библиотеки SchoolAPI
# -------------------------
_original_get_student_profiles = Student.getStudentProfiles


def _build_profile_from_session(student: Student):
    profiles = getattr(student, "profiles", None) or []
    first = profiles[0] if isinstance(profiles, list) and profiles else {}

    if hasattr(first, "id"):
        profile_id = getattr(first, "id")
        class_name = getattr(first, "class_name", "—")
        school_name = getattr(first, "school_name", "—")
    elif isinstance(first, dict):
        profile_id = first.get("id") or getattr(student, "id", None)
        class_name = first.get("class_name") or first.get("group_name") or "—"
        school_name = first.get("school_name") or first.get("school") or "—"
    else:
        profile_id = getattr(student, "id", None)
        class_name = "—"
        school_name = "—"

    session = getattr(student, "session", None)
    if session is not None:
        class_name = getattr(session, "class_name", class_name)
        school_name = getattr(session, "school_name", school_name)

    return SimpleNamespace(
        id=profile_id or getattr(student, "id", None),
        class_name=class_name,
        group_name=class_name,
        school_name=school_name,
        school=school_name,
    )


async def _patched_get_student_profiles(self: Student):
    try:
        return await _original_get_student_profiles(self)
    except DnevnikError as exc:
        if "Code: 403" not in str(exc):
            raise
        profile = _build_profile_from_session(self)
        self.studentProfile = profile
        self.studentProfileJson = {
            "id": getattr(profile, "id", None),
            "class_name": getattr(profile, "class_name", "—"),
            "school_name": getattr(profile, "school_name", "—"),
            "fallback": True,
        }
        return profile


Student.getStudentProfiles = _patched_get_student_profiles


# -------------------------
# Асинхронный движок в фоне
# -------------------------
class AsyncWorker:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro, on_success=None, on_error=None):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def _done_callback(fut):
            try:
                result = fut.result()
                if on_success:
                    on_success(result)
            except Exception as exc:  # noqa: BLE001
                if on_error:
                    on_error(exc)

        future.add_done_callback(_done_callback)
        return future


# -------------------------
# Утилиты форматирования
# -------------------------
def safe_get(obj, *names, default="—"):
    for name in names:
        if isinstance(obj, dict) and name in obj:
            value = obj.get(name)
            if value is not None and value != "":
                return value
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None and value != "":
                return value
    return default


class MeshClient:

    def __init__(self, token: str):
        self.student = Student(token)
        self.schedule_api = Schedule(self.student)
        self.marks_api = Marks(self.student)
        self.homeworks_api = Homeworks(self.student)
        self.notifications_api = Notification(self.student)

    async def _ensure_activated(self):
        if not getattr(self.student, "isActivate", False):
            await self.student.activate()
        return self.student

    async def _get_profile_id(self):
        profile = await self.student.getStudentProfiles()
        return safe_get(profile, "id", default=safe_get(self.student, "id", default=None))

    async def _request_json(self, url: str, headers: dict):
        async with aiohttp.ClientSession() as session:
            response = await session.get(url, headers=headers)
            if response.status == 401:
                await self.student.refresh()
                headers = dict(headers)
                if "Auth-Token" in headers:
                    headers["Auth-Token"] = self.student.token
                if "Auth-token" in headers:
                    headers["Auth-token"] = self.student.token
                if "Authorization" in headers:
                    headers["Authorization"] = f"Bearer {self.student.token}"
                response = await session.get(url, headers=headers)
            if response.status not in (200, 201):
                text = await response.text()
                raise DnevnikError(f"Code: {response.status}\nResponse: {text}")
            return await response.json()

    def _extract_items(self, data, priority_keys):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in priority_keys:
                value = data.get(key)
                if isinstance(value, list):
                    return value
            for value in data.values():
                items = self._extract_items(value, priority_keys)
                if items:
                    return items
        return []

    def _save_debug(self, name: str, data):
        try:
            import json
            debug_dir = Path(__file__).resolve().parent / "debug"
            debug_dir.mkdir(exist_ok=True)
            with open(debug_dir / f"{name}.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    async def activate(self):
        return await self._ensure_activated()

    async def profile_info(self):
        await self._ensure_activated()
        profile = await self.student.getStudentProfiles()
        return {
            "fio": " ".join(
                str(x)
                for x in [
                    safe_get(self.student.session, "last_name", default=""),
                    safe_get(self.student.session, "first_name", default=""),
                    safe_get(self.student.session, "middle_name", default=""),
                ]
                if x
            ).strip() or "—",
            "student_id": safe_get(self.student.session, "id"),
            "person_id": safe_get(self.student.session, "person_id"),
            "birth_date": safe_get(self.student.session, "date_of_birth"),
            "school": safe_get(profile, "school_name", "school", default="—"),
            "class_name": safe_get(profile, "class_name", "group_name", default="—"),
            "profile_id": safe_get(profile, "id"),
        }

    async def schedule_for_date(self, dt: str):
        await self._ensure_activated()
        profile_id = await self._get_profile_id()

        try:
            data = await self._request_json(
                f"https://school.mos.ru/api/family/web/v1/schedule?student_id={profile_id}&date={dt}",
                {
                    "Auth-Token": self.student.token,
                    "X-Mes-Subsystem": "familyweb",
                },
            )
            self._save_debug("schedule_familyweb", data)
            items = self._extract_items(data, ["activities", "items", "payload", "lessons", "events"])
            if items:
                return items
        except Exception:
            pass

        data = await self._request_json(
            f"https://school.mos.ru/api/eventcalendar/v1/api/events?person_ids={self.student.person_id}&begin_date={dt}&end_date={dt}",
            {
                "Authorization": f"Bearer {self.student.token}",
                "X-Mes-Subsystem": "familyweb",
                "X-Mes-Role": "student",
            },
        )
        self._save_debug("schedule_eventcalendar", data)
        return self._extract_items(data, ["activities", "items", "payload", "events", "response"])

    async def marks_for_period(self, date_from: str, date_to: str):
        await self._ensure_activated()
        profile_id = await self._get_profile_id()

        try:
            data = await self._request_json(
                f"https://school.mos.ru/api/family/web/v1/marks?student_id={profile_id}&from={date_from}&to={date_to}",
                {
                    "Auth-Token": self.student.token,
                    "X-Mes-Subsystem": "familyweb",
                },
            )
            self._save_debug("marks_familyweb", data)
            items = self._extract_items(data, ["data", "payload", "items", "marks"])
            if items:
                return items
        except Exception:
            pass

        date_from_formatted = datetime.strptime(date_from, "%Y-%m-%d").strftime("%d.%m.%Y")
        date_to_formatted = datetime.strptime(date_to, "%Y-%m-%d").strftime("%d.%m.%Y")
        data = await self._request_json(
            "https://dnevnik.mos.ru/core/api/marks"
            f"?created_at_from={date_from_formatted}"
            f"&created_at_to={date_to_formatted}"
            f"&student_profile_id={profile_id}",
            {
                "Auth-token": self.student.token,
                "Authorization": f"Bearer {self.student.token}",
                "Profile-Id": str(profile_id),
                "User-Id": str(safe_get(self.student, "id", default="")),
            },
        )
        self._save_debug("marks_core", data)
        return self._extract_items(data, ["data", "payload", "items", "marks"])

    async def subject_averages(self):
        await self._ensure_activated()
        profile_id = await self._get_profile_id()

        data = await self._request_json(
            f"https://school.mos.ru/api/family/web/v1/subject_marks?student_id={profile_id}",
            {
                "Authorization": f"Bearer {self.student.token}",
                "X-Mes-Subsystem": "familyweb",
            },
        )
        self._save_debug("subject_marks_familyweb", data)
        return self._extract_items(data, ["data", "payload", "items", "subjects", "subject_marks", "marks", "results"])

    async def marks_dashboard(self, date_from: str, date_to: str):
        marks_result, averages_result = await asyncio.gather(
            self.marks_for_period(date_from, date_to),
            self.subject_averages(),
            return_exceptions=True,
        )
        if isinstance(marks_result, Exception):
            raise marks_result
        if isinstance(averages_result, Exception):
            averages_result = []
        return {"marks": marks_result, "subject_averages": averages_result}

    async def homework_for_period(self, date_from: str, date_to: str):
        await self._ensure_activated()
        return await self.homeworks_api.getHomeworkByDate(date_from, date_to)

    async def notifications(self):
        await self._ensure_activated()
        return await self.notifications_api.getNotifications()


# -------------------------
# GUI
# -------------------------
class MeshDesktopApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("1С:Предприятие — МЭШ Клиент")
        self.geometry("1220x760")
        self.minsize(1040, 680)

        self.worker = AsyncWorker()
        self.client = None
        self.config_path = Path(__file__).resolve().parent / CONFIG_FILE_NAME

        self._build_style()
        self._build_layout()
        self._load_saved_token()

    def _build_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        self.colors = {
            "bg": "#d8d2c7",
            "panel": "#ece8df",
            "toolbar": "#f4f1ea",
            "title": "#546a7b",
            "accent": "#d6c4a4",
            "accent_dark": "#b79f74",
            "grid": "#ffffff",
            "text": "#202020",
        }
        self.configure(bg=self.colors["bg"])

        style.configure("TFrame", background=self.colors["bg"])
        style.configure("Main.TFrame", background=self.colors["panel"], relief="solid", borderwidth=1)
        style.configure("Card.TFrame", background=self.colors["panel"], relief="solid", borderwidth=1)
        style.configure("Toolbar.TFrame", background=self.colors["toolbar"], relief="solid", borderwidth=1)
        style.configure(
            "Header.TLabel",
            background=self.colors["title"],
            foreground="white",
            font=("Tahoma", 11, "bold"),
            padding=8,
        )
        style.configure(
            "SubHeader.TLabel",
            background=self.colors["toolbar"],
            foreground=self.colors["text"],
            font=("Tahoma", 9, "bold"),
            padding=(6, 4),
        )
        style.configure("TLabel", background=self.colors["bg"], foreground=self.colors["text"], font=("Tahoma", 9))
        style.configure("CardLabel.TLabel", background=self.colors["panel"], foreground=self.colors["text"], font=("Tahoma", 9))
        style.configure(
            "TButton",
            background=self.colors["accent"],
            foreground=self.colors["text"],
            padding=(10, 5),
            font=("Tahoma", 9),
            borderwidth=1,
            relief="raised",
        )
        style.map("TButton", background=[("active", self.colors["accent_dark"]), ("pressed", self.colors["accent_dark"])])
        style.configure(
            "Subtle.TButton",
            background=self.colors["toolbar"],
            foreground="#5f5f5f",
            padding=(8, 4),
            font=("Tahoma", 8),
            borderwidth=1,
            relief="solid",
        )
        style.map("Subtle.TButton", background=[("active", self.colors["panel"]), ("pressed", self.colors["panel"])])
        style.configure("TEntry", padding=5, fieldbackground="#ffffff")
        style.configure("Treeview", background=self.colors["grid"], fieldbackground=self.colors["grid"], rowheight=28, font=("Tahoma", 9))
        style.configure("Treeview.Heading", background=self.colors["accent"], foreground=self.colors["text"], font=("Tahoma", 9, "bold"), relief="raised", padding=4)
        style.map("Treeview.Heading", background=[("active", self.colors["accent_dark"])])
        style.configure("TNotebook", background=self.colors["bg"], borderwidth=0, tabmargins=[2, 4, 2, 0])
        style.configure("TNotebook.Tab", background="#c8c1b3", padding=(16, 8), font=("Tahoma", 9, "bold"), borderwidth=1)
        style.map("TNotebook.Tab", background=[("selected", self.colors["panel"]), ("active", "#d8cfbf")])

    def _build_layout(self):
        top = ttk.Frame(self, style="Main.TFrame")
        top.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(top, text="1С:Предприятие — АРМ ученика МЭШ", style="Header.TLabel").pack(fill="x")

        toolbar = ttk.Frame(self, style="Toolbar.TFrame", padding=8)
        toolbar.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(toolbar, text="Подключение к системе", style="SubHeader.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Label(toolbar, text="Токен авторизации:").grid(row=0, column=1, sticky="w", padx=(0, 6))

        self.token_var = tk.StringVar()
        self.token_entry = ttk.Entry(toolbar, textvariable=self.token_var, width=82)
        self.token_entry.grid(row=0, column=2, sticky="ew")
        self.token_entry.bind("<<Paste>>", self._paste_into_token_entry, add="+")
        self.token_entry.bind("<Shift-Insert>", self._paste_into_token_entry, add="+")
        self.token_entry.bind("<Control-KeyPress>", self._handle_token_paste_shortcut, add="+")

        ttk.Button(toolbar, text="Подключиться", command=self.connect).grid(row=0, column=3, padx=(8, 4))
        ttk.Button(toolbar, text="Получить токен авторизации", command=self.open_token_help).grid(row=0, column=4, padx=(4, 0))

        self.status_var = tk.StringVar(value="Статус: не подключено")
        ttk.Label(toolbar, textvariable=self.status_var).grid(row=1, column=0, columnspan=5, sticky="w", pady=(8, 0))
        toolbar.columnconfigure(2, weight=1)

        body = ttk.Frame(self, style="Main.TFrame", padding=6)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.notebook = ttk.Notebook(body)
        self.notebook.pack(fill="both", expand=True)

        self._build_profile_tab()
        self._build_schedule_tab()
        self._build_marks_tab()
        self._build_homework_tab()
        self._build_notifications_tab()

        footer = ttk.Frame(self, style="Toolbar.TFrame", padding=(10, 4))
        footer.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(
            footer,
            text="2026. Github janggl. Не является оффициальной версией МЭШ.",
            style="CardLabel.TLabel",
        ).pack(side="left")
        ttk.Button(footer, text="❗ Сообщить об ошибке", style="Subtle.TButton", command=self.open_bug_report).pack(side="right")

    def _build_profile_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Профиль")

        ttk.Label(tab, text="Карточка ученика", style="Header.TLabel").pack(fill="x", padx=8, pady=8)
        card = ttk.Frame(tab, style="Card.TFrame", padding=12)
        card.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.profile_labels = {}
        fields = [("ФИО", "fio"), ("ID ученика", "student_id"), ("Person ID", "person_id"), ("Дата рождения", "birth_date"), ("Школа", "school"), ("Класс", "class_name"), ("Profile ID", "profile_id")]
        for idx, (title, key) in enumerate(fields):
            ttk.Label(card, text=f"{title}:", style="CardLabel.TLabel", font=("Tahoma", 9, "bold")).grid(row=idx, column=0, sticky="w", pady=6, padx=(0, 12))
            lbl = ttk.Label(card, text="—", style="CardLabel.TLabel")
            lbl.grid(row=idx, column=1, sticky="w", pady=6)
            self.profile_labels[key] = lbl

        ttk.Button(card, text="Обновить профиль", command=self.load_profile).grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=(14, 0))
        card.columnconfigure(1, weight=1)

    def _build_schedule_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Расписание")

        tools = ttk.Frame(tab, style="Card.TFrame", padding=10)
        tools.pack(fill="x", padx=8, pady=8)
        ttk.Label(tools, text="Дата (ГГГГ-ММ-ДД):", style="CardLabel.TLabel").grid(row=0, column=0, sticky="w")
        self.schedule_date_var = tk.StringVar(value=date.today().isoformat())
        ttk.Entry(tools, textvariable=self.schedule_date_var, width=16).grid(row=0, column=1, padx=8)
        ttk.Button(tools, text="Загрузить", command=self.load_schedule).grid(row=0, column=2)

        self.schedule_tree = ttk.Treeview(tab, columns=("time", "subject", "teacher", "room", "type"), show="headings")
        for col, text, width in [("time", "Время", 160), ("subject", "Предмет", 280), ("teacher", "Учитель", 220), ("room", "Кабинет", 120), ("type", "Тип", 160)]:
            self.schedule_tree.heading(col, text=text)
            self.schedule_tree.column(col, width=width, anchor="w")
        self.schedule_tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _build_marks_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Оценки")

        tools = ttk.Frame(tab, style="Card.TFrame", padding=10)
        tools.pack(fill="x", padx=8, pady=8)
        today = date.today()
        first_day = today.replace(day=1)

        ttk.Label(tools, text="С:", style="CardLabel.TLabel").grid(row=0, column=0, sticky="w")
        self.marks_from_var = tk.StringVar(value=first_day.isoformat())
        ttk.Entry(tools, textvariable=self.marks_from_var, width=16).grid(row=0, column=1, padx=6)
        ttk.Label(tools, text="По:", style="CardLabel.TLabel").grid(row=0, column=2, sticky="w")
        self.marks_to_var = tk.StringVar(value=today.isoformat())
        ttk.Entry(tools, textvariable=self.marks_to_var, width=16).grid(row=0, column=3, padx=6)
        ttk.Button(tools, text="Показать", command=self.load_marks).grid(row=0, column=4, padx=(8, 0))

        averages_card = ttk.Frame(tab, style="Card.TFrame", padding=10)
        averages_card.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(averages_card, text="Средние оценки по предметам", style="SubHeader.TLabel").pack(fill="x", pady=(0, 8))

        self.subject_averages_tree = ttk.Treeview(
            averages_card,
            columns=("subject", "average", "period"),
            show="headings",
            height=6,
        )
        for col, text, width in [("subject", "Предмет", 300), ("average", "Средний балл", 140), ("period", "Период", 260)]:
            self.subject_averages_tree.heading(col, text=text)
            self.subject_averages_tree.column(col, width=width, anchor="w")
        self.subject_averages_tree.pack(fill="x")

        self.marks_tree = ttk.Treeview(tab, columns=("date", "subject", "value", "weight", "comment"), show="headings")
        for col, text, width in [("date", "Дата", 120), ("subject", "Предмет", 240), ("value", "Оценка", 100), ("weight", "Вес", 100), ("comment", "Комментарий", 420)]:
            self.marks_tree.heading(col, text=text)
            self.marks_tree.column(col, width=width, anchor="w")
        self.marks_tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _build_homework_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Домашние задания")

        tools = ttk.Frame(tab, style="Card.TFrame", padding=10)
        tools.pack(fill="x", padx=8, pady=8)
        today = date.today()
        next_week = today + timedelta(days=7)

        ttk.Label(tools, text="С:", style="CardLabel.TLabel").grid(row=0, column=0, sticky="w")
        self.hw_from_var = tk.StringVar(value=today.isoformat())
        ttk.Entry(tools, textvariable=self.hw_from_var, width=16).grid(row=0, column=1, padx=6)
        ttk.Label(tools, text="По:", style="CardLabel.TLabel").grid(row=0, column=2, sticky="w")
        self.hw_to_var = tk.StringVar(value=next_week.isoformat())
        ttk.Entry(tools, textvariable=self.hw_to_var, width=16).grid(row=0, column=3, padx=6)
        ttk.Button(tools, text="Загрузить", command=self.load_homeworks).grid(row=0, column=4, padx=(8, 0))

        self.hw_text = scrolledtext.ScrolledText(tab, wrap="word", font=("Tahoma", 9), bg="#ffffff", height=20)
        self.hw_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _build_notifications_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Уведомления")

        tools = ttk.Frame(tab, style="Card.TFrame", padding=10)
        tools.pack(fill="x", padx=8, pady=8)
        ttk.Button(tools, text="Обновить", command=self.load_notifications).pack(anchor="w")

        self.notifications_text = scrolledtext.ScrolledText(tab, wrap="word", font=("Tahoma", 9), bg="#ffffff")
        self.notifications_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _load_saved_token(self):
        try:
            if self.config_path.exists():
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                token = str(data.get("token", "")).strip()
                if token:
                    self.token_var.set(token)
        except Exception:
            pass

    def _save_token(self):
        try:
            self.config_path.write_text(json.dumps({"token": self.token_var.get().strip()}, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def open_token_help(self):
        webbrowser.open(TOKEN_HELP_URL)

    def open_bug_report(self):
        webbrowser.open(BUG_REPORT_URL)

    def _handle_token_paste_shortcut(self, event):
        key = event.keysym.lower()
        if event.keycode == 86 or key in {"v", "\u043c"}:
            return self._paste_into_token_entry()
        if event.keycode == 65 or key in {"a", "\u0444"}:
            return self._select_all_token_text()
        return None

    def _select_all_token_text(self, _event=None):
        self.token_entry.selection_range(0, tk.END)
        self.token_entry.icursor(tk.END)
        return "break"


    def _paste_into_token_entry(self, _event=None):
        try:
            clipboard_text = self.clipboard_get()
        except tk.TclError:
            return "break"

        try:
            if self.token_entry.selection_present():
                start = self.token_entry.index("sel.first")
                end = self.token_entry.index("sel.last")
                self.token_entry.delete(start, end)
                insert_at = start
            else:
                insert_at = self.token_entry.index(tk.INSERT)
        except tk.TclError:
            insert_at = self.token_entry.index(tk.END)

        self.token_entry.insert(insert_at, clipboard_text)
        self.token_entry.icursor(insert_at + len(clipboard_text))
        return "break"

    def set_status(self, text: str):
        self.after(0, lambda: self.status_var.set(f"Статус: {text}"))

    def _show_error(self, exc: Exception):
        message = str(exc)
        if "Response: <bound method ClientResponse.text" in message:
            message = message.split("Response:")[0].strip()
        self.after(0, lambda: messagebox.showerror("Ошибка", message))
        self.set_status("ошибка")

    def _run_async(self, coro, on_success):
        self.set_status("загрузка...")

        def success(result):
            self.after(0, lambda: on_success(result))
            self.set_status("готово")

        def error(exc):
            self._show_error(exc)

        self.worker.run(coro, on_success=success, on_error=error)

    def ensure_client(self) -> MeshClient:
        token = self.token_var.get().strip()
        if not token:
            raise ValueError("Введите токен mos.ru / МЭШ.")
        self._save_token()
        if self.client is None or self.client.student.token != token:
            self.client = MeshClient(token)
        return self.client

    def connect(self):
        client = self.ensure_client()
        self._run_async(client.profile_info(), self._fill_profile)

    def load_profile(self):
        client = self.ensure_client()
        self._run_async(client.profile_info(), self._fill_profile)

    def _fill_profile(self, data: dict):
        for key, lbl in self.profile_labels.items():
            lbl.configure(text=str(data.get(key, "—")))

    def load_schedule(self):
        client = self.ensure_client()
        dt = self.schedule_date_var.get().strip()
        self._run_async(client.schedule_for_date(dt), self._fill_schedule)

    def _fill_schedule(self, schedule_obj):
        for row in self.schedule_tree.get_children():
            self.schedule_tree.delete(row)

        lessons = schedule_obj if isinstance(schedule_obj, list) else (getattr(schedule_obj, "activities", None) or getattr(schedule_obj, "items", None) or getattr(schedule_obj, "payload", []))
        if not lessons:
            return

        for item in lessons:
            begin = safe_get(item, "begin_time", "start_at", "starts_at", "start_date", "begin_at", default="")
            end = safe_get(item, "end_time", "finish_time", "end_at", "finish_at", "end_date", default="")
            subject = (safe_get(item, "subject_name", "title", "name", default="") or safe_get(item.get("subject", {}) if isinstance(item, dict) else {}, "name", "title") or "—")
            teacher = (safe_get(item, "teacher_name", "teacher", "teacher_fio", default="") or safe_get(item.get("teacher", {}) if isinstance(item, dict) else {}, "name", "title", "short_name") or "—")
            room = (safe_get(item, "room_name", "room_number", "place", "location", default="") or safe_get(item.get("room", {}) if isinstance(item, dict) else {}, "name", "number") or "—")
            lesson_type = safe_get(item, "lesson_type", "type", "source", default="—")
            time_text = f"{begin} - {end}" if begin or end else "—"
            self.schedule_tree.insert("", "end", values=(time_text, subject, teacher, room, lesson_type))

    def load_marks(self):
        client = self.ensure_client()
        date_from = self.marks_from_var.get().strip()
        date_to = self.marks_to_var.get().strip()
        self._run_async(client.marks_dashboard(date_from, date_to), self._fill_marks)

    def _fill_marks(self, marks_obj):
        for row in self.marks_tree.get_children():
            self.marks_tree.delete(row)
        for row in self.subject_averages_tree.get_children():
            self.subject_averages_tree.delete(row)

        subject_averages = []
        marks_payload = marks_obj
        if isinstance(marks_obj, dict):
            subject_averages = marks_obj.get("subject_averages", [])
            marks_payload = marks_obj.get("marks", [])

        self._fill_subject_averages(subject_averages)

        marks = marks_payload if isinstance(marks_payload, list) else (getattr(marks_payload, "data", None) or getattr(marks_payload, "payload", []))
        for item in marks:
            dt = safe_get(item, "created_at", "date", "updated_at", default="—")
            subject = (safe_get(item, "subject_name", "subject", default="") or safe_get(item.get("subject", {}) if isinstance(item, dict) else {}, "name", "title") or "—")
            value = (safe_get(item, "name", "value", "grade", default="") or safe_get(item.get("mark", {}) if isinstance(item, dict) else {}, "name", "value") or "—")
            weight = safe_get(item, "weight", default="—")
            comment = (safe_get(item, "comment", "control_form_name", default="") or safe_get(item.get("control_form", {}) if isinstance(item, dict) else {}, "name", "title") or "—")
            self.marks_tree.insert("", "end", values=(dt, subject, value, weight, comment))

    def _fill_subject_averages(self, averages_obj):
        rows = self._collect_subject_average_rows(averages_obj)
        if not rows:
            self.subject_averages_tree.insert("", "end", values=("Нет данных от API", "—", "—"))
            return

        seen = set()
        for row in rows:
            if row in seen:
                continue
            seen.add(row)
            self.subject_averages_tree.insert("", "end", values=row)

    def _collect_subject_average_rows(self, averages_obj, inherited_subject=None):
        items = averages_obj if isinstance(averages_obj, list) else (getattr(averages_obj, "data", None) or getattr(averages_obj, "payload", None) or getattr(averages_obj, "items", None) or averages_obj)
        if not isinstance(items, list):
            items = [items] if items else []

        rows = []
        for item in items:
            subject = inherited_subject or (safe_get(item, "subject_name", "name", "title", default="") or safe_get(item.get("subject", {}) if isinstance(item, dict) else {}, "name", "title") or "—")
            average = (safe_get(item, "average_mark", "average", "avg", "avg_mark", "mean_mark", "mean", "value", "result", default="") or safe_get(item.get("mark", {}) if isinstance(item, dict) else {}, "name", "value") or "—")
            period = safe_get(item, "period_name", "period", "study_period_name", "attestation_period_name", "education_period_name", default="—")

            if average != "?":
                rows.append((subject, average, period))

            nested_items = []
            for key in ("periods", "results", "attestations", "children", "items"):
                value = item.get(key) if isinstance(item, dict) else getattr(item, key, None)
                if isinstance(value, list) and value:
                    nested_items = value
                    break

            if nested_items:
                rows.extend(self._collect_subject_average_rows(nested_items, subject))

        return rows

    def load_homeworks(self):
        client = self.ensure_client()
        date_from = self.hw_from_var.get().strip()
        date_to = self.hw_to_var.get().strip()
        self._run_async(client.homework_for_period(date_from, date_to), self._fill_homeworks)

    def _fill_homeworks(self, hw_obj):
        self.hw_text.delete("1.0", tk.END)
        items = getattr(hw_obj, "payload", None) or getattr(hw_obj, "homeworks", None) or getattr(hw_obj, "items", [])
        if not items:
            self.hw_text.insert(tk.END, "Нет данных за выбранный период.")
            return

        blocks = []
        for item in items:
            title = safe_get(item, "subject_name", "title", "homework", default="Без предмета")
            dt = safe_get(item, "date", "created_at", default="—")
            text = safe_get(item, "homework", "description", "text", default="—")
            done = safe_get(item, "is_done", default="—")
            block = (
                f"Дата: {dt}\n"
                f"Предмет: {title}\n"
                f"Сделано: {done}\n"
                f"Задание: {text}\n"
                f"{'-' * 80}\n"
            )
            blocks.append(block)
        self.hw_text.insert(tk.END, "\n".join(blocks))

    def load_notifications(self):
        client = self.ensure_client()
        self._run_async(client.notifications(), self._fill_notifications)

    def _fill_notifications(self, notif_obj):
        self.notifications_text.delete("1.0", tk.END)
        items = getattr(notif_obj, "payload", None) or getattr(notif_obj, "items", None) or notif_obj
        if not items:
            self.notifications_text.insert(tk.END, "Нет уведомлений.")
            return

        if not isinstance(items, list):
            items = [items]

        for item in items:
            title = safe_get(item, "title", "event_name", "name", default="Уведомление")
            body = safe_get(item, "body", "text", "message", default="—")
            created = safe_get(item, "created_at", "date", default="—")
            self.notifications_text.insert(tk.END, f"[{created}] {title}\n{body}\n{'=' * 90}\n")


if __name__ == "__main__":
    app = MeshDesktopApp()
    app.mainloop()
