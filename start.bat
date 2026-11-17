@echo off
chcp 866 >nul
rem Запуск демо-бота в один клик, Windows. Файл в кодировке CP866: cmd.exe читает её сразу.
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo [1/3] Создаю виртуальное окружение .venv ...
    python -m venv .venv
    if errorlevel 1 goto no_python
    .venv\Scripts\python.exe -m pip install --upgrade pip >nul 2>&1
    echo [2/3] Ставлю зависимости ...
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 goto no_pip
)

if not exist .env (
    copy /y .env.example .env >nul
    echo [3/3] Создан файл .env.
    echo Откройте его в блокноте, впишите BOT_TOKEN от @BotFather и ADMIN_ID от @userinfobot,
    echo сохраните файл и запустите start.bat снова.
    pause
    exit /b 0
)

echo Проверяю токен ...
.venv\Scripts\python.exe bot.py --check
if errorlevel 1 goto bad_token

echo Запускаю бота. Остановить: Ctrl+C
.venv\Scripts\python.exe bot.py
pause
exit /b 0

:bad_token
echo Токен не принят. Проверьте BOT_TOKEN в файле .env и запустите снова.
pause
exit /b 1

:no_python
echo Python не найден. Установите Python 3.10 или новее с python.org и запустите start.bat снова.
pause
exit /b 1

:no_pip
echo Не удалось установить зависимости. Проверьте интернет и запустите start.bat снова.
pause
exit /b 1