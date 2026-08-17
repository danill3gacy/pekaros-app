#!/usr/bin/env bash
# КофейняОС — установка на сервер одной командой.
#   bash install.sh
# Скрипт: ставит зависимости, создаёт окружение, готовит .env и службы systemd.
set -e

GREEN="\033[0;32m"; YELLOW="\033[1;33m"; RED="\033[0;31m"; NC="\033[0m"
say()  { echo -e "${GREEN}==>${NC} $1"; }
warn() { echo -e "${YELLOW}!${NC} $1"; }
fail() { echo -e "${RED}Ошибка:${NC} $1"; exit 1; }

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

say "Папка проекта: $DIR"

# ---------- 1. Python ----------
if ! command -v python3 >/dev/null 2>&1; then
  say "Устанавливаю Python…"
  if command -v apt >/dev/null 2>&1; then
    apt update -qq && apt install -y python3 python3-venv python3-pip
  else
    fail "Не нашёл python3. Установите его вручную и запустите скрипт снова."
  fi
fi
# На Ubuntu python3 есть всегда, а модуль venv — нет. Ставим при необходимости.
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  say "Доустанавливаю модуль venv…"
  if command -v apt >/dev/null 2>&1; then
    PYVER=$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    apt update -qq && apt install -y "python${PYVER}-venv" python3-venv python3-pip 2>/dev/null \
      || apt install -y python3-venv python3-pip
  else
    warn "Модуль venv недоступен — установите пакет python3-venv вручную."
  fi
fi
PYV=$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')
say "Python $PYV — ок"

# ---------- 2. Окружение и зависимости ----------
if [ ! -d venv ]; then
  say "Создаю окружение…"
  python3 -m venv venv || fail "Не удалось создать venv. На Ubuntu: sudo apt install python3-venv"
fi
say "Устанавливаю зависимости…"
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt
say "Зависимости установлены"

# ---------- 3. Настройки ----------
if [ ! -f .env ]; then
  cp .env.example .env
  say "Создан файл настроек .env"
fi

# абсолютный путь к базе — чтобы бот и сайт всегда открывали одну и ту же
if ! grep -q "^DB_PATH=" .env; then
  echo "DB_PATH=$DIR/coffeeos.db" >> .env
  say "Прописал путь к базе: $DIR/coffeeos.db"
fi

# пароль для сайта, если не задан
if ! grep -q "^WEB_PASSWORD=.\+" .env; then
  PASS=$(python3 -c "import secrets,string;print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(12)))")
  if grep -q "^WEB_PASSWORD=" .env; then
    sed -i.bak "s|^WEB_PASSWORD=.*|WEB_PASSWORD=$PASS|" .env && rm -f .env.bak
  else
    echo "WEB_PASSWORD=$PASS" >> .env
  fi
  say "Сгенерировал пароль для сайта: ${YELLOW}$PASS${NC} (логин: owner) — запишите его"
fi

# В .env лежат токен бота и пароль от сайта. По умолчанию файл создаётся с
# правами 644 — его мог прочитать любой пользователь сервера.
chmod 600 .env 2>/dev/null && say "Права на .env ограничены (600): токен и пароль закрыты"

# ---------- 4. Демо-данные ТОЛЬКО если база реально пуста ----------
# Путь к базе берём из настроек (он может отличаться от папки проекта),
# и никогда не трогаем базу, в которой уже есть чеки.
DBFILE=$(./venv/bin/python -c "from coffeeos import config;print(config.DB_PATH)" 2>/dev/null || echo "")
HASDATA=$(./venv/bin/python -c "
from coffeeos import db
try:
    c=db.get_conn(); n=c.execute('SELECT COUNT(*) FROM receipts').fetchone()[0]; c.close(); print(n)
except Exception: print(0)
" 2>/dev/null || echo 0)
if [ "${HASDATA:-0}" = "0" ]; then
  say "База пуста — заполняю демо-данными (заменятся при первой загрузке реальных чеков)…"
  ./venv/bin/python -m coffeeos seed >/dev/null
else
  say "В базе уже $HASDATA чеков ($DBFILE) — данные не трогаю"
fi

# ---------- 5. Службы systemd (автозапуск 24/7) ----------
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] && [ "$(id -u)" = "0" ]; then
  say "Настраиваю автозапуск (systemd)…"
  # Отдельный служебный пользователь: от root бот и сайт работать не должны —
  # это интернет-facing процессы, и любая их уязвимость станет правами root.
  SVC_USER=coffeeos
  if ! id -u "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER" 2>/dev/null \
      || useradd --system --no-create-home --shell /sbin/nologin "$SVC_USER" 2>/dev/null \
      || SVC_USER=""
  fi
  if [ -n "$SVC_USER" ]; then
    chown -R "$SVC_USER":"$SVC_USER" "$DIR" 2>/dev/null || true
    chmod 600 "$DIR/.env" 2>/dev/null || true
    say "Службы будут работать от пользователя $SVC_USER (не root)"
  else
    warn "Не удалось создать служебного пользователя — службы запустятся от root."
  fi
  for svc in bot web; do
    cat > /etc/systemd/system/coffeeos-$svc.service <<EOF
[Unit]
Description=CoffeeOS $svc
After=network.target

[Service]
${SVC_USER:+User=$SVC_USER}
${SVC_USER:+Group=$SVC_USER}
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python -m coffeeos $svc
Restart=always
RestartSec=5
# базовая изоляция: процессу нужна только своя папка
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=yes
ReadWritePaths=$DIR

[Install]
WantedBy=multi-user.target
EOF
  done
  systemctl daemon-reload
  say "Службы coffeeos-bot и coffeeos-web созданы"
  echo
  echo "Запустить их:  systemctl enable --now coffeeos-bot coffeeos-web"
else
  warn "systemd недоступен (или запущено не от root) — автозапуск не настроен."
  warn "Это нормально для проверки на своём компьютере."
fi

# ---------- 6. Проверка ----------
echo
say "Проверяю, что всё считается…"
./venv/bin/python -m coffeeos status || true

cat <<EOF

──────────────────────────────────────────────
 Установка завершена. Осталось:

 1. Впишите токен бота в файл .env  (строка BOT_TOKEN)
       nano .env
 2. Запустите бота:      ./venv/bin/python -m coffeeos bot
 3. Напишите боту /start — он покажет ваш ID
 4. Впишите ID в .env    (строка BRIEF_CHAT_IDS) и перезапустите
 5. Сайт:                ./venv/bin/python -m coffeeos web
       откройте http://АДРЕС-СЕРВЕРА:8000  (логин owner, пароль из .env)
       ⚠ Пароль идёт по открытому HTTP. Для боевого сервера настройте HTTPS
         (например, Nginx + Let's Encrypt) или ограничьте порт 8000 файрволом.

 Загрузить реальные чеки:
       ./venv/bin/python -m coffeeos import выгрузка.csv --reset

 Задать свои закупочные цены — 5 минут, без них не считается маржа:
       напишите боту «цены» — он покажет, чего не хватает,
       затем «цена зерно 1800», «цена молоко обычное 95» и так далее.
──────────────────────────────────────────────
EOF
