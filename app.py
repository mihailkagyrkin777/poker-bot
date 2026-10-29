import os
import json
import random
import re
import base64
from itertools import combinations
from datetime import datetime
import threading

from flask import Flask
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ============ НАСТРОЙКИ ============
TELEGRAM_TOKEN = "8963463637:AAGPOEESZLhzymp2icj42CObJmWvwDE8RO0"
PROXYAPI_KEY = "sk-R7psprKSZEdyFMl5bG2FwrkDmW0jCAUp"
YOUR_CHAT_ID = 1233078866

BASE_URL = "https://api.proxyapi.ru/v1"
MODEL = "openai/gpt-4o"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "telegram_bot_log.txt")
PLAYERS_FILE = os.path.join(BASE_DIR, "players.json")

client = OpenAI(api_key=PROXYAPI_KEY, base_url=BASE_URL)

# ============ КОЛОДА ============
RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', 'T', 'J', 'Q', 'K', 'A']
SUITS = ['s', 'h', 'd', 'c']
FULL_DECK = [r + s for r in RANKS for s in SUITS]
RANK_VALUE = {r: i for i, r in enumerate(RANKS)}


# ============ ПАРСЕР ДИАПАЗОНОВ ============
def parse_range(range_str):
    range_str = range_str.replace(" ", "")
    if not range_str:
        return []
    parts = range_str.split(",")
    normalized = []
    for p in parts:
        p = p.upper()
        p = p.replace("S", "s").replace("O", "o")
        normalized.append(p)
    hands = set()
    for part in normalized:
        part = part.strip()
        if not part:
            continue
        hands.update(_parse_single_token(part))
    return list(hands)


def _parse_single_token(token):
    if len(token) < 2:
        return []
    if token[0] == token[1] and token[0] in RANKS:
        return _parse_pair(token)
    return _parse_nonpair(token)


def _parse_pair(token):
    rank = token[0]
    if token.endswith("+"):
        start_idx = RANK_VALUE[rank]
        ranks = [RANKS[i] for i in range(start_idx, len(RANKS))]
        return _all_pairs_for_ranks(ranks)
    if "-" in token:
        low, high = token.split("-")
        low_idx = RANK_VALUE[low[0]]
        high_idx = RANK_VALUE[high[0]]
        ranks = [RANKS[i] for i in range(low_idx, high_idx + 1)]
        return _all_pairs_for_ranks(ranks)
    return _all_pairs_for_ranks([rank])


def _all_pairs_for_ranks(ranks):
    result = []
    for r in ranks:
        for s1, s2 in combinations(SUITS, 2):
            result.append(r + s1 + r + s2)
    return result


def _parse_nonpair(token):
    suited = None
    if token.endswith("s") or token.endswith("o"):
        suited = token[-1] == "s"
        token = token[:-1]
    if token.endswith("+"):
        token = token[:-1]
        if len(token) < 2:
            return []
        rank1 = token[0]
        rank2 = token[1]
        r1_val = RANK_VALUE[rank1]
        r2_val = RANK_VALUE[rank2]
        high_r2 = r1_val - 1
        ranks2 = [RANKS[i] for i in range(r2_val, high_r2 + 1)]
        result = []
        for r2 in ranks2:
            result.extend(_combos_for_pair_ranks(rank1, r2, suited))
        return result
    if len(token) < 2:
        return []
    rank1 = token[0]
    rank2 = token[1]
    return _combos_for_pair_ranks(rank1, rank2, suited)


def _combos_for_pair_ranks(rank1, rank2, suited):
    if RANK_VALUE[rank1] < RANK_VALUE[rank2]:
        rank1, rank2 = rank2, rank1
    result = []
    if suited is True or suited is None:
        for s in SUITS:
            result.append(rank1 + s + rank2 + s)
    if suited is False or suited is None:
        for s1 in SUITS:
            for s2 in SUITS:
                if s1 != s2:
                    result.append(rank1 + s1 + rank2 + s2)
    return result


# ============ ОЦЕНКА РУКИ ============
def find_straight(sorted_ranks):
    rank_set = set(sorted_ranks)
    if 12 in rank_set:
        rank_set.add(-1)
    ranks_list = sorted(rank_set, reverse=True)
    for i in range(len(ranks_list) - 4):
        window = ranks_list[i:i + 5]
        if window[0] - window[4] == 4 and len(set(window)) == 5:
            return window[0]
    return None


def evaluate_hand_7cards(cards):
    rank_order = {r: i for i, r in enumerate(RANKS)}
    parsed = [(c[:-1], c[-1]) for c in cards]
    ranks = [rank_order[r] for r, _ in parsed]
    suits = [s for _, s in parsed]

    rank_counts = {}
    for r in ranks:
        rank_counts[r] = rank_counts.get(r, 0) + 1
    suit_counts = {}
    for s in suits:
        suit_counts[s] = suit_counts.get(s, 0) + 1

    flush_suit = None
    for s, cnt in suit_counts.items():
        if cnt >= 5:
            flush_suit = s
            break

    if flush_suit is not None:
        flush_ranks = sorted(
            set(rank_order[r] for (r, s) in parsed if s == flush_suit),
            reverse=True
        )
        sf_high = find_straight(flush_ranks)
        if sf_high is not None:
            return (8, sf_high)

    quads = [r for r, c in rank_counts.items() if c == 4]
    if quads:
        qr = max(quads)
        kickers = sorted([r for r in ranks if r != qr], reverse=True)[:1]
        return (7, qr, *kickers)

    trips = sorted([r for r, c in rank_counts.items() if c == 3], reverse=True)
    pairs = sorted([r for r, c in rank_counts.items() if c == 2], reverse=True)
    if trips and (len(trips) >= 2 or pairs):
        tr = trips[0]
        pr = trips[1] if len(trips) >= 2 else pairs[0]
        return (6, tr, pr)

    if flush_suit is not None:
        flush_ranks = sorted(
            set(rank_order[r] for (r, s) in parsed if s == flush_suit),
            reverse=True
        )[:5]
        return (5, *flush_ranks)

    unique_ranks = sorted(set(ranks), reverse=True)
    straight_high = find_straight(unique_ranks)
    if straight_high is not None:
        return (4, straight_high)

    if trips:
        tr = trips[0]
        kickers = sorted([r for r in ranks if r != tr], reverse=True)[:2]
        return (3, tr, *kickers)

    if len(pairs) >= 2:
        top_two = pairs[:2]
        kickers = sorted([r for r in ranks if r not in top_two], reverse=True)[:1]
        return (2, *top_two, *kickers)

    if len(pairs) == 1:
        pr = pairs[0]
        kickers = sorted([r for r in ranks if r != pr], reverse=True)[:3]
        return (1, pr, *kickers)

    return (0, *sorted(ranks, reverse=True)[:5])


