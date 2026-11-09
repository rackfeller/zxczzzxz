import logging
import random
import re
from datetime import datetime
import pytz
import config
from handlers import msgtext

log = logging.getLogger("ai_reply")

_openai = None

SWEAR_ON = (
    " В этом ответе вставь к месту лёгкий мат (например: блин, бля, пиздец, нахер, "
    "хрен его знает) — коротко и естественно, как в живой переписке. Не оскорбляй собеседника."
)
SWEAR_OFF = " В этом ответе не матерись."


def _swear_note() -> str:
    chance = config.SWEAR_CHANCE
    return SWEAR_ON if chance > 0 and random.random() * 100 < chance else SWEAR_OFF

# Добавляется к промпту, пока у Юли рабочее время (Пн-Пт, WORK_START–WORK_END по МСК)
WORK_NOTE = (
    " Ты сейчас на работе, сидишь, немного устала. "
    "Отвечай на вопрос собеседника по смыслу, как живой человек, и можешь вскользь упомянуть, "
    "что на работе. Например, на «как дела» ответь что-то вроде «Да нормально, на работе сижу, "
    "устала уже». НЕ пиши «освобожусь после 8» просто так. Говори это только если "
    "собеседник сам спросил, когда ты освободишься, или зовёт встретиться/созвониться."
)


def _get_client():
    global _openai
    if _openai is None:
        from openai import AsyncOpenAI  # импорт внутри: openai — необязательная зависимость
        _openai = AsyncOpenAI(
            api_key=config.OPENAI_API_KEY, 
            base_url="https://api.zveno.ai/v1", 
            timeout=30
        )
    return _openai


BATCH_NOTE = (
    " ВАЖНО: собеседник написал несколько сообщений подряд (каждое с новой строки). Прочитай их все "
    "вместе как один заход, пойми общий смысл и ответь ОДНОЙ короткой репликой на главное. "
    "Не отвечай на каждое по отдельности и не перечисляй, не путай, кто что сказал."
)


def merge_history(history: list[dict]) -> list[dict]:
    """Склеивает подряд идущие сообщения одного автора в одно (каждое с новой строки)."""
    merged: list[dict] = []
    for h in history:
        if merged and merged[-1]["role"] == h["role"]:
            merged[-1]["content"] += "\n" + h["content"]
        else:
            merged.append(dict(h))
    return merged


def looks_sane(text: str) -> bool:
    """Отсекает явный бред: код, разметку, длинные простыни, латиницу, зацикливание."""
    if not text or len(text) > 400:
        return False
    if "```" in text or "**" in text or "<" in text or "{" in text:
        return False
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", text)
    if len(letters) >= 8:
        latin = sum(1 for ch in letters if ch.isascii())
        if latin / len(letters) > 0.3:
            return False
    words = re.findall(r"\w+", text.lower())
    if len(words) >= 8 and len(set(words)) / len(words) < 0.4:   # одно и то же по кругу
        return False
    return True


def enabled() -> bool:
    return bool(config.OPENAI_API_KEY)


WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


def schedule_status(now: datetime) -> tuple[bool, str]:
    """Возвращает (на_работе, факт_для_ИИ) по московскому времени `now`.

    Модель сама не знает ни дня недели, ни времени, поэтому ей каждый раз сообщается готовый
    факт — иначе она придумывает («сегодня суббота, выходной» в понедельник)."""
    work_start = int(getattr(config, "WORK_START", 10))
    work_end = int(getattr(config, "WORK_END", 20))
    weekday = now.weekday()  # 0 - Пн, 6 - Вс
    head = (f"ТЕКУЩЕЕ ВРЕМЯ (факт, не придумывай другое): сегодня {WEEKDAYS[weekday]}, "
            f"{now:%d.%m.%Y}, сейчас {now:%H:%M} по Москве. Твой график: пн-пт с "
            f"{work_start}:00 до {work_end}:00, суббота и воскресенье выходные. ")
    if weekday >= 5:
        status = "Сегодня у тебя ВЫХОДНОЙ, ты не на работе."
        on_work = False
    elif work_start <= now.hour < work_end:
        status = f"Сегодня будний день, сейчас ты НА РАБОТЕ (до {work_end}:00)."
        on_work = True
    elif now.hour < work_start:
        status = f"Сегодня будний день, но смена ещё не началась (начинаешь в {work_start}:00), ты не на работе."
        on_work = False
    else:
        status = "Сегодня будний день, но рабочий день уже закончился: ты освободилась и НЕ на работе."
        on_work = False
    tail = (" Называй только этот день и это время. Не говори, что сегодня выходной, если он будний, "
            "и не говори, что ты на работе, если по расписанию это не так.")
    return on_work, head + status + tail


def check_work_schedule() -> bool:
    """Работает ли Юля сейчас (Пн-Пт, по умолчанию с 10 до 20 по МСК)."""
    return schedule_status(datetime.now(pytz.timezone("Europe/Moscow")))[0]


