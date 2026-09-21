# Робо-дзвінки Travelon через Binotel

Бот реєструється в Binotel як звичайний внутрішній номер (SIP), сам набирає агента
з вашого номера, зачитує повідомлення українським TTS-голосом і чекає «1» для підтвердження.
Результат повертається в адмінку.

```
адмінка ──POST /call──▶ bot.py ──TTS──▶ wav ──AMI──▶ Asterisk ──SIP──▶ Binotel ──▶ агент
   ▲                                                                              │
   └───────────── callback {result: confirmed / noconfirm / noanswer / busy} ◀────┘
```

## 1. Binotel
1. My.binotel → Співробітники → створіть внутрішній номер, наприклад **900 «Робот»**.
2. Відкрийте його SIP-налаштування: сервер, логін, пароль.
3. Переконайтесь, що цьому номеру дозволені вихідні дзвінки з потрібного міського/мобільного номера
   (визначення номера буде як у всіх ваших співробітників).

## 2. VPS (Ubuntu 22.04/24.04, 1 vCPU / 1 GB достатньо)
```bash
git clone <цей репозиторій> && cd binotel-robocall
sudo ./install.sh
```
Потім:
- `/etc/asterisk/pjsip.conf` → впишіть `BINOTEL_SIP_HOST`, `BINOTEL_SIP_LOGIN`, `BINOTEL_SIP_PASSWORD`,
  `asterisk -rx "pjsip reload"`, перевірка: `asterisk -rx "pjsip show registrations"` → має бути **Registered**.
- `/opt/robocall/.env` → токени, callback-URL. Секрет AMI має збігатися з `manager.conf`.
- `systemctl start robocall`, лог: `journalctl -u robocall -f`.
- Поставте Caddy/nginx з HTTPS перед `127.0.0.1:8080`.
- Відкрийте на фаєрволі UDP 5060 та RTP 10000–20000 (див. `rtp.conf`).

## 3. Тест
```bash
curl -X POST https://robocall.travelon.to/call \
  -H "X-Token: <WEBHOOK_TOKEN>" -H "Content-Type: application/json" \
  -d '{"number":"0671234567","booking_id":"54443","urgent":true}'
```
Через кілька секунд телефон має задзвонити з номера Travelon.

## 4. Інтеграція з адмінкою
При події «коментар до заявки з позначкою терміново» адмінка робить той самий POST.
Поля: `number`, `booking_id`, `urgent`, необов'язково `text` (довільний текст замість шаблону).

Бот повертає в `ADMIN_CALLBACK_URL` JSON:
```json
{"id":"…","booking_id":"54443","number":"380671234567",
 "status":"done","result":"confirmed","attempts":1}
```
**Сценарій дзвінка:** агент зняв трубку → AMD перевіряє, чи це не автовідповідач →
«Це Тревелон, натисніть 1, щоб прослухати повідомлення» (двічі) → натиснув → текст по заявці.

| result      | значення                                                       |
|-------------|----------------------------------------------------------------|
| confirmed   | жива людина натиснула 1 і прослухала — фінал                   |
| nokey       | трубку зняли, 1 не натиснули (зайнятий / голосова пошта) — повтор |
| machine     | автовідповідач за AMD — повтор                                  |
| noanswer    | не взяв трубку — повтор через `RETRY_DELAY_SEC`                |
| busy        | зайнято — повтор                                                |
| hangup      | кинув трубку — повтор                                           |
| failed      | технічна помилка — повтор                                       |
| sms_sent    | після `MAX_ATTEMPTS` спроб пішло SMS через TurboSMS — фінал     |
| sms_failed  | і SMS не вдалося — зв'язатися вручну                            |

Дзвінки йдуть тільки в `WORK_HOURS`, решта чекають у черзі.

## Налаштування, які варто підкрутити
- Текст шаблону — константа `TEMPLATE` у `bot.py`.
- Голос: `uk-UA-PolinaNeural` (жіночий) / `uk-UA-OstapNeural` (чоловічий). Для преміум-якості
  замініть `make_wav` на Google Cloud TTS або ElevenLabs — решта коду не змінюється.
- Одночасно йде один дзвінок (черга). Якщо треба паралельно — запускайте `process()` через `asyncio.Semaphore`.

## 5. Безпека токенів
Токен адмінки, токен Telegram і секрет AMI **ніколи не пишуться в код і не пересилаються в чат**.
Вони живуть у `/opt/robocall/.env` з правами `600` (тільки root читає). У git цей файл не потрапляє —
у репозиторії лише `.env.example` з порожніми значеннями. Якщо токен колись «засвітиться» —
просто перевипускаєте його в адмінці й міняєте один рядок у `.env`.

## 6. Бот сам бере телефон із заявки
`POST /call/booking/54443` — бот іде в адмінку (`TRAVELON_BOOKING_URL`), витягує телефон агента
і дзвонить. Парсер розуміє і XML, і JSON; у `.env` вказуєте, як називаються поля з телефоном
та ім'ям агента. Коли надішлете приклад відповіді адмінки (з замазаними даними) —
допишемо точний розбір у `travelon.py`.

## 7. Telegram
1. @BotFather → новий бот → токен у `TELEGRAM_TOKEN`.
2. Напишіть боту `/start` — він покаже ID чату; додайте його в `TELEGRAM_ALLOWED_CHATS`
   (можна ID робочої групи). Чужі чати бот ігнорує.
3. Далі просто: **«прозвони заявку 54443»**, «подзвони 54443 не терміново» або `/call 54443`.
   Бот відповість, кому дзвонить, а потім — результат (підтвердив / не взяв / зайнято, повтор).

## 8. Docker: тест і прод однією командою
На будь-якому сервері з Docker (Ubuntu: `curl -fsSL https://get.docker.com | sh`):
```bash
git clone git@github.com:Legree/binotel-robocall.git && cd binotel-robocall
cp .env.example .env && chmod 600 .env   # вписати SIP-дані, токени
docker compose up -d --build
docker compose logs -f                   # чекаємо "AMI connected" і Registered
docker compose exec asterisk asterisk -rx "pjsip show registrations"
```
Переїзд з тестового сервера на бойовий = ті самі команди + скопіювати `.env`.
Оновлення після змін у коді: `git pull && docker compose up -d --build`.

Секрети (`.env`) у git не потрапляють — `.gitignore` це блокує. Репозиторій робіть приватним.

### Перший пуш у GitHub
```bash
cd binotel-robocall
git init && git add . && git commit -m "Binotel robocall bot"
git branch -M main
git remote add origin git@github.com:Legree/binotel-robocall.git   # спочатку створіть порожній приватний репо на github.com
git push -u origin main
```