# ============ МОНТЕ-КАРЛО ============
def equity_vs_range(hero_cards, board, villain_range_str, num_simulations=3000):
    hero_cards = [c.replace("10", "T") for c in hero_cards]
    board = [c.replace("10", "T") for c in board]

    villain_hands = parse_range(villain_range_str)
    if not villain_hands:
        raise ValueError("Пустой диапазон")

    known_cards = list(hero_cards) + list(board)
    valid_villain_hands = []
    for vh in villain_hands:
        v_cards = [vh[0:2], vh[2:4]]
        if any(c in known_cards for c in v_cards):
            continue
        valid_villain_hands.append(v_cards)

    if not valid_villain_hands:
        raise ValueError("Все руки диапазона пересекаются")

    board_needed = 5 - len(board)
    wins = ties = losses = 0

    for _ in range(num_simulations):
        villain_cards = random.choice(valid_villain_hands)
        used = set(known_cards + villain_cards)
        deck = [c for c in FULL_DECK if c not in used]
        random.shuffle(deck)
        extra_board = deck[:board_needed]
        full_board = list(board) + extra_board

        hero_score = evaluate_hand_7cards(list(hero_cards) + full_board)
        villain_score = evaluate_hand_7cards(list(villain_cards) + full_board)

        if hero_score > villain_score:
            wins += 1
        elif hero_score == villain_score:
            ties += 1
        else:
            losses += 1

    total = num_simulations
    return (wins / total * 100, ties / total * 100, losses / total * 100)


def equity_vs_ranges_multi(hero_cards, board, villain_ranges, num_simulations=3000):
    hero_cards = [c.replace("10", "T") for c in hero_cards]
    board = [c.replace("10", "T") for c in board]

    known_cards = list(hero_cards) + list(board)

    valid_hands_per_villain = []
    for range_str in villain_ranges:
        hands = parse_range(range_str)
        if not hands:
            raise ValueError(f"Пустой диапазон: {range_str}")

        valid = []
        for vh in hands:
            v_cards = [vh[0:2], vh[2:4]]
            if any(c in known_cards for c in v_cards):
                continue
            valid.append(v_cards)
        if not valid:
            raise ValueError(f"Все руки {range_str} пересекаются")
        valid_hands_per_villain.append(valid)

    board_needed = 5 - len(board)
    wins = ties = losses = 0

    for _ in range(num_simulations):
        villain_cards_list = [random.choice(v) for v in valid_hands_per_villain]
        all_used = set(known_cards)
        for vc in villain_cards_list:
            all_used.update(vc)

        deck = [c for c in FULL_DECK if c not in all_used]
        random.shuffle(deck)
        extra_board = deck[:board_needed]
        full_board = list(board) + extra_board

        hero_score = evaluate_hand_7cards(list(hero_cards) + full_board)

        best_villain = None
        for vc in villain_cards_list:
            vs = evaluate_hand_7cards(list(vc) + full_board)
            if best_villain is None or vs > best_villain:
                best_villain = vs

        if hero_score > best_villain:
            wins += 1
        elif hero_score == best_villain:
            ties += 1
        else:
            losses += 1

    total = num_simulations
    return (wins / total * 100, ties / total * 100, losses / total * 100)


# ============ БАЗА ПРОФИЛЕЙ ============
def load_players_db():
    if os.path.exists(PLAYERS_FILE):
        try:
            with open(PLAYERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"profiles": {}}


def save_players_db(db):
    with open(PLAYERS_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def get_player_profile(name):
    db = load_players_db()
    return db.get("profiles", {}).get(name, None)


def update_player_action(name, action, amount=0, position=None):
    if not name or name == "Unknown":
        return

    db = load_players_db()
    profiles = db.setdefault("profiles", {})

    if name not in profiles:
        profiles[name] = {
            "hands": 0,
            "vpip": 0,
            "pfr": 0,
            "actions": [],
            "positions": [],
            "last_seen": None,
        }

    p = profiles[name]
    p["hands"] += 1
    p["last_seen"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    if action in ("raise", "call", "bet", "all-in"):
        p["vpip"] += 1
    if action in ("raise", "3bet", "all-in"):
        p["pfr"] += 1

    if position:
        p["positions"].append(position)

    p["actions"].append({
        "action": action,
        "amount": amount,
        "position": position,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })

    if len(p["actions"]) > 100:
        p["actions"] = p["actions"][-100:]

    save_players_db(db)


def get_player_stats(name):
    p = get_player_profile(name)
    if not p:
        return None

    hands = p.get("hands", 0)
    if hands == 0:
        return {"hands": 0}

    return {
        "hands": hands,
        "vpip": round(p.get("vpip", 0) / hands * 100, 1),
        "pfr": round(p.get("pfr", 0) / hands * 100, 1),
        "last_seen": p.get("last_seen"),
    }


def build_range_from_stats(stats):
    if not stats or stats.get("hands", 0) < 5:
        return None

    vpip = stats.get("vpip", 0)

    if vpip >= 60:
        return "22+,A2s+,K2s+,Q2s+,J2s+,T2s+,A2o+,K5o+,Q7o+,J7o+,T7o+"
    elif vpip >= 45:
        return "22+,A2s+,K5s+,Q7s+,J8s+,T8s+,A2o+,K9o+,Q9o+,J9o+,T9o+"
    elif vpip >= 35:
        return "22+,A2s+,K7s+,Q8s+,J8s+,T8s+,A7o+,K9o+,Q9o+,J9o+"
    elif vpip >= 25:
        return "22+,A2s+,K9s+,QTs+,JTs,A9o+,KTo+,QTo+"
    elif vpip >= 18:
        return "55+,A2s+,KTs+,QTs+,JTs,ATo+,KQo"
    elif vpip >= 12:
        return "TT+,AQs+,AKo"
    else:
        return "QQ+,AKs"


# ============ ПРЕСЕТЫ ============
RANGE_PRESETS = {
    "UTG": "TT+,AQs+,AKo",
    "MP": "77+,ATs+,KQs,AJo+,KQo",
    "CO": "22+,A2s+,KTs+,QTs+,JTs,ATo+,KQo",
    "BTN": "22+,A2s+,K7s+,Q8s+,J8s+,T8s+,ATo+,K9o+,Q9o+,J9o+",
    "SB 3bet": "QQ+,AKs,AKo",
    "BB call": "22-99,A2s-AQs,KTs+,QTs+,JTs,AJo+,KQo",
}

STREET_NAMES = {
    "preflop": "префлоп",
    "flop": "флоп",
    "turn": "тёрн",
    "river": "ривер",
}

STREET_CARDS_NEEDED = {
    "preflop": 3,
    "flop": 1,
    "turn": 1,
    "river": 0,
}

ACTION_ICONS = {
    "raise": "🟠",
    "call": "🔵",
    "fold": "⚫",
    "check": "🟡",
    "bet": "🔴",
    "all-in": "🟣",
}

ACTION_NAMES_RU = {
    "raise": "рейз",
    "call": "колл",
    "fold": "фолд",
    "check": "чек",
    "bet": "бет",
    "all-in": "олл-ин",
}


# ============ ЛОГИ ============
def write_log(text):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")


# ============ AI ============
def encode_image_bytes(image_bytes):
    return base64.b64encode(image_bytes).decode("utf-8")


def ask_model_about_table(image_bytes):
    img_b64 = encode_image_bytes(image_bytes)
    prompt = """You are a poker table recognition system. The interface may be in RUSSIAN or ENGLISH.
Analyze the image and return ONLY a JSON object.

Формат JSON:
{
  "hero_cards": ["As", "Qh"] or null,
  "board": ["Js", "Ks", "10d"],
  "pot": 450,
  "bet_to_call": 250,
  "num_players": 5,
  "players": [
    {"name": "Вася", "position": "BTN", "action": "raise", "amount": 250},
    {"name": "Петя", "position": "BB", "action": "call", "amount": 100}
  ]
}

ПРАВИЛА:
1. hero_cards — 2 карты ВНИЗУ. Смотри ВНИМАТЕЛЬНО на масти. Не выдумывай пару, если её нет.
2. board — 0, 3, 4 или 5 карт в центре.
3. pot — банк (число).
4. bet_to_call — ставка для колла (0 если нет).
5. num_players — общее количество игроков.
6. players — ВСЕ игроки. Для каждого:
   - name — ник КАК НАПИСАНО.
   - position — BTN, SB, BB, UTG, MP, HJ, CO. Если не видно — null.
   - action — raise / call / fold / check / bet / all-in / null.
   - amount — сумма (число). fold/check → 0.

7. КАРТЫ: ранг (A, K, Q, J, 10, 9-2) + масть (s, h, d, c). Десятка = "10".

ВАЖНО:
- Если на фото блик или тень, и ты не уверен в масти — смотри на цвет и форму.
- НЕ выдумывай карты. Если не видно — null.
- Ники — КАК ЕСТЬ. Действия — на английский.
- ОДНОЙ строкой JSON.

Только JSON."""

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
            ]
        }],
        max_tokens=1500,
    )
    return response.choices[0].message.content


