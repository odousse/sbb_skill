#!/usr/bin/env python3
"""SBB & Swiss Public Transport CLI helper for AI agents.

Zero-dependency Python 3.9+ CLI supporting a hybrid Swiss open-data stack:
- Zero-auth default: transport.opendata.ch (timetables & live prognoses) +
  data.sbb.ch rail-traffic-information (ongoing SBB disruptions).
- Official national upgrade (when OPENTRANSPORTDATA_API_KEY is configured):
  opentransportdata.swiss OJP 2.0 (disruption-aware routing & live stop events) +
  SIRI-SX Unplanned (real-time national incident feed with local caching) +
  data.sbb.ch planned/major rail works.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

ZURICH_TZ = ZoneInfo("Europe/Zurich")
UTC_TZ = timezone.utc

USER_AGENT = "sbb-trip-planner-skill/1.0 (+https://github.com/odousse/sbb_skill)"

TRANSPORT_API_BASE = "https://transport.opendata.ch/v1"
SBB_DISRUPTIONS_URL = (
    "https://data.sbb.ch/api/explore/v2.1/catalog/datasets/"
    "rail-traffic-information/records"
)
OJP20_ENDPOINT = "https://api.opentransportdata.swiss/ojp20"
SIRI_SX_UNPLANNED_URL = "https://api.opentransportdata.swiss/la/siri-sx-unplanned"
SIRI_SX_FULL_URL = "https://api.opentransportdata.swiss/la/siri-sx"

OJP_NS = "http://www.vdv.de/ojp"
SIRI_NS = "http://www.siri.org.uk/siri"
XML_LANG_ATTR = "{http://www.w3.org/XML/1998/namespace}lang"

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "sbb-skill" / "config.json"
CACHE_DIR = Path("/tmp") / f"sbb_skill_cache_{os.getuid()}"

CAPACITY_LABELS = {
    1: "Low (1/3)",
    2: "Medium (2/3)",
    3: "High (3/3)",
}

STATION_STOPWORDS = {
    "hb",
    "bahnhof",
    "gare",
    "stazione",
    "station",
    "sbb",
    "cff",
    "ffs",
    "see",
    "dorf",
    "stadt",
    "nord",
    "sud",
    "sued",
    "ost",
    "west",
}


# ---------------------------------------------------------------------------
# Configuration & Caching
# ---------------------------------------------------------------------------


def get_config_path() -> Path:
    env_path = os.environ.get("SBB_SKILL_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_CONFIG_PATH


def load_config() -> Dict[str, Any]:
    cfg_path = get_config_path()
    if not cfg_path.is_file():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: Dict[str, Any]) -> Path:
    cfg_path = get_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(cfg_path, 0o600)
    except OSError:
        pass
    return cfg_path


def resolve_api_key(explicit_key: Optional[str] = None) -> Optional[str]:
    if explicit_key and explicit_key.strip():
        return explicit_key.strip()
    for env_name in ("OPENTRANSPORTDATA_API_KEY", "OJP_API_KEY"):
        val = os.environ.get(env_name, "").strip()
        if val:
            return val
    cfg = load_config()
    val = str(cfg.get("api_key", "")).strip()
    return val or None


def resolve_lang(explicit_lang: Optional[str] = None) -> str:
    if explicit_lang:
        return explicit_lang.lower()
    cfg = load_config()
    lang = str(cfg.get("lang", "")).strip().lower()
    return lang if lang in ("de", "fr", "it", "en") else "en"


def _ensure_cache_dir() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CACHE_DIR, 0o700)
    except OSError:
        pass
    return CACHE_DIR


def cache_get(key: str, ttl_seconds: int) -> Optional[bytes]:
    if ttl_seconds <= 0:
        return None
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    path = _ensure_cache_dir() / f"{digest}.cache"
    if not path.is_file():
        return None
    try:
        age = time.time() - path.stat().st_mtime
        if age <= ttl_seconds:
            return path.read_bytes()
    except OSError:
        return None
    return None


def cache_set(key: str, payload: bytes) -> None:
    try:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        path = _ensure_cache_dir() / f"{digest}.cache"
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_bytes(payload)
        tmp_path.replace(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Date, Time, Duration & Text Utilities (Python 3.9+ safe)
# ---------------------------------------------------------------------------


_TZ_NOCOLON_RE = re.compile(r"([+-]\d{2})(\d{2})$")
_FRAC_SEC_RE = re.compile(r"\.(\d+)(?=[+-]\d{2}:\d{2}$|$)")
_OPENDATA_DUR_RE = re.compile(r"^(?:(\d+)d)?(\d{1,2}):(\d{2}):(\d{2})$")
_ISO_DUR_RE = re.compile(
    r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)


def now_zurich() -> datetime:
    return datetime.now(ZURICH_TZ)


def parse_iso_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO-8601 strings robustly on Python 3.9+, normalizing to Europe/Zurich."""
    if not value:
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    s = _TZ_NOCOLON_RE.sub(r"\1:\2", s)
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)

    def _pad_frac(match: re.Match[str]) -> str:
        digits = match.group(1)[:6].ljust(6, "0")
        return f".{digits}"

    s = _FRAC_SEC_RE.sub(_pad_frac, s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZURICH_TZ)
    return dt.astimezone(ZURICH_TZ)


def parse_user_datetime(
    dt_str: Optional[str] = None,
    date_str: Optional[str] = None,
    time_str: Optional[str] = None,
    ref_now: Optional[datetime] = None,
) -> Optional[datetime]:
    """Parse flexible user/LLM date & time inputs into Europe/Zurich datetime."""
    base_now = (ref_now or now_zurich()).astimezone(ZURICH_TZ)
    if date_str or time_str:
        d_part = (date_str or base_now.strftime("%Y-%m-%d")).strip()
        t_part = (time_str or base_now.strftime("%H:%M")).strip()
        combined = f"{d_part}T{t_part}"
        parsed = parse_iso_dt(combined)
        if parsed:
            return parsed
        raise ValueError(f"Could not parse date/time: date={date_str!r}, time={time_str!r}")

    if not dt_str or not dt_str.strip():
        return None

    raw = dt_str.strip()
    lower = raw.lower()
    if lower == "now":
        return base_now

    for prefix, day_offset in (("today", 0), ("tomorrow", 1)):
        if lower.startswith(prefix):
            rem = raw[len(prefix) :].strip()
            target_day = (base_now + timedelta(days=day_offset)).strftime("%Y-%m-%d")
            if not rem:
                return parse_iso_dt(f"{target_day}T08:00")
            return parse_iso_dt(f"{target_day}T{rem}")

    if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", raw):
        parts = raw.split(":")
        hh = int(parts[0])
        mm = int(parts[1])
        ss = int(parts[2]) if len(parts) > 2 else 0
        return base_now.replace(hour=hh, minute=mm, second=ss, microsecond=0)

    parsed = parse_iso_dt(raw)
    if parsed is None:
        raise ValueError(
            f"Unrecognized datetime {dt_str!r}. Use ISO format like '2026-11-13 14:30' or '14:30'."
        )
    return parsed


def fmt_time(dt: Optional[datetime]) -> str:
    if not dt:
        return "--:--"
    return dt.astimezone(ZURICH_TZ).strftime("%H:%M")


def fmt_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "unknown"
    return dt.astimezone(ZURICH_TZ).strftime("%Y-%m-%d %H:%M")