NO_WORK_NOTE = (
    " ВАЖНО: в этом ответе сама не заговаривай про работу, магазин и усталость. Но если "
    "собеседник спросил, где ты, чем занята, работаешь ли сегодня — ответь честно по расписанию "
    "выше. Если он сам рассказывает про свою работу — смело поддерживай эту тему."
)
# Собеседник отвечает на вопрос Юли («А ты?») и рассказывает про СЕБЯ — Юля должна это понять
LISTEN_NOTE = (
    " ПОНИМАНИЕ: сообщения собеседника — это про НЕГО, пока он не спросил про тебя. Если он "
    "рассказывает про свою работу, учёбу, дела или отвечает на твой вопрос (метка «[В ответ на "
    "твоё сообщение «…»]» значит, что он отвечает именно на твою фразу), то это его ответ о себе. "
    "Отреагируй на то, что он сказал: удивись, уточни деталь, поддержи или пошути по теме его "
    "рассказа. Не путай его работу со своей, не переспрашивай очевидное и не пересказывай, "
    "где работаешь ты, если он об этом не спрашивал."
)
WORK_WORDS = ("работ", "магазин", "консультант", "смен", "начальн", "клиент")
WORK_MENTION_LIMIT = 2      # столько раз за последние сообщения Юля уже могла сказать про работу
WORK_MENTION_CHANCE = 0.3   # и даже тогда — только в 30% ответов


def _work_mentions(messages) -> int:
    """Сколько последних сообщений Юли уже упоминали работу."""
    n = 0
    for m in messages:
        if m.out and any(w in (m.raw_text or "").lower() for w in WORK_WORDS):
            n += 1
    return n


# Тон добавляется ко ВСЕМ промптам (в т.ч. к своему AI_SYSTEM_PROMPT из .env) и имеет приоритет
TONE_NOTE = (
    " ТОН: ты дружелюбная и тёплая, говоришь мягко и с лёгким юмором. Не отвечай строго, сухо или "
    "резко, не огрызайся и не язви. Фразы вроде «чё тебе ещё надо», «отстань», «что ещё сказать» "
    "ЗАПРЕЩЕНЫ. На обычные вопросы (возраст, рост, чем занимаешься, как дела) отвечай спокойно и "
    "охотно, одной-двумя фразами, и иногда добавляй короткий встречный вопрос или шутку, чтобы "
    "разговор шёл. Даже если тема скучная, не отвечай односложно и лениво — сделай ответ живым. "
    "Никогда не поучай, не читай мораль и не осуждай собеседника: фразы вроде «скромнее надо быть», "
    "«веди себя прилично», «не позорься», «ты страшный» и любые оценки его внешности или поведения "
    "ЗАПРЕЩЕНЫ. Если он пошлит, грубит или подкатывает неумело — не злись и не обижайся: отшутись "
    "по-доброму, ответь лёгкой иронией («ахах ну ты даёшь», «смелое заявление)») или спокойно "
    "переведи тему вопросом. Обычно собеседник просто неловко шутит, и ты воспринимаешь это "
    "с юмором."
)

FORWARD_NOTE = (
    " Если сообщение собеседника начинается с «[Пересланное сообщение…]» — он переслал тебе чужое "
    "сообщение, фото или пост: отреагируй на него по смыслу, как живой человек. Метки в квадратных "
    "скобках ([фото], [голосовое сообщение], [В ответ на «…»]) — служебные, сама их не пиши."
)


async def generate(tg_client, chat_id: int, limit: int = 6) -> str | None:
    """Генерирует ответ по последним `limit` сообщениям диалога. При любой ошибке — None."""
    if not enabled():
        return None
        
    # --- ОБЫЧНАЯ РАБОТА ИИ ---
    try:
        messages = await tg_client.get_messages(chat_id, limit=max(20, limit))

        # Про работу — только иногда: не чаще пары раз за диалог и не в каждом ответе
        on_work, schedule_fact = schedule_status(datetime.now(pytz.timezone("Europe/Moscow")))
        mentions = _work_mentions(messages)
        if on_work and mentions < WORK_MENTION_LIMIT and random.random() < WORK_MENTION_CHANCE:
            work_note = WORK_NOTE
        else:
            work_note = NO_WORK_NOTE
        log.info("Упоминаний работы в последних сообщениях: %d, подсказка: %s",
                 mentions, "про работу" if work_note is WORK_NOTE else "без работы")
        system_prompt = (config.AI_SYSTEM_PROMPT + " " + schedule_fact + work_note + FORWARD_NOTE + LISTEN_NOTE + TONE_NOTE
                         + _swear_note())

        messages = messages[:max(4, limit)]   # get_messages отдаёт от новых к старым
        history = []
        for m in reversed(messages):  # от старых к новым
            content = await msgtext.describe(m)
            if content:
                history.append(
                    {"role": "assistant" if m.out else "user", "content": content}
                )
                
        if not history:
            return None

        history = merge_history(history)
        if history[-1]["role"] == "user" and "\n" in history[-1]["content"]:
            system_prompt += BATCH_NOTE

        resp = await _get_client().chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[{"role": "system", "content": system_prompt}] + history,
            max_tokens=150,        # ответ — 1-2 короткие фразы; длинный лимит давал простыни
            temperature=0.8,       # без ограничения модель иногда срывается в бред
            top_p=0.9,
        )
        
        # --- ДОБАВЛЕНА ОТЛАДКА ---
        log.info(f"Сырой ответ от сервера: {resp}")
        # -------------------------
        
        text = msgtext.strip_labels((resp.choices[0].message.content or "").strip())
        
        if not text:
            log.warning("ИИ вернул пустую строку! Сработает заготовка.")
            return None
        if not looks_sane(text):
            log.warning("ИИ вернул бред, ответ отброшен: %r", text[:200])
            return None
            
        log.info(f"ИИ успешно сгенерировал текст: {text}")
        return text

    except Exception as e:
        log.exception(f"Ошибка ИИ: {e}")
        return None