def parse_json_response(text):
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        raise ValueError(f"Не найден JSON:\n{text}")

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError as e:
        raise ValueError(f"Некорректный JSON: {e}\n\n{text}")

    if "board" not in data or data["board"] is None:
        data["board"] = []
    if "pot" not in data or data["pot"] is None:
        data["pot"] = 0
    if "bet_to_call" not in data or data["bet_to_call"] is None:
        data["bet_to_call"] = 0
    if "players" not in data or data["players"] is None:
        data["players"] = []
    if "num_players" not in data or data["num_players"] is None:
        data["num_players"] = len(data["players"]) + 1

    normalized_players = []
    for p in data.get("players", []):
        if not isinstance(p, dict):
            continue
        normalized_players.append({
            "name": p.get("name") or "Unknown",
            "position": normalize_position(p.get("position")),
            "action": normalize_action(p.get("action")),
            "amount": float(p.get("amount") or 0),
        })
    data["players"] = normalized_players

    return data


def normalize_action(action):
    if not action:
        return None
    a = str(action).strip().lower()

    en_map = {
        "raise": "raise", "reraise": "raise", "re-raise": "raise",
        "3bet": "raise", "3-bet": "raise",
        "call": "call", "fold": "fold", "check": "check",
        "bet": "bet", "all-in": "all-in", "allin": "all-in", "all in": "all-in",
    }
    ru_map = {
        "рейз": "raise", "ререйз": "raise", "3-бет": "raise", "повышение": "raise",
        "колл": "call", "уравнять": "call", "уравнял": "call",
        "фолд": "fold", "пас": "fold", "сброс": "fold", "сбросил": "fold",
        "чек": "check",
        "бет": "bet", "ставка": "bet", "поставил": "bet",
        "олл-ин": "all-in", "оллин": "all-in", "ва-банк": "all-in",
    }
    return en_map.get(a) or ru_map.get(a)


def normalize_position(pos):
    if not pos:
        return None
    p = str(pos).strip().upper()
    mapping = {
        "BTN": "BTN", "BU": "BTN", "BUTTON": "BTN", "БТН": "BTN", "БАТТОН": "BTN",
        "SB": "SB", "СБ": "SB",
        "BB": "BB", "ББ": "BB",
        "UTG": "UTG", "УТГ": "UTG",
        "UTG+1": "UTG+1", "УТГ+1": "UTG+1",
        "UTG+2": "UTG+2",
        "MP": "MP", "МП": "MP", "MP+1": "MP+1",
        "HJ": "HJ", "ХДЖ": "HJ",
        "CO": "CO", "КО": "CO", "CUTOFF": "CO",
    }
    return mapping.get(p, p)


def fix_card(c):
    if c and c[0].upper() == "T":
        return "10" + c[1]
    return c


def fix_cards(cards):
    return [fix_card(c) for c in cards] if cards else []


# ============ СОСТОЯНИЕ ============
user_state = {}

# === БАЗА ЗНАНИЙ (КЭШ) ===
# Хранит результаты Монте-Карло: ключ -> (win, tie, lose)
simulation_memory = {}

def get_user_state(user_id):
    if user_id not in user_state:
        user_state[user_id] = {
            "hero": [], "board": [], "pot": 0, "bet": 0,
            "range": "QQ+,AK", "extra_ranges": [],
            "street": "preflop", "waiting_for": None,
            "last_analysis": None, "history": [],
            "bet_size": 0, "bet_fe": 0, "implied_winnings": 0,
            "table_players": [],
            "last_parsed_players": [],
            "pending_players": [],  # для подтверждения
            "setup_step": None, "setup_count": 0, "setup_index": 0,
            "editing_player": None,  # какого игрока правим
        }
    return user_state[user_id]


def get_all_ranges(state):
    return [state["range"]] + state.get("extra_ranges", [])


def reset_state(user_id):
    history = user_state.get(user_id, {}).get("history", [])
    table_players = user_state.get(user_id, {}).get("table_players", [])
    user_state[user_id] = {
        "hero": [], "board": [], "pot": 0, "bet": 0,
        "range": "QQ+,AK", "extra_ranges": [],
        "street": "preflop", "waiting_for": None,
        "last_analysis": None, "history": history,
        "bet_size": 0, "bet_fe": 0, "implied_winnings": 0,
        "table_players": table_players,
        "last_parsed_players": [],
        "pending_players": [],
        "setup_step": None, "setup_count": 0, "setup_index": 0,
        "editing_player": None,
    }


# ============ ФОРМАТИРОВАНИЕ ============
def build_players_summary(players):
    if not players:
        return "⚠️ Действия не распознаны."
    lines = []
    for i, p in enumerate(players, 1):
        name = p.get("name", "Unknown")
        action = p.get("action")
        amount = p.get("amount", 0)
        position = p.get("position")

        if not action:
            continue

        icon = ACTION_ICONS.get(action, "❓")
        action_ru = ACTION_NAMES_RU.get(action, action)
        pos_str = f" ({position})" if position else ""
        amount_str = f" {amount:.0f}" if amount > 0 else ""

        lines.append(f"{i}. {icon} {name}{pos_str}: {action_ru}{amount_str}")
    return "\n".join(lines) if lines else "⚠️ Действий не видно."


