![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)

МЭШ Клиент в стиле 1С,
неоффициальное приложение МЭШ
=====================
<img width="1222" height="792" alt="python_MezD74F8f1" src="https://github.com/user-attachments/assets/6a41f5f6-c355-4124-bc84-d4317efec493" />

Что это:
- desktop-приложение на Python + Tkinter, клиент МЭШ для пк;
- показывает профиль, расписание, оценки, домашние задания и уведомления;
- использует библиотеку SchoolAPI-main (https://github.com/DavidZhivaev/SchoolAPI/tree/main);
- интерфейс стилизован ближе к 1С:Предприятие.

Как запустить:
1. Скачайте [последний релиз](https://github.com/janggl/1c-school/releases/tag/release-v0.0.5) к себе на ПК (предварительно установив Python).
2. Установите зависимости:
   pip install -r requirements.txt
3. Запустите:
   python app.py

Важно:
- в поле сверху нужно вставить актуальный токен mos.ru / МЭШ;
- если токен устарел, получите новый через кнопку в интерфейсе;
- для расписания и оценок сохраняются debug-файлы в папку debug/.
