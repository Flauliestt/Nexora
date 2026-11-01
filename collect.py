#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Nexora: ежедневный сборщик игровых данных.

Только стандартная библиотека Python. Пишет data/data.json.
Каждый источник независим: если один упал, остальные и старые данные остаются.

Переменные окружения (все необязательные):
  PUBG_API_KEY  ключ PUBG API (без него блок PUBG пропускается)
  GITHUB_TOKEN  чтобы GitHub не ограничивал запросы к репозиторию Valve
  ONLY          только эти блоки, через запятую: dota, cs.teams, pubg ...
  FORCE         1 = игнорировать дневной кэш
  DEBUG         1 = сохранять сырые ответы в data/debug/
"""
import datetime as dt
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

UA = "NexoraHub/1.0 (+https://t.me/RealNexoraHub)"
OUT = os.environ.get("NEXORA_OUT", "data/data.json")
DEBUG_DIR = os.path.join(os.path.dirname(OUT) or ".", "debug")
LP_PAUSE = 31          # Liquipedia: не чаще 1 запроса parse в 30 секунд
CACHE_HOURS = 20       # удачный блок моложе этого срока не пересобирается
GAMES = ("dota", "cs", "valorant", "pubg", "fortnite")


class Skip(Exception):
    """Блок сознательно пропущен (например, нет ключа)."""


class FetchError(Exception):
    pass


class ParseError(Exception):
    def __init__(self, msg, raw=None):
        Exception.__init__(self, msg)
        self.raw = raw


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on", "да")


# ───────────────────────── сеть ─────────────────────────

def fetch(url, headers=None, tries=3, timeout=40):
    h = {"User-Agent": UA, "Accept-Encoding": "gzip",
         "Accept": "application/json, text/plain, */*"}
    h.update(headers or {})
    last = "?"
    for i in range(tries):
        wait = 3 * (i + 1)
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code in (400, 401, 403, 404):
                break
            if e.code == 429:
                wait = 20 * (i + 1)
        except Exception as e:  # сеть, таймаут, SSL
            last = "%s: %s" % (type(e).__name__, str(e)[:100])
        if i < tries - 1:
            time.sleep(wait)
    host = re.sub(r"^https?://", "", url).split("?")[0][:80]
    raise FetchError("%s (%s)" % (last, host))


def get_json(url, headers=None):
    txt = fetch(url, headers)
    try:
        return json.loads(txt)
    except ValueError:
        raise ParseError("ответ не JSON", raw=txt)


def clean(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)      # [текст](ссылка)
    s = re.sub(r"[*_`]+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def iso_day(ts):
    try:
        return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return None


# ───────────────────────── Dota 2 ─────────────────────────

def dota_teams():
    rows = get_json("https://api.opendota.com/api/teams")
    if not isinstance(rows, list):
        raise ParseError("ожидался список команд", raw=json.dumps(rows)[:3000])
    cutoff = utcnow().timestamp() - 90 * 86400
    rows = [t for t in rows if t.get("name") and (t.get("last_match_time") or 0) >= cutoff]
    rows.sort(key=lambda t: -(t.get("rating") or 0))
    out = []
    for t in rows[:15]:
        out.append({
            "rank": len(out) + 1,
            "name": str(t["name"]),
            "tag": t.get("tag") or "",
            "rating": int(round(t.get("rating") or 0)),
            "wins": int(t.get("wins") or 0),
            "losses": int(t.get("losses") or 0),
            "last": iso_day(t.get("last_match_time")),
        })
    if len(out) < 3:
        raise ParseError("слишком мало активных команд: %d" % len(out), raw=json.dumps(rows[:5])[:2000])
    return out


def dota_meta():
    rows = get_json("https://api.opendota.com/api/heroStats")
    if not isinstance(rows, list) or not rows:
        raise ParseError("пустой heroStats", raw=json.dumps(rows)[:2000])

    def wr(w, p):
        return round(100.0 * w / p, 1) if p else 0.0

    heroes = []
    for h in rows:
        name = h.get("localized_name")
        if not name:
            continue
        pp, pw = int(h.get("pro_pick") or 0), int(h.get("pro_win") or 0)
        hp = int(h.get("7_pick") or 0) + int(h.get("8_pick") or 0)
        hw = int(h.get("7_win") or 0) + int(h.get("8_win") or 0)
        heroes.append({"hero": name, "pro_picks": pp, "pro_wins": pw, "pro_bans": int(h.get("pro_ban") or 0),
                       "pro_wr": wr(pw, pp), "high_picks": hp, "high_wr": wr(hw, hp)})
    if len(heroes) < 20:
        raise ParseError("мало героев: %d" % len(heroes), raw=json.dumps(rows[:2])[:2000])

    pro = sorted(heroes, key=lambda x: -x["pro_picks"])[:10]
    pro = [{"hero": x["hero"], "picks": x["pro_picks"], "bans": x["pro_bans"], "wr": x["pro_wr"]} for x in pro]

    top_picks = sorted(heroes, key=lambda x: -x["high_picks"])
    best = top_picks[:60]            # среди популярных, чтобы не выбирать редких героев
    best = sorted(best, key=lambda x: -x["high_wr"])[:10]
    popular = top_picks[:10]
    fmt = lambda xs: [{"hero": x["hero"], "picks": x["high_picks"], "wr": x["high_wr"]} for x in xs]
    return {"pro": pro, "high_best": fmt(best), "high_popular": fmt(popular)}


def dota_matches():
    rows = get_json("https://api.opendota.com/api/proMatches")
    if not isinstance(rows, list) or not rows:
        raise ParseError("пустой proMatches", raw=json.dumps(rows)[:2000])
    rows.sort(key=lambda m: -(m.get("start_time") or 0))
    out = []
    for m in rows:
        if not m.get("radiant_name") and not m.get("dire_name"):
            continue
        rw = m.get("radiant_win")
        out.append({
            "league": m.get("league_name") or "",
            "radiant": m.get("radiant_name") or "?",
            "dire": m.get("dire_name") or "?",
            "score": "%s:%s" % (m.get("radiant_score", "?"), m.get("dire_score", "?")),
            "winner": (m.get("radiant_name") if rw else m.get("dire_name")) or "?",
            "date": iso_day(m.get("start_time")),
        })
        if len(out) >= 10:
            break
    if not out:
        raise ParseError("нет матчей с названиями команд", raw=json.dumps(rows[:3])[:2000])
    return out


DOTA_REGIONS = ("europe", "americas", "se_asia", "china")


def dota_players():
    out, errs = {}, []
    for reg in DOTA_REGIONS:
        url = ("https://www.dota2.com/webapi/ILeaderboard/GetDivisionLeaderboard/v0001"
               "?division=%s&leaderboard=0" % reg)
        try:
            j = get_json(url)
            lb = j.get("leaderboard")
            if not isinstance(lb, list) or not lb:
                raise ParseError("нет поля leaderboard", raw=json.dumps(j)[:2000])
            rows = []
            for r in sorted(lb, key=lambda r: r.get("rank") or 10 ** 9)[:10]:
                nm = clean(r.get("name") or "")
                if nm:
                    rows.append({"rank": int(r.get("rank") or len(rows) + 1), "name": nm,
                                 "team": clean(r.get("team_tag") or "")})
            if rows:
                out[reg] = rows
        except (FetchError, ParseError) as e:
            errs.append("%s: %s" % (reg, e))
    if not out:
        raise ParseError("все регионы не ответили: " + "; ".join(errs), raw=None)
    return out


# ───────────────────────── CS2: рейтинг Valve (VRS) ─────────────────────────

VRS_REPO = "ValveSoftware/counter-strike_regional_standings"


def parse_vrs(md):
    """Достаёт таблицу рейтинга из markdown-файла Valve."""
    cols = None
    out = []
    for line in md.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue                                        # строка |---|---|
        low = [clean(c).lower() for c in cells]
        if cols is None and not re.fullmatch(r"\d+", low[0] if low else ""):
            m = {}
            for i, c in enumerate(low):
                if re.search(r"standing|rank|^#", c):
                    m.setdefault("rank", i)
                elif re.search(r"point|rating", c):
                    m.setdefault("points", i)
                elif re.search(r"team|name", c):
                    m.setdefault("team", i)
                elif re.search(r"roster|player", c):
                    m.setdefault("roster", i)
            if "team" in m:
                cols = m
            continue
        c = cols or {"rank": 0, "points": 1, "team": 2, "roster": 3}
        try:
            rank = int(re.sub(r"\D", "", clean(cells[c.get("rank", 0)])) or 0)
            pts = int(re.sub(r"\D", "", clean(cells[c["points"]])) or 0) if "points" in c else 0
            team = clean(cells[c["team"]])
        except (IndexError, ValueError):
            continue
        if not rank or not team:
            continue
        roster = []
        if "roster" in c and c["roster"] < len(cells):
            roster = [x.strip() for x in clean(cells[c["roster"]]).split(",") if x.strip()]
        out.append({"rank": rank, "name": team, "points": pts, "roster": roster[:5]})
    out.sort(key=lambda x: x["rank"])
    return out


def cs_teams():
    gh = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        gh["Authorization"] = "Bearer " + tok
    tree = None
    for branch in ("main", "master"):
        try:
            tree = get_json("https://api.github.com/repos/%s/git/trees/%s?recursive=1" % (VRS_REPO, branch), gh)
            break
        except FetchError:
            continue
    if not tree:
        raise FetchError("не удалось получить список файлов Valve")
    best = None
    for node in tree.get("tree", []):
        p = node.get("path", "")
        m = re.search(r"standings_global_(\d{4})_(\d{2})_(\d{2})\.md$", p)
        if m and node.get("type") == "blob":
            key = (p.startswith("live/"), m.group(1) + m.group(2) + m.group(3))
            if best is None or key > best[0]:
                best = (key, p)
    if not best:
        raise ParseError("не найден файл standings_global_*.md",
                         raw="\n".join(n.get("path", "") for n in tree.get("tree", [])[:200]))
    path = best[1]
    md = fetch("https://raw.githubusercontent.com/%s/HEAD/%s" % (VRS_REPO, path))
    rows = parse_vrs(md)
    if len(rows) < 5:
        raise ParseError("в таблице %d команд (%s)" % (len(rows), path), raw=md[:6000])
    m = re.search(r"(\d{4})_(\d{2})_(\d{2})", path)
    return {"date": "%s-%s-%s" % m.groups(), "teams": rows[:15]}


# ───────────────────────── PUBG (нужен ключ) ─────────────────────────

def pubg_players():
    key = os.environ.get("PUBG_API_KEY", "").strip()
    if not key:
        raise Skip("нет секрета PUBG_API_KEY")
    h = {"Authorization": "Bearer " + key, "Accept": "application/vnd.api+json"}
    seasons = None
    for shard in ("steam", "pc-eu"):
        try:
            seasons = get_json("https://api.pubg.com/shards/%s/seasons" % shard, h)
            break
        except FetchError:
            continue
    if not seasons:
        raise FetchError("PUBG: список сезонов недоступен (проверь ключ)")
    cur = next((s for s in seasons.get("data", []) if (s.get("attributes") or {}).get("isCurrentSeason")), None)
    if not cur:
        raise ParseError("нет текущего сезона", raw=json.dumps(seasons)[:2000])
    sid = cur["id"]
    out, errs = {}, []
    for shard, label in (("pc-eu", "EU"), ("pc-as", "AS"), ("pc-na", "NA")):
        time.sleep(1)
        try:
            j = get_json("https://api.pubg.com/shards/%s/leaderboards/%s/squad-fpp" % (shard, sid), h)
            rows = []
            for it in j.get("included", []):
                if it.get("type") != "player":
                    continue
                a = it.get("attributes") or {}
                st = a.get("stats") or {}
                rows.append({"rank": int(a.get("rank") or 0), "name": a.get("name") or "?",
                             "points": int(st.get("rankPoints") or 0),
                             "wins": int(st.get("wins") or 0),
                             "kd": round(float(st.get("killDeathRatio") or 0), 2),
                             "damage": int(round(float(st.get("averageDamage") or 0)))})
            rows = sorted([r for r in rows if r["rank"]], key=lambda r: r["rank"])[:10]
            if rows:
                out[label] = rows
        except (FetchError, ParseError, ValueError) as e:
            errs.append("%s: %s" % (label, e))
    if not out:
        raise ParseError("лидерборды не получены: " + "; ".join(errs), raw=None)
    m = re.search(r"(\d+)$", sid)
    return {"season": ("Сезон " + m.group(1)) if m else sid, "mode": "Squad FPP", "regions": out}


# ───────────────────────── даты ─────────────────────────

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _piece(p):
    """'Sep 4, 2025' / '4 Sep 2025' / '14, 2025' / '14' → (месяц|None, день|None, год|None)."""
    p = p.strip().strip(",")
    m = re.match(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?$", p)
    if m and m.group(1)[:3].lower() in MONTHS:
        return MONTHS[m.group(1)[:3].lower()], int(m.group(2)), int(m.group(3)) if m.group(3) else None
    m = re.match(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?(?:,?\s+(\d{4}))?$", p)
    if m and m.group(2)[:3].lower() in MONTHS:
        return MONTHS[m.group(2)[:3].lower()], int(m.group(1)), int(m.group(3)) if m.group(3) else None
    m = re.match(r"(\d{1,2})(?:,?\s+(\d{4}))?$", p)
    if m:
        return None, int(m.group(1)), int(m.group(2)) if m.group(2) else None
    return None, None, None


def parse_date_range(s):
    """'Sep 4 - 14, 2025' → ('2025-09-04','2025-09-14'). Не понял → (None, None)."""
    s = (s or "").replace("–", "-").replace("—", "-").replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    iso = re.findall(r"\d{4}-\d{2}-\d{2}", s)
    if iso:
        return iso[0], iso[-1]
    parts = [x for x in re.split(r"\s*-\s*", s, maxsplit=1)] if "-" in s else [s]
    try:
        rm, rd, ry = _piece(parts[-1])
        if rd is None:
            return None, None
        if len(parts) == 1:
            lm, ld, ly = rm, rd, ry
        else:
            lm, ld, ly = _piece(parts[0])
            if ld is None:
                return None, None
        rm = rm or lm
        lm = lm or rm
        ry = ry or ly
        ly = ly or ry
        if not (rm and lm and ry and ly):
            return None, None
        if lm > rm and ly == ry:                 # 'Dec 30 - Jan 3, 2026'
            ly -= 1
        a = dt.date(ly, lm, ld)
        b = dt.date(ry, rm, rd)
        if b < a:
            return None, None
        return a.isoformat(), b.isoformat()
    except ValueError:
        return None, None


# ───────────────────────── разбор HTML (Liquipedia) ─────────────────────────

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BLOCK = {"div", "p", "li", "tr", "td", "th", "br", "ul", "table"}


class Node(object):
    __slots__ = ("tag", "attrs", "kids", "cls")

    def __init__(self, tag, attrs=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.kids = []
        self.cls = set((self.attrs.get("class") or "").lower().split())


class _Builder(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        n = Node(tag, [(k, v or "") for k, v in attrs])
        self.stack[-1].kids.append(n)
        if tag not in VOID:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].kids.append(Node(tag, [(k, v or "") for k, v in attrs]))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, d):
        self.stack[-1].kids.append(d)


def build_tree(html):
    b = _Builder()
    b.feed(html)
    b.close()
    return b.root


def walk(n):
    for k in n.kids:
        if isinstance(k, Node):
            yield k
            for x in walk(k):
                yield x


def text(n):
    if not isinstance(n, Node):
        return re.sub(r"\s+", " ", str(n)).strip()
    parts = []

    def rec(x):
        for k in x.kids:
            if isinstance(k, Node):
                if k.tag in ("script", "style"):
                    continue
                rec(k)
                if k.tag in BLOCK:
                    parts.append(" ")
            else:
                parts.append(k)
    rec(n)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


ROW_CLS = {"gridrow", "divrow"}
CELL_CLS = {"gridcell", "divcell"}
HEADER_CLS = {"gridheader", "divheaderrow", "gridrowheader"}
EMPTY_TEAM = {"", "tbd", "tba", "-", "—", "?", "n/a"}


def team_name(cell):
    if cell is None:
        return None
    for n in walk(cell):
        if "team-template-text" in n.cls:
            t = text(n)
            if t:
                return None if t.lower() in EMPTY_TEAM else t
    links = [text(n) for n in walk(cell) if n.tag == "a" and text(n)]
    t = links[-1] if links else text(cell)
    return None if t.lower() in EMPTY_TEAM else t[:60]


def tournament_name(cell):
    for n in walk(cell):
        if n.tag == "a":
            t = text(n)
            if t:
                return t[:120]
    for n in walk(cell):
        if n.tag == "a" and n.attrs.get("title"):
            return n.attrs["title"][:120]
    return text(cell)[:120]


def parse_row(cells):
    """cells: список Node-ячеек одной строки → словарь или None."""
    lab = {}
    for i, c in enumerate(cells):
        cl = " ".join(sorted(c.cls))
        for key in ("tournament", "date", "prize", "firstplace", "secondplace"):
            if key in cl and key not in lab:
                lab[key] = i
    name_i = lab.get("tournament", 0)
    date_i = lab.get("date")
    if date_i is None:
        for i, c in enumerate(cells):
            if i != name_i and parse_date_range(text(c))[0]:
                date_i = i
                break
    if date_i is None:
        return None
    start, end = parse_date_range(text(cells[date_i]))
    if not start:
        return None
    name = tournament_name(cells[name_i])
    if not name:
        return None
    prize_i = lab.get("prize")
    if prize_i is None:
        prize_i = next((i for i, c in enumerate(cells) if "$" in text(c) and i not in (name_i, date_i)), None)
    prize = text(cells[prize_i])[:40] if prize_i is not None else ""
    if "firstplace" in lab:
        win = team_name(cells[lab["firstplace"]])
        run = team_name(cells[lab["secondplace"]]) if "secondplace" in lab else None
    elif len(cells) >= 6:
        win, run = team_name(cells[-2]), team_name(cells[-1])
    else:
        win = run = None
    return {"name": name, "start": start, "end": end, "prize": prize, "winner": win, "runner_up": run}


def parse_tournaments_html(html, tier=""):
    root = build_tree(html)
    items = []
    for n in walk(root):
        is_row = n.tag == "tr" or bool(n.cls & ROW_CLS)
        if not is_row or n.cls & HEADER_CLS:
            continue
        cells = [k for k in n.kids if isinstance(k, Node) and (k.tag in ("td", "th") or k.cls & CELL_CLS)]
        if len(cells) < 2 or all(c.tag == "th" for c in cells):
            continue
        it = parse_row(cells)
        if it:
            it["tier"] = tier
            items.append(it)
    return items


# ───────────────────────── турниры (Liquipedia) ─────────────────────────

LP_WIKI = {"dota": "dota2", "cs": "counterstrike", "valorant": "valorant", "pubg": "pubg", "fortnite": "fortnite"}
LP_PAGES = {
    "dota": [("S-Tier_Tournaments", "S"), ("A-Tier_Tournaments", "A")],
    "cs": [("S-Tier_Tournaments", "S"), ("A-Tier_Tournaments", "A")],
    "valorant": [("S-Tier_Tournaments", "S")],
    "pubg": [("S-Tier_Tournaments", "S")],
    "fortnite": [("S-Tier_Tournaments", "S")],
}
_lp_last = [0.0]
_lp_blocked = [False]   # после 429 больше не стучимся к Liquipedia в этом запуске


def lp_page(wiki, page):
    if _lp_blocked[0]:
        raise FetchError("Liquipedia ограничила запросы (429) в этом запуске, остальные пропущены")
    gap = LP_PAUSE - (time.time() - _lp_last[0])
    if gap > 0:
        time.sleep(gap)
    url = ("https://liquipedia.net/%s/api.php?action=parse&page=%s&prop=text&format=json"
           "&disablelimitreport=1&disableeditsection=1" % (wiki, page))
    try:
        txt = fetch(url, {"Accept": "application/json"}, tries=1)
    except FetchError as e:
        if "429" in str(e):
            _lp_blocked[0] = True
        raise
    finally:
        _lp_last[0] = time.time()
    try:
        j = json.loads(txt)
    except ValueError:
        raise ParseError("Liquipedia вернула не JSON", raw=txt)
    if "error" in j:
        raise ParseError("Liquipedia: %s" % (j["error"].get("info") or j["error"].get("code")), raw=txt)
    t = (j.get("parse") or {}).get("text")
    t = t.get("*") if isinstance(t, dict) else t
    if not t:
        raise ParseError("в ответе нет текста страницы", raw=txt)
    return t


def status_of(it, today):
    if it["start"] > today:
        return "upcoming"
    if it["end"] < today:
        return "finished"
    return "ongoing"


def pick_tournaments(items, today):
    """Из всех строк оставляет ближайшие будущие, идущие и недавние завершённые."""
    seen, uniq = set(), []
    for it in items:
        k = (it["name"].lower(), it["start"])
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    for it in uniq:
        it["status"] = status_of(it, today)
    up = sorted([i for i in uniq if i["status"] == "upcoming"], key=lambda i: i["start"])[:10]
    on = sorted([i for i in uniq if i["status"] == "ongoing"], key=lambda i: i["start"])[:10]
    fin = sorted([i for i in uniq if i["status"] == "finished"], key=lambda i: i["end"], reverse=True)[:12]
    return up + on + fin


def tournaments(game):
    wiki = LP_WIKI[game]
    today = utcnow().date().isoformat()
    items, errs, got_page = [], [], False
    for page, tier in LP_PAGES[game]:
        try:
            html = lp_page(wiki, page)
        except (FetchError, ParseError) as e:
            errs.append("%s: %s" % (page, e))
            continue
        got_page = True
        rows = parse_tournaments_html(html, tier)
        if len(rows) < 3 or truthy(os.environ.get("DEBUG")):
            save_debug("%s_%s.html" % (game, page.replace(":", "_")), html[:80000])
        items.extend(rows)
    if not items:
        raise ParseError(("страницы получены, но турниры не распознаны" if got_page else "Liquipedia недоступна")
                         + ("; " + "; ".join(errs) if errs else ""))
    return pick_tournaments(items, today)


# ───────────────────────── запуск ─────────────────────────

def save_debug(name, content):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        with open(os.path.join(DEBUG_DIR, name), "w", encoding="utf-8") as f:
            f.write(content)
    except OSError:
        pass


def _place(data, name, value):
    game, block = name.split(".", 1)
    data["games"].setdefault(game, {})[block] = value


def _count(v):
    if isinstance(v, list):
        return len(v)
    if isinstance(v, dict):
        return sum(_count(x) if isinstance(x, (list, dict)) else 1 for x in v.values())
    return 1


def build_tasks():
    t = [("dota.teams", dota_teams), ("dota.meta", dota_meta), ("dota.matches", dota_matches),
         ("dota.players", dota_players), ("cs.teams", cs_teams), ("pubg.players", pubg_players)]
    for g in GAMES:
        t.append((g + ".tournaments", (lambda g=g: tournaments(g))))
    return t


def load_prev():
    try:
        with open(OUT, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("games"), dict):
            d.setdefault("updated", {})
            d.setdefault("sources", {})
            return d
    except (OSError, ValueError):
        pass
    return {"schema": 1, "generated": None, "updated": {}, "sources": {}, "games": {}}


def selected(name, only):
    return not only or any(name == o or name.startswith(o + ".") for o in only)


def is_fresh(data, name, now):
    src = data["sources"].get(name) or {}
    ts = data["updated"].get(name)
    if not src.get("ok") or not ts:
        return False
    try:
        age = now - dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return False
    return age < dt.timedelta(hours=CACHE_HOURS)


def main():
    now = utcnow()
    _lp_blocked[0] = False
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    data = load_prev()
    only = [x.strip() for x in os.environ.get("ONLY", "").split(",") if x.strip()]
    force = truthy(os.environ.get("FORCE"))
    debug = truthy(os.environ.get("DEBUG"))
    ok = fail = 0
    log = []
    for name, fn in build_tasks():
        if not selected(name, only):
            continue
        if not force and is_fresh(data, name, now):
            data["sources"][name] = {"ok": True, "cached": True}
            log.append((name, "кэш", "данные свежие, пропуск"))
            continue
        try:
            val = fn()
            _place(data, name, val)
            data["updated"][name] = stamp
            data["sources"][name] = {"ok": True, "n": _count(val)}
            log.append((name, "ок", "записей: %d" % _count(val)))
            ok += 1
        except Skip as e:
            data["sources"][name] = {"ok": False, "skipped": True, "note": str(e)}
            log.append((name, "пропуск", str(e)))
        except Exception as e:
            msg = ("%s: %s" % (type(e).__name__, e))[:300]
            data["sources"][name] = {"ok": False, "error": msg}
            log.append((name, "ошибка", msg))
            fail += 1
            raw = getattr(e, "raw", None)
            if raw:
                save_debug(name + ".txt", str(raw)[:20000])
        if debug:
            save_debug("_log.txt", "\n".join("%s | %s | %s" % r for r in log))
    data["schema"] = 1
    data["generated"] = stamp
    data.pop("example", None)
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=False)
        f.write("\n")
    for r in log:
        print("%-20s %-8s %s" % r)
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        try:
            with open(summ, "a", encoding="utf-8") as f:
                f.write("### Сборщик Nexora\n\n| Блок | Статус | Детали |\n|---|---|---|\n")
                for n, s, d in log:
                    f.write("| %s | %s | %s |\n" % (n, s, str(d).replace("|", "/").replace("\n", " ")))
        except OSError:
            pass
    if fail and not ok:
        print("ВНИМАНИЕ: ни один блок не обновился")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