def parse_duration_minutes(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    s = raw.strip()
    m1 = _OPENDATA_DUR_RE.match(s)
    if m1:
        days = int(m1.group(1) or 0)
        hours = int(m1.group(2))
        mins = int(m1.group(3))
        secs = int(m1.group(4))
        return days * 1440 + hours * 60 + mins + (1 if secs >= 30 else 0)
    m2 = _ISO_DUR_RE.match(s)
    if m2:
        days = int(m2.group(1) or 0)
        hours = int(m2.group(2) or 0)
        mins = int(m2.group(3) or 0)
        secs = float(m2.group(4) or 0.0)
        return days * 1440 + hours * 60 + mins + (1 if secs >= 30 else 0)
    return None


def fmt_duration_minutes(total_mins: Optional[int]) -> str:
    if total_mins is None:
        return "?"
    if total_mins < 60:
        return f"{total_mins}m"
    h, m = divmod(total_mins, 60)
    return f"{h}h {m:02d}m"


def strip_html(raw: Optional[str]) -> str:
    if not raw:
        return ""
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", raw)
    text = re.sub(r"(?i)</\s*(p|div|li|tr|h[1-6])\s*>", "\n", text)
    text = re.sub(r"(?i)<\s*li\b[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    cleaned = "\n".join(ln for ln in lines if ln)
    return cleaned.strip()


def fold_accents(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in norm if not unicodedata.combining(ch)).lower()


def station_match_patterns(station_name: str) -> List[re.Pattern[str]]:
    """Build safe word-boundary regexes for matching a Swiss station in alert text."""
    folded = fold_accents(station_name)
    # Split bilingual / compound names like "Fribourg/Freiburg" or "Biel/Bienne"
    variants: List[str] = []
    for slash_part in re.split(r"[/(),]", folded):
        part = slash_part.strip()
        if not part:
            continue
        variants.append(part)
        # Strip trailing generic tokens like "Zürich HB" -> "zürich"
        tokens = [t for t in re.split(r"[\s\-]+", part) if t]
        core_tokens = [t for t in tokens if t not in STATION_STOPWORDS]
        if core_tokens:
            core = " ".join(core_tokens)
            if core not in variants and len(core) >= 3:
                variants.append(core)

    patterns: List[re.Pattern[str]] = []
    seen = set()
    for var in variants:
        if len(var) < 3 or var in seen:
            continue
        seen.add(var)
        if var == "zug":
            # Avoid matching the German word "Zug" ("train") when preceded by articles/pronouns
            pat = re.compile(
                r"(?<!der )(?<!ein )(?<!dieser )(?<!ihr )(?<!im )\bzug\b"
                r"(?!\s+(?:fallt|faellt|verkehrt|wird|ist|hat|verspatet|ausfall))"
            )
        else:
            escaped = r"[\s\-/]+".join(re.escape(tok) for tok in var.split())
            pat = re.compile(rf"\b{escaped}\b")
        patterns.append(pat)
    return patterns


# ---------------------------------------------------------------------------
# HTTP Client
# ---------------------------------------------------------------------------


class ApiError(RuntimeError):
    pass


def http_fetch(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: int = 15,
    cache_ttl: int = 0,
    use_cache: bool = True,
) -> bytes:
    cache_key = f"{method}:{url}:{body.decode('utf-8', 'ignore') if body else ''}"
    if use_cache and cache_ttl > 0:
        cached = cache_get(cache_key, cache_ttl)
        if cached is not None:
            return cached

    req_headers = {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip",
    }
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            if "gzip" in encoding or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_bytes = exc.read()
            if err_bytes[:2] == b"\x1f\x8b":
                err_bytes = gzip.decompress(err_bytes)
            err_body = err_bytes.decode("utf-8", errors="replace")[:300].strip()
        except Exception:
            pass
        detail = f" ({err_body})" if err_body else ""
        raise ApiError(f"HTTP {exc.code} {exc.reason} from {url}{detail}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"Network error contacting {url}: {exc.reason}") from exc

    if use_cache and cache_ttl > 0:
        cache_set(cache_key, raw)
    return raw


def http_get_json(
    url: str,
    *,
    params: Optional[Sequence[Tuple[str, str]]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 15,
    cache_ttl: int = 0,
    use_cache: bool = True,
) -> Any:
    full_url = url
    if params:
        query = urllib.parse.urlencode(params, doseq=True)
        sep = "&" if "?" in url else "?"
        full_url = f"{url}{sep}{query}"
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    raw = http_fetch(
        full_url,
        headers=hdrs,
        timeout=timeout,
        cache_ttl=cache_ttl,
        use_cache=use_cache,
    )
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ApiError(f"Invalid JSON response from {full_url}: {exc}") from exc


# ---------------------------------------------------------------------------
# Disruptions: SBB Open Data (Zero-Auth) + SIRI-SX (Official Upgrade)
# ---------------------------------------------------------------------------


_SBB_LINES_EN_RE = re.compile(r"Lines?\s+([A-Z0-9,\s/\-]+?)\s+(?:are|is)\s+affected", re.IGNORECASE)
_SBB_LINES_DE_RE = re.compile(r"Linien?\s+([A-Z0-9,\s/\-]+?)\s+(?:sind|ist)\s+betroffen", re.IGNORECASE)


def _extract_lines_from_text(text: str) -> List[str]:
    for pat in (_SBB_LINES_EN_RE, _SBB_LINES_DE_RE):
        m = pat.search(text)
        if m:
            raw = re.sub(r"\b(?:and|und|et|e)\b", ",", m.group(1), flags=re.IGNORECASE)
            tokens = [t.strip() for t in raw.split(",") if t.strip()]
            return [t for t in tokens if len(t) <= 12][:15]
    return []


def parse_sbb_disruption_records(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize SBB `rail-traffic-information` records into unified alert dicts."""
    items: List[Dict[str, Any]] = []
    for idx, rec in enumerate(records):
        title = strip_html(
            rec.get("title")
            or rec.get("headline")
            or rec.get("name")
            or "Rail traffic notice"
        )
        # Prefer description_html over description because SBB's plain `description`
        # strips <br /> without whitespace, gluing sentences together.
        desc = strip_html(
            rec.get("description_html")
            or rec.get("description")
            or rec.get("description_text")
            or rec.get("text")
            or ""
        )
        start_raw = (
            rec.get("validitybegin")
            or rec.get("startdatetime")
            or rec.get("valid_from")
            or rec.get("published")
        )
        end_raw = (
            rec.get("validityend")
            or rec.get("enddatetime")
            or rec.get("valid_to")
        )
        start_dt = parse_iso_dt(str(start_raw)) if start_raw else None
        end_dt = parse_iso_dt(str(end_raw)) if end_raw else None
        published_dt = parse_iso_dt(str(rec.get("published"))) if rec.get("published") else None

        cause = strip_html(str(rec.get("cause") or rec.get("reason") or "")) or None
        link = rec.get("link") or rec.get("url") or None
        link_id = link.rstrip("/").rsplit("/", 1)[-1] if isinstance(link, str) and "/" in link else None
        alert_id = str(
            rec.get("id")
            or rec.get("guid")
            or rec.get("recordid")
            or link_id
            or f"sbb-{idx + 1}"
        )

        d_begin = str(rec.get("dailyvaliditybegin") or "").strip()
        d_end = str(rec.get("dailyvalidityend") or "").strip()
        is_all_day = d_begin.startswith("00:00") and (
            d_end.startswith("23:59") or d_end.startswith("24:00")
        )
        daily_win = f"{d_begin[:5]}–{d_end[:5]}" if (d_begin and d_end and not is_all_day) else None

        items.append(
            {
                "id": alert_id,
                "source": "data.sbb.ch",
                "planned": None,
                "severity": rec.get("severity") or None,
                "title": title,
                "description": desc,
                "cause": cause,
                "start": start_dt.isoformat() if start_dt else None,
                "end": end_dt.isoformat() if end_dt else None,
                "published": published_dt.isoformat() if published_dt else None,
                "daily_window": daily_win,
                "affected_lines": _extract_lines_from_text(desc),
                "affected_stops": [],
                "link": link,
                "_start_dt": start_dt,
                "_end_dt": end_dt,
            }
        )
    return items


def fetch_sbb_disruptions(
    *,
    ref_time: Optional[datetime] = None,
    timeout: int = 15,
    use_cache: bool = True,
) -> List[Dict[str, Any]]:
    _ = ref_time
    data = http_get_json(
        SBB_DISRUPTIONS_URL,
        params=[
            ("order_by", "published desc"),
            ("limit", "100"),
        ],
        timeout=timeout,
        cache_ttl=120,
        use_cache=use_cache,
    )
    records = data.get("results") if isinstance(data, dict) else None
    if records is None and isinstance(data, list):
        records = data
    return parse_sbb_disruption_records(records or [])


def _local_tag(elem: ET.Element) -> str:
    tag = elem.tag
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _pick_localized_text(
    parent: ET.Element,
    tag_names: Sequence[str],
    preferred_lang: str = "en",
) -> str:
    candidates: List[Tuple[str, str]] = []
    wanted = set(tag_names)
    for child in parent:
        if _local_tag(child) not in wanted:
            continue
        lang = (child.attrib.get(XML_LANG_ATTR) or child.attrib.get("lang") or "").lower()
        # Text can be directly inside <Summary> or nested inside <Text> / <DefaultedText>
        direct = (child.text or "").strip()
        if direct:
            candidates.append((lang, strip_html(direct)))
        else:
            for sub in child.iter():
                if sub is not child and (sub.text or "").strip():
                    sub_lang = (
                        sub.attrib.get(XML_LANG_ATTR)
                        or sub.attrib.get("lang")
                        or lang
                    ).lower()
                    candidates.append((sub_lang, strip_html(sub.text)))
    if not candidates:
        return ""
    lang_order = [preferred_lang.lower(), "en", "de", "fr", "it", ""]
    for pref in lang_order:
        for c_lang, c_text in candidates:
            if c_lang == pref or (pref and c_lang.startswith(pref)):
                return c_text
    return candidates[0][1]


def parse_siri_sx_xml(xml_bytes: bytes, preferred_lang: str = "en") -> List[Dict[str, Any]]:
    """Parse SIRI-SX XML situations into unified alert dicts."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ApiError(f"Invalid SIRI-SX XML payload: {exc}") from exc

    alerts: List[Dict[str, Any]] = []
    for elem in root.iter():
        if _local_tag(elem) != "PtSituationElement":
            continue

        progress = ""
        sit_num = ""
        planned: Optional[bool] = None
        severity: Optional[str] = None
        start_dt: Optional[datetime] = None
        end_dt: Optional[datetime] = None
        lines: List[str] = []
        stops: List[str] = []
        link: Optional[str] = None

        for child in elem:
            ctag = _local_tag(child)
            ctext = (child.text or "").strip()
            if ctag == "Progress":
                progress = ctext.lower()
            elif ctag == "SituationNumber":
                sit_num = ctext
            elif ctag == "Planned":
                planned = ctext.lower() == "true"
            elif ctag == "Severity":
                severity = ctext
            elif ctag == "ValidityPeriod":
                for vp_child in child:
                    vtag = _local_tag(vp_child)
                    vtxt = (vp_child.text or "").strip()
                    if vtag == "StartTime" and not start_dt:
                        start_dt = parse_iso_dt(vtxt)
                    elif vtag == "EndTime" and not end_dt:
                        end_dt = parse_iso_dt(vtxt)
            elif ctag == "InfoLinks":
                for il in child.iter():
                    if _local_tag(il) == "Uri" and (il.text or "").strip():
                        link = il.text.strip()
                        break
            elif ctag == "Affects":
                for aff in child.iter():
                    atag = _local_tag(aff)
                    atxt = (aff.text or "").strip()
                    if not atxt:
                        continue
                    if atag in ("PublishedLineName", "PublishedServiceName"):
                        if atxt not in lines:
                            lines.append(atxt)
                    elif atag in ("StopPointName", "StopPlaceName"):
                        if atxt not in stops:
                            stops.append(atxt)

        if progress == "closed":
            continue

        summary = _pick_localized_text(elem, ("Summary",), preferred_lang)
        description = _pick_localized_text(elem, ("Description", "Detail"), preferred_lang)
        advice = _pick_localized_text(elem, ("Advice",), preferred_lang)
        if advice and advice not in description:
            description = f"{description}\nAdvice: {advice}".strip()

        if not summary and description:
            summary = description.splitlines()[0][:120]
        if not summary:
            summary = f"Transit alert {sit_num or 'SIRI-SX'}"

        alerts.append(
            {
                "id": sit_num or f"siri-{len(alerts) + 1}",
                "source": "opentransportdata.swiss/siri-sx",
                "planned": planned,
                "severity": severity,
                "title": summary,
                "description": description,
                "cause": None,
                "start": start_dt.isoformat() if start_dt else None,
                "end": end_dt.isoformat() if end_dt else None,
                "published": None,
                "daily_window": None,
                "affected_lines": lines[:12],
                "affected_stops": stops[:20],
                "link": link,
                "_start_dt": start_dt,
                "_end_dt": end_dt,
            }
        )
    return alerts


def fetch_siri_sx_disruptions(
    api_key: str,
    *,
    include_planned: bool = False,
    lang: str = "en",
    timeout: int = 20,
    use_cache: bool = True,
) -> List[Dict[str, Any]]:
    url = SIRI_SX_FULL_URL if include_planned else SIRI_SX_UNPLANNED_URL
    ttl = 900 if include_planned else 90  # Respect 48/day planned vs 2/min unplanned quotas
    xml_bytes = http_fetch(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/xml, text/xml",
        },
        timeout=timeout,
        cache_ttl=ttl,
        use_cache=use_cache,
    )
    return parse_siri_sx_xml(xml_bytes, preferred_lang=lang)


def filter_disruptions(
    alerts: Sequence[Dict[str, Any]],
    *,
    query: Optional[str] = None,
    stations: Optional[Sequence[str]] = None,
    lines: Optional[Sequence[str]] = None,
    ref_time: Optional[datetime] = None,
    include_all_future: bool = False,
) -> List[Dict[str, Any]]:
    """Filter alerts by time validity window and optional query / route stations / lines."""
    target = (ref_time or now_zurich()).astimezone(ZURICH_TZ)
    window_start = target - timedelta(minutes=30)
    window_end = target + timedelta(hours=36)

    station_pats: List[Tuple[str, List[re.Pattern[str]]]] = []
    for st in stations or []:
        pats = station_match_patterns(st)
        if pats:
            station_pats.append((st, pats))

    line_tokens = set()
    for ln in lines or []:
        compact = re.sub(r"\s+", "", fold_accents(ln))
        if len(compact) >= 2:
            line_tokens.add(compact)

    query_terms: List[str] = []
    if query:
        # Support comma/pipe OR queries like "Bern, Fribourg" or single phrases
        for chunk in re.split(r"[,|;]", query):
            folded = fold_accents(chunk.strip())
            if folded:
                query_terms.append(folded)

    matched: List[Dict[str, Any]] = []
    seen_sigs = set()

    for item in alerts:
        start_dt: Optional[datetime] = item.get("_start_dt")
        end_dt: Optional[datetime] = item.get("_end_dt")

        if end_dt and end_dt < window_start:
            continue
        if not include_all_future and start_dt and start_dt > window_end:
            continue

        haystack_raw = " ".join(
            [
                item.get("title") or "",
                item.get("description") or "",
                " ".join(item.get("affected_stops") or []),
                " ".join(item.get("affected_lines") or []),
            ]
        )
        haystack = fold_accents(haystack_raw)

        if query_terms:
            if not any(
                all(tok in haystack for tok in term.split()) for term in query_terms
            ):
                continue

        if station_pats or line_tokens:
            hit = False
            for _st_name, pats in station_pats:
                if any(p.search(haystack) for p in pats):
                    hit = True
                    break
            if not hit and line_tokens:
                alert_lines = {
                    re.sub(r"\s+", "", fold_accents(x))
                    for x in (item.get("affected_lines") or [])
                }
                if alert_lines & line_tokens:
                    hit = True
            if not hit:
                continue

        sig = (fold_accents(item.get("title") or ""), item.get("start") or "")
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)

        clean_copy = {k: v for k, v in item.items() if not k.startswith("_")}
        matched.append(clean_copy)

    return matched


def get_hybrid_disruptions(
    *,
    mode: str,
    api_key: Optional[str],
    lang: str = "en",
    ref_time: Optional[datetime] = None,
    include_planned_siri: bool = False,
    allow_fallback: bool = True,
    timeout: int = 15,
    use_cache: bool = True,
) -> Tuple[List[Dict[str, Any]], str, List[str]]:
    """Fetch disruptions using hybrid strategy; returns (alerts, source_label, warnings)."""
    warnings: List[str] = []
    use_official = mode == "official" or (mode == "auto" and bool(api_key))

    if mode == "official" and not api_key:
        raise ApiError(
            "Official mode requires OPENTRANSPORTDATA_API_KEY (or ~/.config/sbb-skill/config.json)."
        )

    combined: List[Dict[str, Any]] = []
    sources_used: List[str] = []

    if use_official and api_key:
        try:
            siri_alerts = fetch_siri_sx_disruptions(
                api_key,
                include_planned=include_planned_siri,
                lang=lang,
                timeout=timeout,
                use_cache=use_cache,
            )
            combined.extend(siri_alerts)
            sources_used.append(
                "SIRI-SX (full)" if include_planned_siri else "SIRI-SX (unplanned)"
            )
        except ApiError as exc:
            if not allow_fallback and mode == "official":
                raise
            warnings.append(f"SIRI-SX unavailable ({exc}); falling back to data.sbb.ch.")

    # Always supplement with SBB rail-traffic-information unless full SIRI-SX already succeeded
    if not include_planned_siri or not combined:
        try:
            sbb_alerts = fetch_sbb_disruptions(
                ref_time=ref_time,
                timeout=timeout,
                use_cache=use_cache,
            )
            combined.extend(sbb_alerts)
            sources_used.append("data.sbb.ch (rail-traffic-information)")
        except ApiError as exc:
            if not combined:
                raise
            warnings.append(f"Supplemental data.sbb.ch fetch failed ({exc}).")

    return combined, " + ".join(sources_used), warnings


# ---------------------------------------------------------------------------
# Zero-Auth Backend: transport.opendata.ch
# ---------------------------------------------------------------------------


def opendata_search_stations(
    query: str,
    *,
    limit: int = 8,
    timeout: int = 15,
    use_cache: bool = True,
) -> List[Dict[str, Any]]:
    data = http_get_json(
        f"{TRANSPORT_API_BASE}/locations",
        params=[("query", query), ("type", "station")],
        timeout=timeout,
        cache_ttl=3600,
        use_cache=use_cache,
    )
    out: List[Dict[str, Any]] = []
    for st in (data.get("stations") or [])[:limit]:
        if not st.get("name"):
            continue
        coord = st.get("coordinate") or {}
        out.append(
            {
                "id": str(st.get("id") or ""),
                "name": st["name"],
                "icon": st.get("icon"),
                "lat": coord.get("x"),
                "lon": coord.get("y"),
            }
        )
    return out


def _compute_delay_mins(
    sched_dt: Optional[datetime],
    est_dt: Optional[datetime],
    explicit_delay: Any = None,
) -> Optional[int]:
    if isinstance(explicit_delay, int):
        return explicit_delay
    if isinstance(explicit_delay, str) and explicit_delay.lstrip("+-").isdigit():
        return int(explicit_delay)
    if sched_dt and est_dt:
        return int(round((est_dt - sched_dt).total_seconds() / 60.0))
    return None


def _clean_platform(plat: Any) -> Optional[str]:
    if plat is None:
        return None
    s = str(plat).strip()
    return s or None


def opendata_plan_trip(
    origin: str,
    destination: str,
    *,
    via: Optional[Sequence[str]] = None,
    target_dt: Optional[datetime] = None,
    is_arrival: bool = False,
    limit: int = 4,
    timeout: int = 15,
) -> Dict[str, Any]:
    params: List[Tuple[str, str]] = [
        ("from", origin),
        ("to", destination),
        ("limit", str(max(1, min(limit, 16)))),
    ]
    for v in via or []:
        params.append(("via[]", v))
    if target_dt:
        z_dt = target_dt.astimezone(ZURICH_TZ)
        params.append(("date", z_dt.strftime("%Y-%m-%d")))
        params.append(("time", z_dt.strftime("%H:%M")))
    if is_arrival:
        params.append(("isArrivalTime", "1"))

    data = http_get_json(
        f"{TRANSPORT_API_BASE}/connections",
        params=params,
        timeout=timeout,
        use_cache=False,
    )

    from_name = ((data.get("from") or {}).get("name")) or origin
    to_name = ((data.get("to") or {}).get("name")) or destination
    connections_out: List[Dict[str, Any]] = []

    for conn in data.get("connections") or []:
        c_from = conn.get("from") or {}
        c_to = conn.get("to") or {}
        dep_sched = parse_iso_dt(c_from.get("departure"))
        dep_est = parse_iso_dt((c_from.get("prognosis") or {}).get("departure"))
        arr_sched = parse_iso_dt(c_to.get("arrival"))
        arr_est = parse_iso_dt((c_to.get("prognosis") or {}).get("arrival"))

        dur_mins = parse_duration_minutes(conn.get("duration"))
        legs_out: List[Dict[str, Any]] = []
        corridor_stations: List[str] = [from_name, to_name]
        corridor_lines: List[str] = []

        for sec in conn.get("sections") or []:
            dep_obj = sec.get("departure") or {}
            arr_obj = sec.get("arrival") or {}
            dep_st = ((dep_obj.get("station") or {}).get("name")) or ""
            arr_st = ((arr_obj.get("station") or {}).get("name")) or ""
            for st_n in (dep_st, arr_st):
                if st_n and st_n not in corridor_stations:
                    corridor_stations.append(st_n)

            walk_obj = sec.get("walk")
            journey = sec.get("journey")
            if walk_obj is not None and not journey:
                walk_secs = walk_obj.get("duration") if isinstance(walk_obj, dict) else None
                walk_mins = int(round(walk_secs / 60.0)) if isinstance(walk_secs, (int, float)) else None
                legs_out.append(
                    {
                        "type": "walk",
                        "from": dep_st,
                        "to": arr_st,
                        "duration_mins": walk_mins,
                    }
                )
                continue

            if not journey:
                continue

            cat = (journey.get("category") or "").strip()
            num = str(journey.get("number") or "").strip()
            line_name = f"{cat} {num}".strip() or (journey.get("name") or "Train").strip()
            if line_name and line_name not in corridor_lines:
                corridor_lines.append(line_name)

            s_dep_sched = parse_iso_dt(dep_obj.get("departure"))
            s_dep_prog = dep_obj.get("prognosis") or {}
            s_dep_est = parse_iso_dt(s_dep_prog.get("departure"))
            s_dep_delay = _compute_delay_mins(s_dep_sched, s_dep_est, dep_obj.get("delay"))

            dep_plat_sched = _clean_platform(dep_obj.get("platform"))
            dep_plat_est = _clean_platform(s_dep_prog.get("platform"))
            # In transport.opendata.ch, `!` suffix on platform (e.g. "4!") also signals platform change
            plat_changed = False
            if dep_plat_sched and dep_plat_sched.endswith("!"):
                plat_changed = True
                dep_plat_sched = dep_plat_sched.rstrip("!").strip()
            if dep_plat_est and dep_plat_est.endswith("!"):
                plat_changed = True
                dep_plat_est = dep_plat_est.rstrip("!").strip()
            if dep_plat_sched and dep_plat_est and dep_plat_sched != dep_plat_est:
                plat_changed = True

            s_arr_sched = parse_iso_dt(arr_obj.get("arrival"))
            s_arr_prog = arr_obj.get("prognosis") or {}
            s_arr_est = parse_iso_dt(s_arr_prog.get("arrival"))
            s_arr_delay = _compute_delay_mins(s_arr_sched, s_arr_est, arr_obj.get("delay"))

            arr_plat_sched = _clean_platform(arr_obj.get("platform"))
            arr_plat_est = _clean_platform(s_arr_prog.get("platform"))
            arr_plat_changed = False
            if arr_plat_sched and arr_plat_sched.endswith("!"):
                arr_plat_changed = True
                arr_plat_sched = arr_plat_sched.rstrip("!").strip()
            if arr_plat_est and arr_plat_est.endswith("!"):
                arr_plat_changed = True
                arr_plat_est = arr_plat_est.rstrip("!").strip()
            if arr_plat_sched and arr_plat_est and arr_plat_sched != arr_plat_est:
                arr_plat_changed = True

            for pstop in journey.get("passList") or []:
                pst_name = ((pstop.get("station") or {}).get("name")) or ""
                if pst_name and pst_name not in corridor_stations:
                    corridor_stations.append(pst_name)

            cap1 = journey.get("capacity1st") or s_dep_prog.get("capacity1st") or conn.get("capacity1st")
            cap2 = journey.get("capacity2nd") or s_dep_prog.get("capacity2nd") or conn.get("capacity2nd")
            is_replacement = cat.upper() in ("EV", "BUS") and "ersatz" in fold_accents(
                journey.get("name") or ""
            ) or cat.upper() == "EV"

            legs_out.append(
                {
                    "type": "transit",
                    "line": line_name,
                    "category": cat,
                    "operator": journey.get("operator"),
                    "direction": journey.get("to"),
                    "from": dep_st,
                    "to": arr_st,
                    "dep_sched": s_dep_sched.isoformat() if s_dep_sched else None,
                    "dep_est": s_dep_est.isoformat() if s_dep_est else None,
                    "dep_delay_mins": s_dep_delay,
                    "dep_platform": dep_plat_est or dep_plat_sched,
                    "dep_platform_sched": dep_plat_sched,
                    "dep_platform_changed": plat_changed,
                    "arr_sched": s_arr_sched.isoformat() if s_arr_sched else None,
                    "arr_est": s_arr_est.isoformat() if s_arr_est else None,
                    "arr_delay_mins": s_arr_delay,
                    "arr_platform": arr_plat_est or arr_plat_sched,
                    "arr_platform_sched": arr_plat_sched,
                    "arr_platform_changed": arr_plat_changed,
                    "cancelled": False,
                    "rail_replacement": is_replacement,
                    "capacity_1st": int(cap1) if str(cap1 or "").isdigit() else None,
                    "capacity_2nd": int(cap2) if str(cap2 or "").isdigit() else None,
                    "situations": [],
                    "_dep_dt": s_dep_est or s_dep_sched,
                    "_arr_dt": s_arr_est or s_arr_sched,
                    "_dep_sched_dt": s_dep_sched,
                    "_arr_sched_dt": s_arr_sched,
                }
            )

        transfer_warnings = _audit_transfers(legs_out)
        for lg in legs_out:
            for k in list(lg.keys()):
                if k.startswith("_"):
                    lg.pop(k, None)

        connections_out.append(
            {
                "from": c_from.get("station", {}).get("name") or from_name,
                "to": c_to.get("station", {}).get("name") or to_name,
                "dep_sched": dep_sched.isoformat() if dep_sched else None,
                "dep_est": dep_est.isoformat() if dep_est else None,
                "arr_sched": arr_sched.isoformat() if arr_sched else None,
                "arr_est": arr_est.isoformat() if arr_est else None,
                "duration_mins": dur_mins,
                "duration_text": fmt_duration_minutes(dur_mins),
                "transfers": conn.get("transfers", 0),
                "cancelled": False,
                "deviation": bool((conn.get("service") or {}).get("irregular")),
                "service_note": (conn.get("service") or {}).get("irregular"),
                "legs": legs_out,
                "transfer_warnings": transfer_warnings,
                "situations": [],
                "corridor_stations": corridor_stations,
                "corridor_lines": corridor_lines,
            }
        )

    return {
        "backend": "transport.opendata.ch (zero-auth)",
        "from": from_name,
        "to": to_name,
        "connections": connections_out,
    }


def _audit_transfers(legs: Sequence[Dict[str, Any]]) -> List[str]:
    warnings: List[str] = []
    transit_legs = [lg for lg in legs if lg.get("type") == "transit"]
    for idx in range(len(transit_legs) - 1):
        prev_leg = transit_legs[idx]
        next_leg = transit_legs[idx + 1]
        arr_eff: Optional[datetime] = prev_leg.get("_arr_dt")
        dep_eff: Optional[datetime] = next_leg.get("_dep_dt")
        arr_sched: Optional[datetime] = prev_leg.get("_arr_sched_dt")
        dep_sched: Optional[datetime] = next_leg.get("_dep_sched_dt")
        hub = prev_leg.get("to") or next_leg.get("from") or "transfer station"
        if arr_eff and dep_eff:
            eff_mins = int(round((dep_eff - arr_eff).total_seconds() / 60.0))
            sched_mins = (
                int(round((dep_sched - arr_sched).total_seconds() / 60.0))
                if arr_sched and dep_sched
                else eff_mins
            )
            if eff_mins < 0:
                warnings.append(
                    f"BROKEN TRANSFER at {hub}: {prev_leg['line']} arrives {fmt_time(arr_eff)}, "
                    f"after {next_leg['line']} departs {fmt_time(dep_eff)} ({eff_mins} min)."
                )
            elif eff_mins <= 2:
                warnings.append(
                    f"CRITICAL TRANSFER at {hub}: only {eff_mins} min between "
                    f"{prev_leg['line']} and {next_leg['line']} (scheduled {sched_mins} min)."
                )
            elif eff_mins < 5 and eff_mins < sched_mins:
                warnings.append(
                    f"Tight transfer at {hub}: {eff_mins} min left due to delay "
                    f"(scheduled {sched_mins} min)."
                )
    return warnings


def opendata_stationboard(
    station: str,
    *,
    to_filter: Optional[str] = None,
    target_dt: Optional[datetime] = None,
    arrivals: bool = False,
    limit: int = 10,
    timeout: int = 15,
) -> Dict[str, Any]:
    fetch_limit = max(limit * 4, 40) if to_filter else max(1, min(limit, 40))
    params: List[Tuple[str, str]] = [
        ("station", station),
        ("limit", str(fetch_limit)),
        ("type", "arrival" if arrivals else "departure"),
    ]
    if target_dt:
        params.append(("datetime", target_dt.astimezone(ZURICH_TZ).strftime("%Y-%m-%d %H:%M")))

    data = http_get_json(
        f"{TRANSPORT_API_BASE}/stationboard",
        params=params,
        timeout=timeout,
        use_cache=False,
    )
    resolved_station = ((data.get("station") or {}).get("name")) or station
    to_folded = fold_accents(to_filter.strip()) if to_filter else None

    entries: List[Dict[str, Any]] = []
    for item in data.get("stationboard") or []:
        dest = item.get("to") or ""
        pass_stops = [
            ((ps.get("station") or {}).get("name") or "")
            for ps in (item.get("passList") or [])
        ]
        if to_folded:
            hay = fold_accents(" ".join([dest] + pass_stops))
            if to_folded not in hay:
                continue

        stop = item.get("stop") or {}
        prog = stop.get("prognosis") or {}
        time_key = "arrival" if arrivals else "departure"
        t_sched = parse_iso_dt(stop.get(time_key) or stop.get("departure") or stop.get("arrival"))
        t_est = parse_iso_dt(prog.get(time_key) or prog.get("departure") or prog.get("arrival"))
        delay = _compute_delay_mins(t_sched, t_est, stop.get("delay"))

        plat_sched = _clean_platform(stop.get("platform"))
        plat_est = _clean_platform(prog.get("platform"))
        plat_changed = False
        if plat_sched and plat_sched.endswith("!"):
            plat_changed = True
            plat_sched = plat_sched.rstrip("!").strip()
        if plat_est and plat_est.endswith("!"):
            plat_changed = True
            plat_est = plat_est.rstrip("!").strip()
        if plat_sched and plat_est and plat_sched != plat_est:
            plat_changed = True

        cat = (item.get("category") or "").strip()
        num = str(item.get("number") or "").strip()
        line_name = f"{cat} {num}".strip() or (item.get("name") or "Train").strip()

        entries.append(
            {
                "line": line_name,
                "operator": item.get("operator"),
                "destination": dest,
                "sched": t_sched.isoformat() if t_sched else None,
                "est": t_est.isoformat() if t_est else None,
                "delay_mins": delay,
                "platform": plat_est or plat_sched,
                "platform_sched": plat_sched,
                "platform_changed": plat_changed,
                "cancelled": False,
            }
        )
        if len(entries) >= limit:
            break

    return {
        "backend": "transport.opendata.ch (zero-auth)",
        "station": resolved_station,
        "board_type": "arrivals" if arrivals else "departures",
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# Official National Backend: OJP 2.0 (opentransportdata.swiss)
# ---------------------------------------------------------------------------


def _xml_escape(val: str) -> str:
    return (
        val.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _ojp_post(api_key: str, xml_payload: str, *, timeout: int = 20, cache_ttl: int = 0) -> ET.Element:
    raw = http_fetch(
        OJP20_ENDPOINT,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/xml; charset=utf-8",
            "Accept": "application/xml, text/xml",
        },
        body=xml_payload.encode("utf-8"),
        timeout=timeout,
        cache_ttl=cache_ttl,
        use_cache=cache_ttl > 0,
    )
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ApiError(f"Invalid XML from OJP 2.0: {exc}") from exc


def _find_child_text(elem: Optional[ET.Element], path_tags: Sequence[str]) -> Optional[str]:
    cur = elem
    for wanted in path_tags:
        if cur is None:
            return None
        nxt = None
        for ch in cur:
            if _local_tag(ch) == wanted:
                nxt = ch
                break
        cur = nxt
    if cur is None:
        return None
    if (cur.text or "").strip():
        return cur.text.strip()
    for sub in cur.iter():
        if _local_tag(sub) == "Text" and (sub.text or "").strip():
            return sub.text.strip()
    return None


def _find_first_child(elem: Optional[ET.Element], tag_name: str) -> Optional[ET.Element]:
    if elem is None:
        return None
    for ch in elem:
        if _local_tag(ch) == tag_name:
            return ch
    return None


def ojp_search_stations(
    api_key: str,
    query: str,
    *,
    lang: str = "en",
    limit: int = 8,
    timeout: int = 15,
    use_cache: bool = True,
) -> List[Dict[str, Any]]:
    now_utc = datetime.now(UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")
    xml_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<OJP xmlns="{OJP_NS}" xmlns:siri="{SIRI_NS}" version="2.0">
  <OJPRequest>
    <siri:ServiceRequest>
      <siri:ServiceRequestContext>
        <siri:Language>{_xml_escape(lang)}</siri:Language>
      </siri:ServiceRequestContext>
      <siri:RequestTimestamp>{now_utc}</siri:RequestTimestamp>
      <siri:RequestorRef>sbb_trip_planner_skill</siri:RequestorRef>
      <OJPLocationInformationRequest>
        <siri:RequestTimestamp>{now_utc}</siri:RequestTimestamp>
        <InitialInput>
          <Name>{_xml_escape(query)}</Name>
        </InitialInput>
        <Restrictions>
          <Type>stop</Type>
          <NumberOfResults>{max(1, min(limit, 20))}</NumberOfResults>
          <IncludePtStops>true</IncludePtStops>
        </Restrictions>
      </OJPLocationInformationRequest>
    </siri:ServiceRequest>
  </OJPRequest>
</OJP>"""
    root = _ojp_post(
        api_key,
        xml_body,
        timeout=timeout,
        cache_ttl=86400 if use_cache else 0,
    )
    results: List[Dict[str, Any]] = []
    for pr in root.iter():
        if _local_tag(pr) != "PlaceResult":
            continue
        place = _find_first_child(pr, "Place")
        if place is None:
            continue
        stop_place = _find_first_child(place, "StopPlace")
        stop_point = _find_first_child(place, "StopPoint")
        ref = (
            _find_child_text(stop_place, ("StopPlaceRef",))
            or _find_child_text(stop_point, ("StopPointRef",))
        )
        ref_tag = "StopPlaceRef" if _find_child_text(stop_place, ("StopPlaceRef",)) else "siri:StopPointRef"
        name = (
            _find_child_text(stop_place, ("StopPlaceName",))
            or _find_child_text(stop_point, ("StopPointName",))
            or _find_child_text(place, ("Name",))
        )
        if not ref or not name:
            continue
        geo = _find_first_child(place, "GeoPosition")
        lat_s = _find_child_text(geo, ("Latitude",))
        lon_s = _find_child_text(geo, ("Longitude",))
        results.append(
            {
                "id": ref,
                "ref_tag": ref_tag,
                "name": name,
                "icon": "train",
                "lat": float(lat_s) if lat_s else None,
                "lon": float(lon_s) if lon_s else None,
            }
        )
        if len(results) >= limit:
            break
    return results


def _resolve_ojp_stop(api_key: str, query: str, lang: str, timeout: int) -> Tuple[str, str, str]:
    matches = ojp_search_stations(api_key, query, lang=lang, limit=3, timeout=timeout)
    if not matches:
        raise ApiError(f"OJP 2.0 could not resolve stop name {query!r}.")
    best = matches[0]
    return best["id"], best.get("ref_tag", "StopPlaceRef"), best["name"]


def ojp_plan_trip(
    api_key: str,
    origin: str,
    destination: str,
    *,
    via: Optional[Sequence[str]] = None,
    target_dt: Optional[datetime] = None,
    is_arrival: bool = False,
    limit: int = 4,
    lang: str = "en",
    timeout: int = 20,
) -> Dict[str, Any]:
    orig_ref, orig_tag, orig_name = _resolve_ojp_stop(api_key, origin, lang, timeout)
    dest_ref, dest_tag, dest_name = _resolve_ojp_stop(api_key, destination, lang, timeout)

    via_xml_blocks: List[str] = []
    for v in via or []:
        v_ref, v_tag, v_name = _resolve_ojp_stop(api_key, v, lang, timeout)
        via_xml_blocks.append(
            f"""        <Via>
          <ViaPoint>
            <{v_tag}>{_xml_escape(v_ref)}</{v_tag}>
            <Name><Text>{_xml_escape(v_name)}</Text></Name>
          </ViaPoint>
        </Via>"""
        )

    now_utc = datetime.now(UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")
    dep_arr_utc = (target_dt or now_zurich()).astimezone(UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")
    orig_time_xml = f"<DepArrTime>{dep_arr_utc}</DepArrTime>" if not is_arrival else ""
    dest_time_xml = f"<DepArrTime>{dep_arr_utc}</DepArrTime>" if is_arrival else ""

    xml_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<OJP xmlns="{OJP_NS}" xmlns:siri="{SIRI_NS}" version="2.0">
  <OJPRequest>
    <siri:ServiceRequest>
      <siri:ServiceRequestContext>
        <siri:Language>{_xml_escape(lang)}</siri:Language>
      </siri:ServiceRequestContext>
      <siri:RequestTimestamp>{now_utc}</siri:RequestTimestamp>
      <siri:RequestorRef>sbb_trip_planner_skill</siri:RequestorRef>
      <OJPTripRequest>
        <siri:RequestTimestamp>{now_utc}</siri:RequestTimestamp>
        <Origin>
          <PlaceRef>
            <{orig_tag}>{_xml_escape(orig_ref)}</{orig_tag}>
            <Name><Text>{_xml_escape(orig_name)}</Text></Name>
          </PlaceRef>
          {orig_time_xml}
        </Origin>
        <Destination>
          <PlaceRef>
            <{dest_tag}>{_xml_escape(dest_ref)}</{dest_tag}>
            <Name><Text>{_xml_escape(dest_name)}</Text></Name>
          </PlaceRef>
          {dest_time_xml}
        </Destination>
{chr(10).join(via_xml_blocks)}
        <Params>
          <NumberOfResults>{max(1, min(limit, 12))}</NumberOfResults>
          <IncludeIntermediateStops>true</IncludeIntermediateStops>
          <UseRealtimeData>full</UseRealtimeData>
        </Params>
      </OJPTripRequest>
    </siri:ServiceRequest>
  </OJPRequest>
</OJP>"""
    root = _ojp_post(api_key, xml_body, timeout=timeout)

    # Map embedded PtSituationElements by SituationNumber
    sit_map: Dict[str, str] = {}
    for sit in parse_siri_sx_xml(ET.tostring(root, encoding="utf-8"), preferred_lang=lang):
        sit_id = sit.get("id") or ""
        label = sit.get("title") or ""
        if sit.get("description") and sit["description"] != label:
            label = f"{label} — {sit['description'][:180]}"
        if sit_id and label:
            sit_map[sit_id] = label

    connections_out: List[Dict[str, Any]] = []
    for tr in root.iter():
        if _local_tag(tr) != "TripResult":
            continue
        trip = _find_first_child(tr, "Trip")
        if trip is None:
            continue

        dur_mins = parse_duration_minutes(_find_child_text(trip, ("Duration",)))
        transfers_s = _find_child_text(trip, ("Transfers",))
        transfers = int(transfers_s) if transfers_s and transfers_s.isdigit() else 0
        trip_cancelled = (_find_child_text(trip, ("Cancelled",)) or "").lower() == "true"
        trip_deviation = (_find_child_text(trip, ("Deviation",)) or "").lower() == "true"

        legs_out: List[Dict[str, Any]] = []
        corridor_stations: List[str] = [orig_name, dest_name]
        corridor_lines: List[str] = []
        trip_situations: List[str] = []

        for leg_el in trip:
            if _local_tag(leg_el) not in ("Leg", "TripLeg"):
                continue
            timed = _find_first_child(leg_el, "TimedLeg")
            transfer_leg = _find_first_child(leg_el, "TransferLeg") or _find_first_child(
                leg_el, "ContinuousLeg"
            )

            if transfer_leg is not None and timed is None:
                w_mins = parse_duration_minutes(_find_child_text(transfer_leg, ("Duration",)))
                legs_out.append(
                    {
                        "type": "walk",
                        "from": "",
                        "to": "",
                        "duration_mins": w_mins,
                    }
                )
                continue

            if timed is None:
                continue

            board = _find_first_child(timed, "LegBoard")
            alight = _find_first_child(timed, "LegAlight")
            service = _find_first_child(timed, "Service")

            b_name = _find_child_text(board, ("StopPointName",)) or orig_name
            a_name = _find_child_text(alight, ("StopPointName",)) or dest_name
            for st_n in (b_name, a_name):
                if st_n and st_n not in corridor_stations:
                    corridor_stations.append(st_n)

            for child in timed:
                if _local_tag(child) in ("LegIntermediates", "LegIntermediate"):
                    ist_name = _find_child_text(child, ("StopPointName",))
                    if ist_name and ist_name not in corridor_stations:
                        corridor_stations.append(ist_name)

            svc_dep = _find_first_child(board, "ServiceDeparture")
            dep_sched = parse_iso_dt(_find_child_text(svc_dep, ("TimetabledTime",)))
            dep_est = parse_iso_dt(_find_child_text(svc_dep, ("EstimatedTime",)))
            dep_delay = _compute_delay_mins(dep_sched, dep_est)
            dep_plat_sched = _clean_platform(_find_child_text(board, ("PlannedQuay",)))
            dep_plat_est = _clean_platform(_find_child_text(board, ("EstimatedQuay",)))
            dep_plat_changed = bool(
                dep_plat_sched and dep_plat_est and dep_plat_sched != dep_plat_est
            )

            svc_arr = _find_first_child(alight, "ServiceArrival")
            arr_sched = parse_iso_dt(_find_child_text(svc_arr, ("TimetabledTime",)))
            arr_est = parse_iso_dt(_find_child_text(svc_arr, ("EstimatedTime",)))
            arr_delay = _compute_delay_mins(arr_sched, arr_est)
            arr_plat_sched = _clean_platform(_find_child_text(alight, ("PlannedQuay",)))
            arr_plat_est = _clean_platform(_find_child_text(alight, ("EstimatedQuay",)))
            arr_plat_changed = bool(
                arr_plat_sched and arr_plat_est and arr_plat_sched != arr_plat_est
            )

            line_name = (
                _find_child_text(service, ("PublishedServiceName",))
                or _find_child_text(service, ("PublishedLineName",))
                or "Train"
            )
            if line_name and line_name not in corridor_lines:
                corridor_lines.append(line_name)

            direction = _find_child_text(service, ("DestinationText",))
            leg_cancelled = (_find_child_text(service, ("Cancelled",)) or "").lower() == "true"
            leg_deviation = (_find_child_text(service, ("Deviation",)) or "").lower() == "true"
            if leg_deviation:
                trip_deviation = True

            mode_el = _find_first_child(service, "Mode")
            pt_mode = (_find_child_text(mode_el, ("PtMode",)) or "").lower()
            is_replacement = line_name.upper().startswith("EV") or "railreplacement" in pt_mode

            leg_sits: List[str] = []
            if service is not None:
                for sub in service.iter():
                    if _local_tag(sub) == "SituationNumber" and (sub.text or "").strip():
                        s_id = sub.text.strip()
                        s_txt = sit_map.get(s_id, f"Situation {s_id}")
                        if s_txt not in leg_sits:
                            leg_sits.append(s_txt)
                        if s_txt not in trip_situations:
                            trip_situations.append(s_txt)

            legs_out.append(
                {
                    "type": "transit",
                    "line": line_name,
                    "category": pt_mode or None,
                    "operator": _find_child_text(service, ("OperatorRef",)),
                    "direction": direction,
                    "from": b_name,
                    "to": a_name,
                    "dep_sched": dep_sched.isoformat() if dep_sched else None,
                    "dep_est": dep_est.isoformat() if dep_est else None,
                    "dep_delay_mins": dep_delay,
                    "dep_platform": dep_plat_est or dep_plat_sched,
                    "dep_platform_sched": dep_plat_sched,
                    "dep_platform_changed": dep_plat_changed,
                    "arr_sched": arr_sched.isoformat() if arr_sched else None,
                    "arr_est": arr_est.isoformat() if arr_est else None,
                    "arr_delay_mins": arr_delay,
                    "arr_platform": arr_plat_est or arr_plat_sched,
                    "arr_platform_sched": arr_plat_sched,
                    "arr_platform_changed": arr_plat_changed,
                    "cancelled": leg_cancelled,
                    "rail_replacement": is_replacement,
                    "capacity_1st": None,
                    "capacity_2nd": None,
                    "situations": leg_sits,
                    "_dep_dt": dep_est or dep_sched,
                    "_arr_dt": arr_est or arr_sched,
                    "_dep_sched_dt": dep_sched,
                    "_arr_sched_dt": arr_sched,
                }
            )

        transit_only = [lg for lg in legs_out if lg.get("type") == "transit"]
        dep_first_sched = transit_only[0]["dep_sched"] if transit_only else None
        dep_first_est = transit_only[0]["dep_est"] if transit_only else None
        arr_last_sched = transit_only[-1]["arr_sched"] if transit_only else None
        arr_last_est = transit_only[-1]["arr_est"] if transit_only else None

        transfer_warnings = _audit_transfers(legs_out)
        for lg in legs_out:
            for k in list(lg.keys()):
                if k.startswith("_"):
                    lg.pop(k, None)

        connections_out.append(
            {
                "from": transit_only[0]["from"] if transit_only else orig_name,
                "to": transit_only[-1]["to"] if transit_only else dest_name,
                "dep_sched": dep_first_sched,
                "dep_est": dep_first_est,
                "arr_sched": arr_last_sched,
                "arr_est": arr_last_est,
                "duration_mins": dur_mins,
                "duration_text": fmt_duration_minutes(dur_mins),
                "transfers": transfers,
                "cancelled": trip_cancelled or any(lg.get("cancelled") for lg in transit_only),
                "deviation": trip_deviation,
                "service_note": "Cancelled" if trip_cancelled else ("Deviation" if trip_deviation else None),
                "legs": legs_out,
                "transfer_warnings": transfer_warnings,
                "situations": trip_situations,
                "corridor_stations": corridor_stations,
                "corridor_lines": corridor_lines,
            }
        )

    return {
        "backend": "opentransportdata.swiss OJP 2.0 (official)",
        "from": orig_name,
        "to": dest_name,
        "connections": connections_out,
    }


def ojp_stationboard(
    api_key: str,
    station: str,
    *,
    to_filter: Optional[str] = None,
    target_dt: Optional[datetime] = None,
    arrivals: bool = False,
    limit: int = 10,
    lang: str = "en",
    timeout: int = 20,
) -> Dict[str, Any]:
    st_ref, st_tag, st_name = _resolve_ojp_stop(api_key, station, lang, timeout)
    now_utc = datetime.now(UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")
    dep_arr_utc = (target_dt or now_zurich()).astimezone(UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")
    fetch_limit = max(limit * 3, 30) if to_filter else max(1, min(limit, 40))
    ev_type = "arrival" if arrivals else "departure"

    xml_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<OJP xmlns="{OJP_NS}" xmlns:siri="{SIRI_NS}" version="2.0">
  <OJPRequest>
    <siri:ServiceRequest>
      <siri:ServiceRequestContext>
        <siri:Language>{_xml_escape(lang)}</siri:Language>
      </siri:ServiceRequestContext>
      <siri:RequestTimestamp>{now_utc}</siri:RequestTimestamp>
      <siri:RequestorRef>sbb_trip_planner_skill</siri:RequestorRef>
      <OJPStopEventRequest>
        <siri:RequestTimestamp>{now_utc}</siri:RequestTimestamp>
        <Location>
          <PlaceRef>
            <{st_tag}>{_xml_escape(st_ref)}</{st_tag}>
            <Name><Text>{_xml_escape(st_name)}</Text></Name>
          </PlaceRef>
          <DepArrTime>{dep_arr_utc}</DepArrTime>
        </Location>
        <Params>
          <NumberOfResults>{fetch_limit}</NumberOfResults>
          <StopEventType>{ev_type}</StopEventType>
          <IncludeOnwardCalls>true</IncludeOnwardCalls>
          <UseRealtimeData>full</UseRealtimeData>
        </Params>
      </OJPStopEventRequest>
    </siri:ServiceRequest>
  </OJPRequest>
</OJP>"""
    root = _ojp_post(api_key, xml_body, timeout=timeout)
    to_folded = fold_accents(to_filter.strip()) if to_filter else None
    entries: List[Dict[str, Any]] = []

    for ser in root.iter():
        if _local_tag(ser) != "StopEventResult":
            continue
        se = _find_first_child(ser, "StopEvent")
        if se is None:
            continue
        this_call = _find_first_child(se, "ThisCall")
        call_at_stop = _find_first_child(this_call, "CallAtStop")
        service = _find_first_child(se, "Service")

        dest = (
            _find_child_text(service, ("DestinationText",))
            or _find_child_text(service, ("OriginText",))
            or ""
        )
        onward_names: List[str] = []
        for ch in se:
            if _local_tag(ch) == "OnwardCall":
                cs = _find_first_child(ch, "CallAtStop")
                on_name = _find_child_text(cs, ("StopPointName",))
                if on_name:
                    onward_names.append(on_name)

        if to_folded:
            hay = fold_accents(" ".join([dest] + onward_names))
            if to_folded not in hay:
                continue

        svc_time_el = _find_first_child(
            call_at_stop, "ServiceArrival" if arrivals else "ServiceDeparture"
        )
        t_sched = parse_iso_dt(_find_child_text(svc_time_el, ("TimetabledTime",)))
        t_est = parse_iso_dt(_find_child_text(svc_time_el, ("EstimatedTime",)))
        delay = _compute_delay_mins(t_sched, t_est)

        plat_sched = _clean_platform(_find_child_text(call_at_stop, ("PlannedQuay",)))
        plat_est = _clean_platform(_find_child_text(call_at_stop, ("EstimatedQuay",)))
        plat_changed = bool(plat_sched and plat_est and plat_sched != plat_est)

        line_name = (
            _find_child_text(service, ("PublishedServiceName",))
            or _find_child_text(service, ("PublishedLineName",))
            or "Train"
        )
        cancelled = (_find_child_text(service, ("Cancelled",)) or "").lower() == "true"

        entries.append(
            {
                "line": line_name,
                "operator": _find_child_text(service, ("OperatorRef",)),
                "destination": dest,
                "sched": t_sched.isoformat() if t_sched else None,
                "est": t_est.isoformat() if t_est else None,
                "delay_mins": delay,
                "platform": plat_est or plat_sched,
                "platform_sched": plat_sched,
                "platform_changed": plat_changed,
                "cancelled": cancelled,
            }
        )
        if len(entries) >= limit:
            break

    return {
        "backend": "opentransportdata.swiss OJP 2.0 (official)",
        "station": st_name,
        "board_type": "arrivals" if arrivals else "departures",
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# Markdown Formatting (Token-Efficient & Agent-Friendly)
# ---------------------------------------------------------------------------


def _fmt_delay_badge(delay_mins: Optional[int], cancelled: bool = False) -> str:
    if cancelled:
        return " [CANCELLED]"
    if delay_mins is None or delay_mins == 0:
        return ""
    if delay_mins > 0:
        return f" (+{delay_mins} min)"
    return f" ({delay_mins} min)"


def _fmt_platform(
    plat: Optional[str],
    plat_sched: Optional[str],
    changed: bool,
) -> str:
    if not plat:
        return ""
    if changed and plat_sched and plat_sched != plat:
        return f"Pl. {plat} (changed from {plat_sched}!)"
    if changed:
        return f"Pl. {plat} (platform change!)"
    return f"Pl. {plat}"


def format_trip_markdown(result: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"## Connections: {result['from']} → {result['to']}")
    lines.append(f"_Backend: {result['backend']}_")

    for w in result.get("warnings") or []:
        lines.append(f"> Note: {w}")

    corridor_alerts = result.get("corridor_disruptions") or []
    if corridor_alerts:
        lines.append("")
        lines.append(f"### ⚠️ Active Corridor Disruptions ({len(corridor_alerts)})")
        for al in corridor_alerts[:6]:
            win = ""
            s_dt = parse_iso_dt(al.get("start"))
            e_dt = parse_iso_dt(al.get("end"))
            if s_dt or e_dt:
                win = f" ({fmt_dt(s_dt)} → {fmt_dt(e_dt)})"
            lines.append(f"- **{al['title']}**{win}")
            if al.get("description"):
                snippet = al["description"].replace("\n", " ")
                if len(snippet) > 260:
                    snippet = snippet[:257] + "..."
                lines.append(f"  {snippet}")
            if al.get("link"):
                lines.append(f"  Link: {al['link']}")
    else:
        lines.append("")
        lines.append("_No active rail disruptions matched stations/lines on these connections._")

    conns = result.get("connections") or []
    if not conns:
        lines.append("")
        lines.append("No connections found for the requested query/time.")
        return "\n".join(lines)

    for idx, conn in enumerate(conns, start=1):
        dep_s = parse_iso_dt(conn.get("dep_sched"))
        arr_s = parse_iso_dt(conn.get("arr_sched"))
        status_tags: List[str] = []
        if conn.get("cancelled"):
            status_tags.append("CANCELLED")
        if conn.get("deviation"):
            status_tags.append("ALTERED / DEVIATION")
        status_str = f" [{' | '.join(status_tags)}]" if status_tags else ""
        date_prefix = dep_s.strftime("%Y-%m-%d ") if dep_s else ""

        lines.append("")
        lines.append(
            f"### Option {idx}: {date_prefix}{fmt_time(dep_s)} → {fmt_time(arr_s)} "
            f"({conn['duration_text']}, {conn['transfers']} transfer(s)){status_str}"
        )

        for tw in conn.get("transfer_warnings") or []:
            lines.append(f"- ⚠️ **{tw}**")
        for sit in conn.get("situations") or []:
            lines.append(f"- ⚠️ {sit}")

        for leg in conn.get("legs") or []:
            if leg.get("type") == "walk":
                w_str = f" ({leg['duration_mins']} min)" if leg.get("duration_mins") else ""
                target = f" to {leg['to']}" if leg.get("to") else ""
                lines.append(f"- Walk{target}{w_str}")
                continue

            d_sched = parse_iso_dt(leg.get("dep_sched"))
            a_sched = parse_iso_dt(leg.get("arr_sched"))
            d_badge = _fmt_delay_badge(leg.get("dep_delay_mins"), leg.get("cancelled", False))
            a_badge = _fmt_delay_badge(leg.get("arr_delay_mins"), False)
            d_plat = _fmt_platform(
                leg.get("dep_platform"),
                leg.get("dep_platform_sched"),
                bool(leg.get("dep_platform_changed")),
            )
            a_plat = _fmt_platform(
                leg.get("arr_platform"),
                leg.get("arr_platform_sched"),
                bool(leg.get("arr_platform_changed")),
            )
            d_plat_s = f" [{d_plat}]" if d_plat else ""
            a_plat_s = f" [{a_plat}]" if a_plat else ""
            dir_s = f" (dir. {leg['direction']})" if leg.get("direction") else ""
            repl_s = " **[RAIL REPLACEMENT BUS]**" if leg.get("rail_replacement") else ""

            occ_parts: List[str] = []
            if leg.get("capacity_1st") in CAPACITY_LABELS:
                occ_parts.append(f"1st: {CAPACITY_LABELS[leg['capacity_1st']]}")
            if leg.get("capacity_2nd") in CAPACITY_LABELS:
                occ_parts.append(f"2nd: {CAPACITY_LABELS[leg['capacity_2nd']]}")
            occ_s = f" | Occupancy {'; '.join(occ_parts)}" if occ_parts else ""

            lines.append(
                f"- **{leg['line']}**{dir_s}{repl_s}: "
                f"{fmt_time(d_sched)}{d_badge} {leg['from']}{d_plat_s} → "
                f"{fmt_time(a_sched)}{a_badge} {leg['to']}{a_plat_s}{occ_s}"
            )

    return "\n".join(lines)


def format_disruptions_markdown(result: Dict[str, Any]) -> str:
    alerts = result.get("disruptions") or []
    lines: List[str] = []
    q_label = f" matching '{result['query']}'" if result.get("query") else ""
    lines.append(f"## Active Swiss Rail Disruptions{q_label} ({len(alerts)})")
    lines.append(f"_Sources: {result['sources']}_")

    for w in result.get("warnings") or []:
        lines.append(f"> Note: {w}")

    if not alerts:
        lines.append("")
        lines.append("No active service disruptions matched your filter.")
        return "\n".join(lines)

    for al in alerts:
        s_dt = parse_iso_dt(al.get("start"))
        e_dt = parse_iso_dt(al.get("end"))
        validity = ""
        if s_dt or e_dt:
            validity = f"Valid: {fmt_dt(s_dt)} → {fmt_dt(e_dt)}"
        if al.get("daily_window"):
            validity = f"{validity} (daily {al['daily_window']})".strip()

        sev = f" [{al['severity']}]" if al.get("severity") else ""
        lines.append("")
        lines.append(f"### {al['title']}{sev}")
        if validity:
            lines.append(f"- **{validity}**")
        if al.get("cause"):
            lines.append(f"- **Cause:** {al['cause']}")
        if al.get("affected_lines"):
            lines.append(f"- **Lines:** {', '.join(al['affected_lines'])}")
        if al.get("description"):
            lines.append(f"- **Details:** {al['description']}")
        if al.get("link"):
            lines.append(f"- **More info:** {al['link']}")

    return "\n".join(lines)


def format_board_markdown(result: Dict[str, Any]) -> str:
    entries = result.get("entries") or []
    btype = result.get("board_type", "departures").capitalize()
    lines: List[str] = [
        f"## {btype} — {result['station']}",
        f"_Backend: {result['backend']}_",
        "",
    ]
    for w in result.get("warnings") or []:
        lines.append(f"> Note: {w}")

    if not entries:
        lines.append("No upcoming services found.")
        return "\n".join(lines)

    lines.append("| Time | Status | Line | Direction | Platform |")
    lines.append("| :--- | :--- | :--- | :--- | :--- |")
    for ent in entries:
        t_sched = parse_iso_dt(ent.get("sched"))
        t_est = parse_iso_dt(ent.get("est"))
        delay = ent.get("delay_mins")
        if ent.get("cancelled"):
            status = "CANCELLED"
        elif delay and delay > 0:
            status = f"+{delay} min ({fmt_time(t_est)})"
        elif delay == 0 and t_est:
            status = "On time"
        else:
            status = "Scheduled"
        plat = _fmt_platform(
            ent.get("platform"),
            ent.get("platform_sched"),
            bool(ent.get("platform_changed")),
        ) or "-"
        lines.append(
            f"| {fmt_time(t_sched)} | {status} | **{ent['line']}** | {ent['destination']} | {plat} |"
        )
    return "\n".join(lines)


def format_stations_markdown(result: Dict[str, Any]) -> str:
    stations = result.get("stations") or []
    lines = [
        f"## Station Matches for '{result['query']}'",
        f"_Backend: {result['backend']}_",
        "",
    ]
    if not stations:
        lines.append("No matching Swiss public transport stops found.")
        return "\n".join(lines)
    for st in stations:
        coords = (
            f" ({st['lat']:.4f}, {st['lon']:.4f})"
            if isinstance(st.get("lat"), (int, float)) and isinstance(st.get("lon"), (int, float))
            else ""
        )
        lines.append(f"- **{st['name']}** — ID: `{st['id']}`{coords}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Subcommand Handlers
# ---------------------------------------------------------------------------


def _norm_station_arg(name: str) -> str:
    return name.replace("_", " ").strip()


def cmd_trip(args: argparse.Namespace) -> int:
    origin = _norm_station_arg(args.origin)
    destination = _norm_station_arg(args.destination)
    via_list = [_norm_station_arg(v) for v in (args.via or [])]
    api_key = resolve_api_key(args.api_key)
    lang = resolve_lang(args.lang)
    target_dt = parse_user_datetime(args.datetime, args.date, args.time)
    warnings: List[str] = []

    use_official = args.mode == "official" or (args.mode == "auto" and bool(api_key))
    if args.mode == "official" and not api_key:
        raise ApiError("Official mode requested, but OPENTRANSPORTDATA_API_KEY is not set.")

    trip_data: Optional[Dict[str, Any]] = None
    if use_official and api_key:
        try:
            trip_data = ojp_plan_trip(
                api_key,
                origin,
                destination,
                via=via_list,
                target_dt=target_dt,
                is_arrival=args.arrival,
                limit=args.limit,
                lang=lang,
                timeout=args.timeout,
            )
        except ApiError as exc:
            if args.no_fallback or args.mode == "official":
                raise
            warnings.append(f"OJP 2.0 routing unavailable ({exc}); switched to transport.opendata.ch.")

    if trip_data is None:
        trip_data = opendata_plan_trip(
            origin,
            destination,
            via=via_list,
            target_dt=target_dt,
            is_arrival=args.arrival,
            limit=args.limit,
            timeout=args.timeout,
        )

    corridor_disruptions: List[Dict[str, Any]] = []
    if not args.skip_disruptions:
        all_stations: List[str] = []
        all_lines: List[str] = []
        for conn in trip_data.get("connections") or []:
            for st in conn.get("corridor_stations") or []:
                if st not in all_stations:
                    all_stations.append(st)
            for ln in conn.get("corridor_lines") or []:
                if ln not in all_lines:
                    all_lines.append(ln)
        try:
            raw_alerts, _src, d_warns = get_hybrid_disruptions(
                mode=args.mode,
                api_key=api_key,
                lang=lang,
                ref_time=target_dt,
                allow_fallback=not args.no_fallback,
                timeout=args.timeout,
                use_cache=not args.no_cache,
            )
            warnings.extend(d_warns)
            corridor_disruptions = filter_disruptions(
                raw_alerts,
                stations=all_stations or [origin, destination],
                lines=all_lines,
                ref_time=target_dt,
            )
        except ApiError as exc:
            warnings.append(f"Could not cross-check disruption feed ({exc}).")

    trip_data["corridor_disruptions"] = corridor_disruptions
    trip_data["warnings"] = warnings

    if args.json:
        print(json.dumps(trip_data, indent=2, ensure_ascii=False))
    else:
        print(format_trip_markdown(trip_data))
    return 0


def cmd_disruptions(args: argparse.Namespace) -> int:
    api_key = resolve_api_key(args.api_key)
    lang = resolve_lang(args.lang)
    target_dt = parse_user_datetime(args.datetime) if args.datetime else None

    raw_alerts, sources, warnings = get_hybrid_disruptions(
        mode=args.mode,
        api_key=api_key,
        lang=lang,
        ref_time=target_dt,
        include_planned_siri=args.include_planned_siri,
        allow_fallback=not args.no_fallback,
        timeout=args.timeout,
        use_cache=not args.no_cache,
    )
    filtered = filter_disruptions(
        raw_alerts,
        query=args.query,
        ref_time=target_dt,
        include_all_future=args.all,
    )
    if args.limit and args.limit > 0:
        filtered = filtered[: args.limit]

    payload = {
        "sources": sources,
        "query": args.query,
        "count": len(filtered),
        "warnings": warnings,
        "disruptions": filtered,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(format_disruptions_markdown(payload))
    return 0


def cmd_board(args: argparse.Namespace) -> int:
    station = _norm_station_arg(args.station)
    to_filter = _norm_station_arg(args.to) if args.to else None
    api_key = resolve_api_key(args.api_key)
    lang = resolve_lang(args.lang)
    target_dt = parse_user_datetime(args.datetime, args.date, args.time)
    warnings: List[str] = []

    use_official = args.mode == "official" or (args.mode == "auto" and bool(api_key))
    if args.mode == "official" and not api_key:
        raise ApiError("Official mode requested, but OPENTRANSPORTDATA_API_KEY is not set.")

    board_data: Optional[Dict[str, Any]] = None
    if use_official and api_key:
        try:
            board_data = ojp_stationboard(
                api_key,
                station,
                to_filter=to_filter,
                target_dt=target_dt,
                arrivals=args.arrivals,
                limit=args.limit,
                lang=lang,
                timeout=args.timeout,
            )
        except ApiError as exc:
            if args.no_fallback or args.mode == "official":
                raise
            warnings.append(f"OJP 2.0 stop board unavailable ({exc}); switched to transport.opendata.ch.")

    if board_data is None:
        board_data = opendata_stationboard(
            station,
            to_filter=to_filter,
            target_dt=target_dt,
            arrivals=args.arrivals,
            limit=args.limit,
            timeout=args.timeout,
        )

    board_data["warnings"] = warnings
    if args.json:
        print(json.dumps(board_data, indent=2, ensure_ascii=False))
    else:
        print(format_board_markdown(board_data))
    return 0


def cmd_stations(args: argparse.Namespace) -> int:
    query = _norm_station_arg(args.query)
    api_key = resolve_api_key(args.api_key)
    lang = resolve_lang(args.lang)
    use_official = args.mode == "official" or (args.mode == "auto" and bool(api_key))
    backend = "transport.opendata.ch (zero-auth)"
    stations: Optional[List[Dict[str, Any]]] = None

    if use_official and api_key:
        try:
            stations = ojp_search_stations(
                api_key,
                query,
                lang=lang,
                limit=args.limit,
                timeout=args.timeout,
                use_cache=not args.no_cache,
            )
            backend = "opentransportdata.swiss OJP 2.0 (official)"
        except ApiError:
            if args.no_fallback or args.mode == "official":
                raise

    if stations is None:
        stations = opendata_search_stations(
            query,
            limit=args.limit,
            timeout=args.timeout,
            use_cache=not args.no_cache,
        )

    payload = {"query": query, "backend": backend, "stations": stations}
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(format_stations_markdown(payload))
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config()
    changed = False
    if args.set_key is not None:
        if args.set_key == "":
            cfg.pop("api_key", None)
        else:
            cfg["api_key"] = args.set_key.strip()
        changed = True
    if args.set_lang:
        cfg["lang"] = args.set_lang.lower()
        changed = True
    if changed:
        saved_to = save_config(cfg)
        print(f"Saved configuration to {saved_to}")

    eff_key = resolve_api_key()
    masked = f"{eff_key[:4]}...{eff_key[-4:]}" if eff_key and len(eff_key) >= 10 else ("set" if eff_key else "not set")
    summary = {
        "config_path": str(get_config_path()),
        "api_key_configured": bool(eff_key),
        "api_key_preview": masked,
        "default_lang": resolve_lang(),
        "active_tier": "hybrid-official (OJP 2.0 + SIRI-SX + data.sbb.ch)" if eff_key else "zero-auth (transport.opendata.ch + data.sbb.ch)",
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"- Config file: `{summary['config_path']}`")
        print(f"- API key: **{summary['api_key_preview']}**")
        print(f"- Default language: `{summary['default_lang']}`")
        print(f"- Active tier: **{summary['active_tier']}**")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    api_key = resolve_api_key(args.api_key)
    checks: List[Dict[str, Any]] = []

    try:
        sts = opendata_search_stations("Bern", limit=1, timeout=args.timeout, use_cache=False)
        checks.append(
            {
                "service": "transport.opendata.ch (Timetables & Prognoses)",
                "ok": bool(sts),
                "detail": f"Resolved '{sts[0]['name']}' ({sts[0]['id']})" if sts else "Empty station list",
            }
        )
    except ApiError as exc:
        checks.append({"service": "transport.opendata.ch", "ok": False, "detail": str(exc)})

    try:
        dis = fetch_sbb_disruptions(timeout=args.timeout, use_cache=False)
        checks.append(
            {
                "service": "data.sbb.ch (rail-traffic-information Disruptions)",
                "ok": True,
                "detail": f"Fetched {len(dis)} rail traffic records",
            }
        )
    except ApiError as exc:
        checks.append({"service": "data.sbb.ch", "ok": False, "detail": str(exc)})

    if api_key:
        try:
            ojp_sts = ojp_search_stations(api_key, "Bern", limit=1, timeout=args.timeout, use_cache=False)
            checks.append(
                {
                    "service": "opentransportdata.swiss OJP 2.0",
                    "ok": bool(ojp_sts),
                    "detail": f"Resolved '{ojp_sts[0]['name']}' ({ojp_sts[0]['id']})" if ojp_sts else "Empty response",
                }
            )
        except ApiError as exc:
            checks.append({"service": "opentransportdata.swiss OJP 2.0", "ok": False, "detail": str(exc)})
    else:
        checks.append(
            {
                "service": "opentransportdata.swiss (Optional Official Tier)",
                "ok": True,
                "detail": "Skipped (no OPENTRANSPORTDATA_API_KEY configured; running in zero-auth mode)",
            }
        )

    payload = {
        "zurich_now": fmt_dt(now_zurich()),
        "api_key_configured": bool(api_key),
        "cache_dir": str(CACHE_DIR),
        "checks": checks,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"## SBB Skill Doctor ({payload['zurich_now']} Europe/Zurich)")
        for ch in checks:
            icon = "OK" if ch["ok"] else "FAIL"
            print(f"- [{icon}] **{ch['service']}**: {ch['detail']}")
    return 0 if all(c["ok"] for c in checks) else 1


# ---------------------------------------------------------------------------
# Argument Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--mode",
        choices=("auto", "zero-auth", "official"),
        default="auto",
        help="Backend selection (default: auto — uses official OJP 2.0/SIRI-SX when API key is set, else zero-auth).",
    )
    common.add_argument("--api-key", help="Explicit opentransportdata.swiss Bearer token.")
    common.add_argument("--lang", choices=("de", "fr", "it", "en"), help="Preferred text language.")
    common.add_argument("--timeout", type=int, default=15, help="HTTP timeout in seconds (default: 15).")
    common.add_argument("--no-cache", action="store_true", help="Bypass local /tmp response cache.")
    common.add_argument("--no-fallback", action="store_true", help="Disable fallback to zero-auth on official API errors.")
    common.add_argument("--json", action="store_true", help="Output structured JSON instead of Markdown.")

    parser = argparse.ArgumentParser(
        prog="sbb.py",
        description="Plan Swiss SBB train trips with real-time delays, station boards, and live service disruptions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # trip (alias: plan)
    p_trip = sub.add_parser(
        "trip",
        aliases=["plan"],
        parents=[common],
        help="Plan A-to-B train connections with live delays, transfer checks, and corridor disruptions.",
    )
    p_trip.add_argument("origin", help="Departure station (e.g. 'Zürich HB', 'Genève').")
    p_trip.add_argument("destination", help="Arrival station (e.g. 'Lausanne', 'Lugano').")
    p_trip.add_argument("--via", action="append", default=[], help="Intermediate stop (repeatable).")
    p_trip.add_argument("--datetime", help="Departure/arrival time (e.g. '2026-11-13 17:30', '17:30', 'tomorrow 08:00').")
    p_trip.add_argument("--date", help="Date YYYY-MM-DD (alternative to --datetime).")
    p_trip.add_argument("--time", help="Time HH:MM in Europe/Zurich (alternative to --datetime).")
    p_trip.add_argument("--arrival", action="store_true", help="Interpret target time as arrival time instead of departure.")
    p_trip.add_argument("--limit", type=int, default=4, help="Number of connections to return (default: 4).")
    p_trip.add_argument("--skip-disruptions", action="store_true", help="Skip automatic corridor disruption check.")
    p_trip.set_defaults(func=cmd_trip)

    # disruptions
    p_dis = sub.add_parser(
        "disruptions",
        aliases=["alerts"],
        parents=[common],
        help="Query ongoing and scheduled Swiss rail service disruptions.",
    )
    p_dis.add_argument("--query", "-q", help="Filter by station, line, or corridor (comma-separated for OR, e.g. 'Bern, Fribourg').")
    p_dis.add_argument("--datetime", help="Check alerts active around a specific date/time.")
    p_dis.add_argument("--all", action="store_true", help="Include future planned disruptions beyond the next 36 hours.")
    p_dis.add_argument(
        "--include-planned-siri",
        action="store_true",
        help="Query full SIRI-SX feed instead of unplanned-only (strict quota: 48/day).",
    )
    p_dis.add_argument("--limit", type=int, default=25, help="Max disruptions to return (default: 25).")
    p_dis.set_defaults(func=cmd_disruptions)

    # board
    p_board = sub.add_parser(
        "board",
        parents=[common],
        help="Show live station departure or arrival board with real-time delays and platform changes.",
    )
    p_board.add_argument("station", help="Station name (e.g. 'Bern', 'Zürich HB').")
    p_board.add_argument("--to", help="Filter trains calling at or heading toward this station.")
    p_board.add_argument("--datetime", help="Board start time (default: now).")
    p_board.add_argument("--date", help="Date YYYY-MM-DD.")
    p_board.add_argument("--time", help="Time HH:MM.")
    p_board.add_argument("--arrivals", action="store_true", help="Show arrivals board instead of departures.")
    p_board.add_argument("--limit", type=int, default=10, help="Number of entries to return (default: 10).")
    p_board.set_defaults(func=cmd_board)

    # stations
    p_st = sub.add_parser(
        "stations",
        parents=[common],
        help="Search Swiss public transport station names and DIDOK/SLOID identifiers.",
    )
    p_st.add_argument("query", help="Station name fragment (e.g. 'Biel', 'Geneve Aeroport').")
    p_st.add_argument("--limit", type=int, default=8, help="Max matches (default: 8).")
    p_st.set_defaults(func=cmd_stations)

    # config
    p_cfg = sub.add_parser(
        "config",
        parents=[common],
        help="Show or save optional opentransportdata.swiss API key & default language.",
    )
    p_cfg.add_argument("--set-key", help="Save opentransportdata.swiss API key (pass '' to clear).")
    p_cfg.add_argument("--set-lang", choices=("de", "fr", "it", "en"), help="Save default language.")
    p_cfg.set_defaults(func=cmd_config)

    # doctor
    p_doc = sub.add_parser(
        "doctor",
        parents=[common],
        help="Verify connectivity and active backend status.",
    )
    p_doc.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ApiError, ValueError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