def build_reasoning(hero, board, pot, bet, win, tie, lose, equity,
                    ev_call, best_action, best_ev, ev_check, ev_bet,
                    bet_size, bet_fe, ev_implied=None, implied_winnings=0):
    reasons = []

    if equity >= 0.80:
        reasons.append(f"🔥 Очень сильная рука: эквити `{win:.1f}%`")
    elif equity >= 0.60:
        reasons.append(f"✅ Сильная рука: эквити `{win:.1f}%`")
    elif equity >= 0.45:
        reasons.append(f"⚖️ Средняя рука: эквити `{win:.1f}%`")
    elif equity >= 0.30:
        reasons.append(f"⚠️ Слабая рука: эквити `{win:.1f}%`")
    else:
        reasons.append(f"❌ Очень слабая рука: эквити `{win:.1f}%`")

    if bet > 0:
        pot_odds_needed = bet / (pot + bet) * 100
        reasons.append(f"📐 Пот-оддсы: нужно `{pot_odds_needed:.1f}%` для колла")
        if equity * 100 >= pot_odds_needed:
            reasons.append("✅ Эквити выше пот-оддсов → колл оправдан")
        else:
            reasons.append("❌ Эквити ниже пот-оддсов → обычный колл не окупается")
    else:
        reasons.append("💤 Оппонент не ставил — угрозы нет")

    if ev_implied is not None and bet > 0:
        if ev_implied > ev_call:
            reasons.append(
                f"📈 Implied Odds: с выигрышем `+{implied_winnings}` "
                f"EV колла = `{ev_implied:+.2f}` (выше обычного `{ev_call:+.2f}`)"
            )
            if ev_implied > 0 and ev_call < 0:
                reasons.append("🔥 Implied делает убыточный колл прибыльным — дро!")

    if bet > 0:
        if ev_bet is not None and ev_bet > ev_call and ev_bet > (ev_implied or -999):
            reasons.append(f"🎯 Бет FE `{bet_fe*100:.0f}%` → EV `{ev_bet:+.2f}` (лучше колла)")
            reasons.append("🔥 Бет выгоднее — используй фолд-эквити")
        elif ev_implied is not None and ev_implied > ev_call and ev_implied > 0:
            reasons.append(f"🔥 Колл с Implied прибыльный: EV `{ev_implied:+.2f}`")
        elif ev_call > 0:
            reasons.append(f"💰 Колл прибыльный: EV `{ev_call:+.2f}`")
        else:
            reasons.append(f"💸 Колл убыточный: EV `{ev_call:+.2f}`")
    else:
        if ev_bet is not None and ev_bet > (ev_check or 0):
            reasons.append(f"🎯 Бет FE `{bet_fe*100:.0f}%` → EV `{ev_bet:+.2f}` (лучше чека)")
        else:
            reasons.append(f"💰 Чек: EV `{(ev_check or 0):+.2f}`")

    if len(board) >= 3 and 0.25 < equity < 0.50:
        reasons.append("📌 Возможное дро — учитывай implied при ставках")

    return reasons


def format_analysis_message(hero, board, pot, bet, win, tie, lose, equity,
                            ev_call, best_action, best_ev, state,
                            ev_check=None, ev_bet=None, bet_size=0, bet_fe=0,
                            ev_implied=None, implied_winnings=0):
    ranges = get_all_ranges(state)
    num_opps = len(ranges)

    lines = []
    lines.append("📸 *РАСПОЗНАНО:*")
    lines.append(f"🎴 Твои карты: `{', '.join(hero) if hero else '—'}`")
    lines.append(f"🃏 Борд: `{', '.join(board) if board else '— (префлоп)'}`")
    lines.append(f"💰 Банк: `{pot:.0f}`")
    lines.append(f"🎯 Ставка: `{bet:.0f}`")
    lines.append("")

    players = state.get("last_parsed_players", [])
    if players:
        lines.append(f"👥 *ИГРОКОВ ЗА СТОЛОМ: {len(players)}*")
        actions_text = build_players_summary(players)
        lines.append(actions_text)
        lines.append("")

    lines.append(f"📊 *ОППОНЕНТОВ (диапазоны): {num_opps}*")
    for i, r in enumerate(ranges, 1):
        lines.append(f"  {i}. `{r}`")
    lines.append("")
    lines.append(f"📍 *Улица: {STREET_NAMES.get(state['street'], state['street'])}*")
    lines.append("")
    lines.append("📊 *ЭКВИТИ:*")
    lines.append(f"✅ Победа: `{win:.1f}%`")
    lines.append(f"⚖️ Ничья: `{tie:.1f}%`")
    lines.append(f"❌ Проигрыш: `{lose:.1f}%`")
    lines.append("")
    lines.append("💰 *EV РЕШЕНИЙ:*")
    lines.append("EV фолда: `+0.00`")
    if bet > 0:
        lines.append(f"EV колла: `{ev_call:+.2f}`")
    else:
        if ev_check is not None:
            lines.append(f"EV чека: `{ev_check:+.2f}`")
    if ev_bet is not None:
        lines.append(f"EV бета `{bet_size:.0f}` (FE={bet_fe*100:.0f}%): `{ev_bet:+.2f}`")
    if ev_implied is not None:
        lines.append(f"EV колла с implied (+{implied_winnings}): `{ev_implied:+.2f}`")
    lines.append("")

    reasoning = build_reasoning(
        hero, board, pot, bet, win, tie, lose, equity,
        ev_call, best_action, best_ev, ev_check, ev_bet, bet_size, bet_fe,
        ev_implied, implied_winnings
    )
    lines.append("🧠 *ОБОСНОВАНИЕ:*")
    for r in reasoning:
        lines.append(f"• {r}")
    lines.append("")

    if best_ev > 0:
        lines.append(f"✅ *ЛУЧШЕЕ: {best_action}* (EV = `{best_ev:+.2f}`)")
    else:
        lines.append(f"❌ *ЛУЧШЕЕ: {best_action}* (убыточно)")
    return "\n".join(lines)


def build_confirm_keyboard(players):
    """Клавиатура для подтверждения игроков."""
    keyboard = []
    for i, p in enumerate(players):
        name = p.get("name", "Unknown")
        keyboard.append([
            InlineKeyboardButton(f"✅ {name}", callback_data=f"confirm_{i}"),
            InlineKeyboardButton(f"✏️ {name}", callback_data=f"edit_{i}"),
        ])
    keyboard.append([
        InlineKeyboardButton("✅ ВСЁ ВЕРНО", callback_data="confirm_all"),
    ])
    return InlineKeyboardMarkup(keyboard)


def build_keyboard(state):
    street = state["street"]

    if street == "preflop":
        street_btn = InlineKeyboardButton("📤 ФЛОП", callback_data="street_add")
    elif street == "flop":
        street_btn = InlineKeyboardButton("📤 ТЁРН", callback_data="street_add")
    elif street == "turn":
        street_btn = InlineKeyboardButton("📤 РИВЕР", callback_data="street_add")
    else:
        street_btn = InlineKeyboardButton("✅ Конец", callback_data="quick_next")

    num_opps = len(get_all_ranges(state))

    keyboard = [
        [
            InlineKeyboardButton("🟠 RAISE", callback_data="action_raise"),
            InlineKeyboardButton("🔵 CALL", callback_data="action_call"),
        ],
        [
            InlineKeyboardButton("🟡 CHECK", callback_data="action_check"),
            InlineKeyboardButton("💵 БЕТ", callback_data="bet_open"),
        ],
        [
            InlineKeyboardButton("🔴 3-BET", callback_data="action_3bet"),
            InlineKeyboardButton("⚫ FOLD", callback_data="action_fold"),
        ],
        [
            InlineKeyboardButton("📈 Implied", callback_data="implied_open"),
            InlineKeyboardButton("📜 История", callback_data="quick_history"),
        ],
        [
            InlineKeyboardButton(f"➕ Оппонент ({num_opps})", callback_data="opp_add"),
            InlineKeyboardButton("➖ Убрать", callback_data="opp_remove"),
        ],
        [
            InlineKeyboardButton("🔄 Сброс", callback_data="action_reset"),
            street_btn,
        ],
        [
            InlineKeyboardButton("👥 Игроки", callback_data="show_players"),
            InlineKeyboardButton("📸 Ещё фото", callback_data="quick_next"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def calculate_and_format(state):
    hero = state["hero"]
    board = state["board"]
    pot = state["pot"]
    bet = state["bet"]
    ranges = get_all_ranges(state)

    # === ПРОВЕРКА ПАМЯТИ (КЭШ) ===
    cache_key = (
        tuple(sorted(hero)), 
        tuple(sorted(board)), 
        tuple(sorted(ranges))
    )

    if cache_key in simulation_memory:
        # Берем из памяти (мгновенно)
        win, tie, lose = simulation_memory[cache_key]
        print(f"⚡ CACHE HIT: {cache_key}")
    else:
        # Считаем (0.5-1 сек)
        if len(ranges) == 1:
            win, tie, lose = equity_vs_range(hero, board, ranges[0], num_simulations=3000)
        else:
            win, tie, lose = equity_vs_ranges_multi(hero, board, ranges, num_simulations=3000)
        
        # Записываем в память
        simulation_memory[cache_key] = (win, tie, lose)
        print(f"💾 CACHE MISS: {cache_key} (saved)")
        
        # Ограничиваем память (чтобы не забить оперативку)
        if len(simulation_memory) > 500:
            oldest_key = next(iter(simulation_memory))
            del simulation_memory[oldest_key]

    equity = win / 100 + (tie / 100) / 2
    ev_fold = 0
    ev_call = (equity * (pot + bet)) - bet if bet > 0 else equity * pot
    ev_check = equity * pot

    ev_bet = None
    bet_size = state.get("bet_size", 0)
    bet_fe = state.get("bet_fe", 0)

    if bet_size > 0 and bet_fe > 0:
        ev_when_called = (equity * (pot + bet_size)) - ((1 - equity) * bet_size)
        ev_bet = (bet_fe * pot) + ((1 - bet_fe) * ev_when_called)

    implied_winnings = state.get("implied_winnings", 0)
    ev_implied = None
    if implied_winnings > 0 and bet > 0:
        ev_implied = (equity * (pot + bet + implied_winnings)) - bet

    options = [("ФОЛД", ev_fold)]
    if bet > 0:
        options.append(("КОЛЛ", ev_call))
    else:
        options.append(("ЧЕК", ev_check))
    if ev_bet is not None:
        options.append((f"БЕТ {bet_size:.0f} (FE={bet_fe*100:.0f}%)", ev_bet))
    if ev_implied is not None:
        options.append((f"КОЛЛ (implied +{implied_winnings})", ev_implied))

    best_action, best_ev = max(options, key=lambda x: x[1])

    text = format_analysis_message(
        hero, board, pot, bet, win, tie, lose, equity,
        ev_call, best_action, best_ev, state,
        ev_check=ev_check, ev_bet=ev_bet,
        bet_size=bet_size, bet_fe=bet_fe,
        ev_implied=ev_implied, implied_winnings=implied_winnings
    )
    return text, win, best_action, build_keyboard(state)


# ============ КОМАНДЫ ============
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎴 ПОКЕР-СОВЕТНИК v3\n\n"
        "Возможности:\n"
        "📸 Фото → AI распознаёт игроков и действия\n"
        "✅ Подтверждение действий кнопками\n"
        "📊 Профили игроков (VPIP, PFR)\n"
        "🎯 Автоматические диапазоны\n"
        "📤 Мульти-улица, 💵 FE, 📈 Implied\n\n"
        "Команды:\n"
        "/setup — настроить стол\n"
        "/players — активные игроки\n"
        "/profile Ник — профиль\n"
        "/range — диапазон\n"
        "/presets — пресеты\n"
        "/manual — ручной ввод\n"
        "/reset — сброс"
    )
    await update.message.reply_text(text)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 ПОМОЩЬ\n\n"
        "1. /setup — настрой стол\n"
        "2. Отправь фото раздачи\n"
        "3. Подтверди действия игроков (✅ или ✏️)\n"
        "4. Получи анализ\n\n"
        "Профили: /players, /profile Ник\n"
        "FE: 💵 БЕТ, Implied: 📈 Implied"
    )
    await update.message.reply_text(text)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_state(update.effective_user.id)
    await update.message.reply_text("🔄 Раздача сброшена.")


async def cmd_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Пример: /range TT+,AK")
        return
    new_range = " ".join(context.args)
    user_id = update.effective_user.id
    state = get_user_state(user_id)
    try:
        hands = parse_range(new_range)
        if not hands:
            raise ValueError("Пусто")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        return
    state["range"] = new_range
    await update.message.reply_text(f"✅ Диапазон: {new_range}")


async def cmd_presets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    for name in RANGE_PRESETS.keys():
        keyboard.append([InlineKeyboardButton(
            f"{name}: {RANGE_PRESETS[name][:22]}...",
            callback_data=f"preset_{name}"
        )])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="preset_cancel")])
    await update.message.reply_text(
        "🎯 Выбери пресет:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✍️ РУЧНОЙ ВВОД\n\nкарты | борд | банк | ставка\n\n"
        "Пример: Ah As | Js Ks Td | 100 | 50"
    )


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_user_state(user_id)

    keyboard = [
        [
            InlineKeyboardButton("2", callback_data="setup_count_2"),
            InlineKeyboardButton("3", callback_data="setup_count_3"),
            InlineKeyboardButton("4", callback_data="setup_count_4"),
        ],
        [
            InlineKeyboardButton("5", callback_data="setup_count_5"),
            InlineKeyboardButton("6", callback_data="setup_count_6"),
            InlineKeyboardButton("7", callback_data="setup_count_7"),
        ],
        [
            InlineKeyboardButton("8", callback_data="setup_count_8"),
            InlineKeyboardButton("9", callback_data="setup_count_9"),
        ],
        [
            InlineKeyboardButton("❌ Отмена", callback_data="setup_cancel"),
        ],
    ]
    state["setup_step"] = "count"
    await update.message.reply_text(
        "🎰 *НАСТРОЙКА СТОЛА*\n\n"
        "Сколько ВСЕГО игроков (включая тебя)?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_user_state(user_id)
    table_players = state.get("table_players", [])

    if not table_players:
        await update.message.reply_text("⚠️ Стол не настроен. Используй /setup")
        return

    text = f"👥 *АКТИВНЫЕ ИГРОКИ ({len(table_players)}):*\n\n"
    for i, name in enumerate(table_players, 1):
        stats = get_player_stats(name)
        if stats and stats.get("hands", 0) > 0:
            text += (
                f"{i}. *{name}*\n"
                f"   Раздач: {stats['hands']}\n"
                f"   VPIP: {stats.get('vpip', 0)}% | PFR: {stats.get('pfr', 0)}%\n\n"
            )
        else:
            text += f"{i}. *{name}* — нет данных\n\n"

    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Пример: /profile Вася")
        return

    name = " ".join(context.args)
    stats = get_player_stats(name)

    if not stats:
        await update.message.reply_text(f"⚠️ Игрок {name} не найден.")
        return

    if stats.get("hands", 0) == 0:
        await update.message.reply_text(f"⚠️ По {name} нет данных.")
        return

    rng = build_range_from_stats(stats)
    text = (
        f"📊 *ПРОФИЛЬ: {name}*\n"
        f"─────────────────\n"
        f"Раздач: {stats['hands']}\n"
        f"VPIP: {stats.get('vpip', 0)}%\n"
        f"PFR: {stats.get('pfr', 0)}%\n"
        f"Последний раз: {stats.get('last_seen', '—')}\n\n"
    )
    if rng:
        text += f"🎯 *Примерный диапазон:*\n`{rng}`"

    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_user_state(update.effective_user.id)
    text = (
        "📊 СТАТИСТИКА\n\n"
        f"Диапазон: {state['range']}\n"
        f"Улица: {STREET_NAMES.get(state['street'])}\n"
        f"История: {len(state.get('history', []))} раздач"
    )
    await update.message.reply_text(text)


# ============ ФОТО ============
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_user_state(user_id)

    msg = await update.message.reply_text(
        "📸 Фото получено. Распознаю...\n⏳ 5-10 секунд."
    )

    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        raw = ask_model_about_table(bytes(image_bytes))
        data = parse_json_response(raw)

        hero = fix_cards(data.get("hero_cards") or [])
        board = fix_cards(data.get("board") or [])
        pot = float(data.get("pot") or 0)
        bet = float(data.get("bet_to_call") or 0)
        players = data.get("players", [])

           # === УМНЫЙ СБРОС БОРДА ===
        old_board = state.get("board", [])
        old_hero = state.get("hero", [])

        # Проверяем: это новая раздача?
        is_new_hand = False

        # 1. Если новый борд пустой (префлоп), а старый был не пустой
        if len(board) == 0 and len(old_board) > 0:
            is_new_hand = True

        # 2. Если карты на борде полностью другие (не совпадают с продолжением)
        elif len(board) > 0 and len(old_board) > 0:
            # Проверяем, является ли новый борд продолжением старого
            # (первые len(old_board) карт должны совпадать)
            if board[:len(old_board)] != old_board:
                is_new_hand = True

        # 3. Если твои карты изменились (новая раздача)
        if hero != old_hero and len(old_hero) > 0:
            is_new_hand = True

        if is_new_hand:
            # Сбрасываем только игровые данные, профили игроков оставляем
            table_players = state.get("table_players", [])
            state = {
                "hero": [], "board": [], "pot": 0, "bet": 0,
                "range": "QQ+,AK", "extra_ranges": [],
                "street": "preflop", "waiting_for": None,
                "last_analysis": None, 
                "history": state.get("history", []),
                "bet_size": 0, "bet_fe": 0, "implied_winnings": 0,
                "table_players": table_players,
                "last_parsed_players": [],
                "pending_players": [],
                "setup_step": None, "setup_count": 0, "setup_index": 0,
                "editing_player": None,
            }
            write_log(f"USER {user_id} NEW HAND detected. Auto-reset.")

        # Обновляем состояние новыми данными
        state["hero"] = hero
        state["board"] = board
        state["pot"] = pot
        state["bet"] = bet
        state["bet_size"] = 0
        state["bet_fe"] = 0
        state["implied_winnings"] = 0
        state["last_parsed_players"] = players
        state["pending_players"] = players

        # Считаем анализ (включая текущие диапазоны)
        text, win, best_action, keyboard = calculate_and_format(state)

        # Добавляем подтверждение действий
        if players:
            text += "\n\n━━━━━━━━━━━━━━━━━━━━\n"
            text += "⚠️ *ПОДТВЕРДИ ДЕЙСТВИЯ:*\n"
            text += "(профили обновятся после подтверждения)"
            confirm_kb = build_confirm_keyboard(players)
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=confirm_kb)
        else:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

        state["history"].append(
            f"`{' '.join(hero)}` | `{' '.join(board) if board else '—'}` "
            f"| Победа: `{win:.1f}%` | {best_action}"
        )

    except Exception as e:
        write_log(f"USER {user_id} ERR: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")


# ============ ТЕКСТ ============
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_user_state(user_id)
    text = update.message.text.strip()

    # Настройка стола
    if state.get("setup_step") == "names":
        try:
            name = text.strip()
            if not name:
                await update.message.reply_text("⚠️ Введи ник")
                return

            table_players = state.get("table_players", [])
            table_players.append(name)
            state["table_players"] = table_players
            state["setup_index"] += 1

            total_needed = state.get("setup_count", 1) - 1

            if state["setup_index"] >= total_needed:
                state["setup_step"] = None
                players_list = "\n".join(
                    f"{i+1}. {n}" for i, n in enumerate(table_players)
                )
                await update.message.reply_text(
                    f"✅ *Стол настроен:*\n{players_list}\n\n"
                    f"Отправь фото.",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(
                    f"✅ {name} добавлен.\n"
                    f"Игрок #{state['setup_index'] + 1} "
                    f"(осталось {total_needed - state['setup_index']}):"
                )
            return
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
            return

    # Улицы
    if state.get("waiting_for") == "street_cards":
        try:
            cards = fix_cards(text.split())
            expected = STREET_CARDS_NEEDED.get(state["street"], 0)

            if len(cards) != expected:
                await update.message.reply_text(f"⚠️ Нужно {expected} карт(ы).")
                return

            all_cards = state["hero"] + state["board"] + cards
            dupes = [c for c in set(all_cards) if all_cards.count(c) > 1]
            if dupes:
                await update.message.reply_text(f"❌ Дубли: {', '.join(dupes)}")
                return

            state["board"].extend(cards)
            state["waiting_for"] = None

            if len(state["board"]) == 3:
                state["street"] = "flop"
            elif len(state["board"]) == 4:
                state["street"] = "turn"
            elif len(state["board"]) == 5:
                state["street"] = "river"

            analysis, win, best_action, keyboard = calculate_and_format(state)
            await update.message.reply_text(
                analysis, parse_mode="Markdown", reply_markup=keyboard
            )
            return
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
            return

    # Ручной ввод
    if "|" not in text:
        await update.message.reply_text(
            "⚠️ Отправь фото, или: Ah As | Js Ks Td | 100 | 50"
        )
        return

    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 4:
        await update.message.reply_text("⚠️ Нужно 4 части: карты | борд | банк | ставка")
        return

    try:
        hero = fix_cards(parts[0].split())
        board = fix_cards(parts[1].split()) if parts[1] else []
        pot = float(parts[2] or 0)
        bet = float(parts[3] or 0)

        if len(hero) != 2:
            raise ValueError("Нужно 2 карты")

        state["hero"] = hero
        state["board"] = board
        state["pot"] = pot
        state["bet"] = bet

        if len(board) == 0:
            state["street"] = "preflop"
        elif len(board) == 3:
            state["street"] = "flop"
        elif len(board) == 4:
            state["street"] = "turn"
        else:
            state["street"] = "river"

        analysis, win, best_action, keyboard = calculate_and_format(state)
        await update.message.reply_text(
            analysis, parse_mode="Markdown", reply_markup=keyboard
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


# ============ КНОПКИ ============
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    state = get_user_state(user_id)
    data = query.data

    # === Подтверждение действий ===
    if data == "confirm_all":
        players = state.get("pending_players", [])
        if not players:
            await query.edit_message_text("⚠️ Нет действий для подтверждения.")
            return

        confirmed = 0
        for p in players:
            name = p.get("name", "Unknown")
            action = p.get("action")
            amount = p.get("amount", 0)
            position = p.get("position")

            if action:
                update_player_action(name, action, amount, position)
                confirmed += 1

        state["pending_players"] = []

        try:
            analysis, win, best_action, keyboard = calculate_and_format(state)
            await query.edit_message_text(
                f"✅ *Обновлено профилей: {confirmed}*\n\n{analysis}",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")
        return

    if data.startswith("confirm_"):
        try:
            idx = int(data.replace("confirm_", ""))
            players = state.get("pending_players", [])
            if idx >= len(players):
                await query.answer("⚠️ Неверный индекс")
                return

            p = players[idx]
            name = p.get("name", "Unknown")
            action = p.get("action")
            amount = p.get("amount", 0)
            position = p.get("position")

            if action:
                update_player_action(name, action, amount, position)
                await query.answer(f"✅ {name}: {action} записан")
            else:
                await query.answer(f"⚠️ {name}: действие не распознано")

            # Перестраиваем клавиатуру без подтверждённого
            players.pop(idx)
            state["pending_players"] = players

            if players:
                confirm_kb = build_confirm_keyboard(players)
                await query.edit_message_reply_markup(reply_markup=confirm_kb)
            else:
                await query.edit_message_text(
                    "✅ Все действия подтверждены."
                )
            return
        except Exception as e:
            await query.answer(f"Ошибка: {e}")
            return

    if data.startswith("edit_"):
        try:
            idx = int(data.replace("edit_", ""))
            players = state.get("pending_players", [])
            if idx >= len(players):
                return

            p = players[idx]
            name = p.get("name", "Unknown")

            keyboard = [
                [InlineKeyboardButton("RAISE", callback_data=f"set_{idx}_raise")],
                [InlineKeyboardButton("CALL", callback_data=f"set_{idx}_call")],
                [InlineKeyboardButton("FOLD", callback_data=f"set_{idx}_fold")],
                [InlineKeyboardButton("CHECK", callback_data=f"set_{idx}_check")],
                [InlineKeyboardButton("BET", callback_data=f"set_{idx}_bet")],
                [InlineKeyboardButton("ALL-IN", callback_data=f"set_{idx}_all-in")],
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel_edit")],
            ]

            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        except Exception as e:
            await query.answer(f"Ошибка: {e}")
            return

    if data.startswith("set_"):
        try:
            parts = data.replace("set_", "").split("_", 1)
            idx = int(parts[0])
            new_action = parts[1]

            players = state.get("pending_players", [])
            if idx >= len(players):
                return

            p = players[idx]
            name = p.get("name", "Unknown")
            old_amount = p.get("amount", 0)

            # Обновляем действие
            p["action"] = new_action
            if new_action in ("fold", "check"):
                p["amount"] = 0
            state["pending_players"][idx] = p

            # Записываем в БД
            update_player_action(name, new_action, old_amount, p.get("position"))

            # Убираем из pending
            players.pop(idx)
            state["pending_players"] = players

            await query.answer(f"✅ {name}: {new_action}")

            if players:
                confirm_kb = build_confirm_keyboard(players)
                await query.edit_message_reply_markup(reply_markup=confirm_kb)
            else:
                await query.edit_message_text("✅ Все действия подтверждены.")
            return
        except Exception as e:
            await query.answer(f"Ошибка: {e}")
            return

    if data == "cancel_edit":
        players = state.get("pending_players", [])
        if players:
            confirm_kb = build_confirm_keyboard(players)
            await query.edit_message_reply_markup(reply_markup=confirm_kb)
        else:
            await query.edit_message_text("❌ Правка отменена.")
        return

    # === Настройка стола ===
    if data.startswith("setup_count_"):
        count = int(data.replace("setup_count_", ""))
        state["setup_count"] = count
        state["setup_index"] = 0
        state["table_players"] = []
        state["setup_step"] = "names"

        await query.edit_message_text(
            f"✅ Игроков: {count}\n"
            f"Введи ник игрока #1 (без тебя):",
            parse_mode="Markdown"
        )
        return

    if data == "setup_cancel":
        state["setup_step"] = None
        await query.edit_message_text("❌ Настройка отменена.")
        return

    if data == "show_players":
        table_players = state.get("table_players", [])
        if not table_players:
            await query.edit_message_text("⚠️ Стол не настроен. /setup")
            return

        text = f"👥 АКТИВНЫЕ ИГРОКИ ({len(table_players)}):\n\n"
        for i, name in enumerate(table_players, 1):
            stats = get_player_stats(name)
            if stats and stats.get("hands", 0) > 0:
                text += (
                    f"{i}. {name}\n"
                    f"   Раздач: {stats['hands']} | "
                    f"VPIP: {stats.get('vpip', 0)}%\n"
                )
            else:
                text += f"{i}. {name} — нет данных\n"

        keyboard = [
            [InlineKeyboardButton("➖ Кто ушёл?", callback_data="player_remove")],
            [InlineKeyboardButton("➕ Новый игрок", callback_data="player_add")],
            [InlineKeyboardButton("↩️ Назад", callback_data="quick_next")],
        ]
        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data == "player_remove":
        table_players = state.get("table_players", [])
        if not table_players:
            await query.edit_message_text("⚠️ Нет игроков.")
            return

        keyboard = []
        for name in table_players:
            keyboard.append([InlineKeyboardButton(
                name, callback_data=f"remove_{name}"
            )])
        keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="show_players")])

        await query.edit_message_text(
            "Кто ушёл со стола?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data.startswith("remove_"):
        name = data.replace("remove_", "")
        table_players = state.get("table_players", [])
        if name in table_players:
            table_players.remove(name)
            state["table_players"] = table_players
            await query.edit_message_text(
                f"✅ {name} убран.\nОсталось: {len(table_players)} игроков."
            )
        return

    if data == "player_add":
        state["setup_step"] = "names"
        state["setup_index"] = state.get("setup_count", 1) - 1
        await query.edit_message_text(
            "Введи ник нового игрока:\n_Напиши ник в чат_",
            parse_mode="Markdown"
        )
        return

    # === Быстрые ===
    if data == "quick_next":
        await query.edit_message_text("📸 Жду новое фото.")
        return

    if data == "quick_history":
        history = state.get("history", [])
        if not history:
            await query.edit_message_text("📜 История пуста.")
            return
        text = "📜 ПОСЛЕДНИЕ РАЗДАЧИ:\n\n"
        for i, h in enumerate(history[-5:], 1):
            text += f"{i}. {h}\n\n"
        await query.edit_message_text(text, parse_mode="Markdown")
        return

    # === Оппоненты ===
    if data == "opp_add":
        state["waiting_for"] = "add_opponent"
        await query.edit_message_text(
            "➕ Отправь диапазон нового оппонента.\nПример: TT+,AK"
        )
        return

    if data == "opp_remove":
        if not state.get("extra_ranges"):
            await query.edit_message_text("⚠️ Нет доп. оппонентов.")
            return
        removed = state["extra_ranges"].pop()
        total = len(get_all_ranges(state))
        await query.edit_message_text(
            f"➖ Убран: {removed}\nОппонентов: {total}"
        )
        return

    # === Улицы ===
    if data == "street_add":
        street = state["street"]
        needed = STREET_CARDS_NEEDED.get(street, 0)
        if needed == 0:
            await query.edit_message_text("⚠️ Уже ривер.")
            return
        state["waiting_for"] = "street_cards"
        hint = "Ah Kd 7s" if needed == 3 else "Ah"
        await query.edit_message_text(
            f"📤 Отправь {needed} карт(ы).\nПример: {hint}"
        )
        return

    # === БЕТ ===
    if data == "bet_open":
        keyboard = [
            [
                InlineKeyboardButton("1/3", callback_data="bet_size_033"),
                InlineKeyboardButton("1/2", callback_data="bet_size_050"),
            ],
            [
                InlineKeyboardButton("3/4", callback_data="bet_size_075"),
                InlineKeyboardButton("Пот", callback_data="bet_size_100"),
            ],
            [
                InlineKeyboardButton("✏️ Свой", callback_data="bet_size_custom"),
                InlineKeyboardButton("❌", callback_data="bet_cancel"),
            ],
        ]
        await query.edit_message_text(
            "💵 Размер ставки:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data == "bet_cancel":
        try:
            analysis, win, best_action, keyboard = calculate_and_format(state)
            await query.edit_message_text(
                analysis, parse_mode="Markdown", reply_markup=keyboard
            )
        except Exception as e:
            await query.edit_message_text(f"❌ {e}")
        return

    if data.startswith("bet_size_"):
        size_code = data.replace("bet_size_", "")
        if size_code == "custom":
            state["waiting_for"] = "bet_size_custom"
            await query.edit_message_text("✏️ Отправь размер (число).")
            return

        pot = state["pot"]
        multiplier = int(size_code) / 100
        bet_size = round(pot * multiplier)
        state["bet_size"] = bet_size

        keyboard = [
            [
                InlineKeyboardButton("20%", callback_data="bet_fe_20"),
                InlineKeyboardButton("40%", callback_data="bet_fe_40"),
            ],
            [
                InlineKeyboardButton("60%", callback_data="bet_fe_60"),
                InlineKeyboardButton("80%", callback_data="bet_fe_80"),
            ],
            [
                InlineKeyboardButton("✏️ FE", callback_data="bet_fe_custom"),
                InlineKeyboardButton("❌", callback_data="bet_cancel"),
            ],
        ]
        await query.edit_message_text(
            f"💵 Ставка: {bet_size}\n\n🎯 Fold Equity:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data.startswith("bet_fe_"):
        fe_code = data.replace("bet_fe_", "")
        if fe_code == "custom":
            state["waiting_for"] = "bet_fe_custom"
            await query.edit_message_text("✏️ FE в % (0-100).")
            return

        fe = int(fe_code) / 100
        state["bet_fe"] = fe
        state["waiting_for"] = None

        try:
            analysis, win, best_action, keyboard = calculate_and_format(state)
            await query.edit_message_text(
                analysis, parse_mode="Markdown", reply_markup=keyboard
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")
        return

    # === Implied ===
    if data == "implied_open":
        keyboard = [
            [
                InlineKeyboardButton("50", callback_data="implied_set_50"),
                InlineKeyboardButton("100", callback_data="implied_set_100"),
            ],
            [
                InlineKeyboardButton("200", callback_data="implied_set_200"),
                InlineKeyboardButton("500", callback_data="implied_set_500"),
            ],
            [
                InlineKeyboardButton("✏️ Свой", callback_data="implied_set_custom"),
                InlineKeyboardButton("❌", callback_data="bet_cancel"),
            ],
        ]
        await query.edit_message_text(
            "📈 Implied — сколько выиграешь дополнительно?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data.startswith("implied_set_"):
        code = data.replace("implied_set_", "")
        if code == "custom":
            state["waiting_for"] = "implied_custom"
            await query.edit_message_text("✏️ Отправь число.")
            return

        amount = float(code)
        state["implied_winnings"] = amount

        try:
            analysis, win, best_action, keyboard = calculate_and_format(state)
            await query.edit_message_text(
                analysis, parse_mode="Markdown", reply_markup=keyboard
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")
        return

    # === Пресеты ===
    if data.startswith("preset_"):
        preset_name = data.replace("preset_", "")
        if preset_name == "cancel":
            await query.edit_message_text("❌ Отменено.")
            return
        if preset_name in RANGE_PRESETS:
            state["range"] = RANGE_PRESETS[preset_name]
            await query.edit_message_text(
                f"✅ Пресет {preset_name}."
            )
        return

    # === Сброс ===
    if data == "action_reset":
        reset_state(user_id)
        await query.edit_message_text("🔄 Раздача сброшена.")
        return

    # === Действия ===
    new_range = state["range"]
    action_desc = ""

    if data == "action_raise":
        new_range = "QQ+,AKs,AKo"
        action_desc = "🟠 RAISE"
    elif data == "action_call":
        new_range = "22-99,AQs-AJs,KQs,AQo"
        action_desc = "🔵 CALL"
    elif data == "action_check":
        action_desc = "🟡 CHECK"
    elif data == "action_3bet":
        new_range = "QQ+,AKs"
        action_desc = "🔴 3-BET"
    elif data == "action_fold":
        state["hero"] = []
        state["board"] = []
        await query.edit_message_text("⚫ Оппонент сбросил.")
        return
    else:
        return

    try:
        hands = parse_range(new_range)
        if not hands:
            raise ValueError("Пусто")
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка: {e}")
        return

    state["range"] = new_range

    if len(state["hero"]) == 2:
        try:
            analysis, win, best_action, keyboard = calculate_and_format(state)
            await query.edit_message_text(
                analysis, parse_mode="Markdown", reply_markup=keyboard
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")
    else:
        await query.edit_message_text(
            f"✅ {action_desc}\nДиапазон: {new_range}"
        )


# ============ ЗАПУСК ============
def main():
    print("=" * 55)
    print("🎴 POKER TELEGRAM BOT v3")
    print("=" * 55)
    print(f"Модель: {MODEL}")
    print(f"Логи: {LOG_FILE}")
    print(f"Профили: {PLAYERS_FILE}")
    print("=" * 55)

    if TELEGRAM_TOKEN == "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER":
        print("❌ Установи TELEGRAM_TOKEN!")
        return
    if PROXYAPI_KEY == "ВСТАВЬ_СЮДА_КЛЮЧ_PROXYAPI":
        print("❌ Установи PROXYAPI_KEY!")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("range", cmd_range))
    app.add_handler(CommandHandler("presets", cmd_presets))
    app.add_handler(CommandHandler("manual", cmd_manual))
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("players", cmd_players))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("mystats", cmd_stats))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("✅ Бот запущен!")
    print()
    app.run_polling(allowed_updates=Update.ALL_TYPES)



# ============ ЗАПУСК НА RENDER ============
app_flask = Flask(__name__)

@app_flask.route('/')
def home():
    return "Poker Bot is running"

@app_flask.route('/health')
def health():
    return "OK"

def run_bot():
    """Запускаем Telegram-бота в отдельном потоке"""
    bot_app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Регистрируем все обработчики (как в старом main)
    bot_app.add_handler(CommandHandler("start", cmd_start))
    bot_app.add_handler(CommandHandler("help", cmd_help))
    bot_app.add_handler(CommandHandler("reset", cmd_reset))
    bot_app.add_handler(CommandHandler("range", cmd_range))
    bot_app.add_handler(CommandHandler("presets", cmd_presets))
    bot_app.add_handler(CommandHandler("manual", cmd_manual))
    bot_app.add_handler(CommandHandler("setup", cmd_setup))
    bot_app.add_handler(CommandHandler("players", cmd_players))
    bot_app.add_handler(CommandHandler("profile", cmd_profile))
    bot_app.add_handler(CommandHandler("mystats", cmd_stats))
    bot_app.add_handler(CallbackQueryHandler(handle_button))
    bot_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("✅ Бот запущен в фоне!")
    bot_app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    # Запускаем Flask в фоне (для Render health check)
    flask_thread = threading.Thread(
        target=lambda: app_flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080))),
        daemon=True
    )
    flask_thread.start()

    # Запускаем бота в главном потоке (чтобы asyncio работал)
    print("✅ Запускаем бота...")
    run_bot()